"""In-memory reference for GET /v1/attempts/{attempt_id}/artifact.

Authorization is two-part:

* The caller authenticates as a coordinator principal with its own key
  (``Authorization: Bearer <coordinator key>``). The host stores only key
  hashes. The coordinator identity comes from the key, never from a request
  field.
* Each attempt gets a scoped capability at create time, returned once to the
  creating principal only. The host stores only its SHA-256 and compares in
  constant time. It is bound to one attempt, scoped to that attempt's artifact
  and result, and can expire (TTL) or be revoked.

Launcher v0.1.15 does not yet implement this endpoint. Conformance tests
exercise this module so the contract is pinned before the host grows the
route. The implementation is planned; do not treat this as the live server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

try:
    from .privatefs import write_private
    from .project_state import DispatchRefused, check_dispatch
except ImportError:  # loaded by path
    from contract.v1.privatefs import write_private
    from contract.v1.project_state import DispatchRefused, check_dispatch

SCHEMA = "hyvv.worker-artifact/1"
DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
UNSAFE_ID_RE = re.compile(r"[\\/]|(\.\.)")

ERROR_HTTP = {
    "invalid_attempt_id": 400,
    "unauthorized": 401,
    "capability_required": 401,
    "not_owner": 403,
    "capability_invalid": 403,
    "capability_expired": 403,
    "capability_revoked": 403,
    "unknown_attempt": 404,
    "result_missing": 404,
    "result_pending": 409,
    "no_result_for_state": 409,
    "artifact_mismatch": 409,
    "artifact_changed": 409,
    "artifact_unsafe": 409,
    "artifact_oversized": 413,
    "artifact_malformed": 502,
    "worker_disconnected": 503,
}

TERMINAL_WITHOUT_RESULT = {"cancelled", "not_started"}
PENDING_STATES = {"queued", "backoff", "starting", "running", "cancel_requested"}
RETRYABLE = {"worker_disconnected"}


CAPABILITY_SCOPE = ("artifact", "result")
CAPABILITY_PREFIX = "hyvvcap1_"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ArtifactError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        self.http = ERROR_HTTP[code]
        self.retryable = code in RETRYABLE
        super().__init__(self.message)

    def body(self) -> dict[str, Any]:
        out = {"code": self.code, "error": self.code, "message": self.message}
        if self.retryable:
            out["retryable"] = True
        return out


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


class ArtifactStore:
    """Coordinator-only artifact store with first-read pinning and safe files."""

    def __init__(self, root: Path | None = None, max_bytes: int | None = None, *,
                 principals: dict[str, str] | None = None, capability_ttl_s: float | None = None, clock=time.time,
                 project_roots: dict[str, Path | str] | None = None):
        self.root = Path(root or tempfile.mkdtemp(prefix="hyvv-artifact-"))
        self.results = self.root / "results"
        self.results.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes or os.environ.get("HYVV_ARTIFACT_MAX_BYTES") or DEFAULT_MAX_BYTES)
        # Per-coordinator keys; only their hashes are kept.
        self._principal_hashes: dict[str, str] = {name: _sha256_hex(key) for name, key in (principals or {}).items()}
        self.capability_ttl_s = capability_ttl_s
        self.clock = clock
        self.cap_path = self.root / "capabilities.json"
        self.caps: dict[str, dict[str, Any]] = {}  # attempt_id -> {sha256, expires_at, revoked, scope}
        self.attempts: dict[str, dict[str, Any]] = {}
        self.pin_path = self.root / "artifact-pins.json"
        # attempt_id -> {"result_sha256", "body_sha256"}. Digests only: raw
        # patches live solely in the owner-only result files, never in pins.
        self.pins: dict[str, dict[str, str]] = {}
        self.bodies: dict[str, bytes] = {}  # in-memory cache of pinned response bytes (not persisted)
        self.disconnected = False
        self.logs: list[str] = []
        # Project name -> primary repo root holding .workflow/project-state.json.
        self.project_roots = {k: Path(v) for k, v in (project_roots or {}).items()}
        self.override_path = self.root / "dispatch-overrides.json"
        self.overrides: dict[str, dict[str, Any]] = {}
        self._load_pins()
        self._load_caps()
        self._load_overrides()

    def _load_pins(self) -> None:
        if not self.pin_path.exists():
            return
        try:
            data = json.loads(self.pin_path.read_text(encoding="utf-8"))
        except Exception:
            return
        pins: dict[str, dict[str, str]] = {}
        for k, v in (data.get("pins") or {}).items():
            if isinstance(v, str):  # older digest-only form
                pins[k] = {"result_sha256": v}
            elif isinstance(v, dict) and isinstance(v.get("result_sha256"), str):
                pins[k] = {kk: vv for kk, vv in v.items() if kk in ("result_sha256", "body_sha256") and isinstance(vv, str)}
        self.pins = pins
        # A legacy "bodies" key held raw response bytes (patches). Ignore it and
        # rewrite the file digest-only right away.
        if "bodies" in data:
            self._save_pins()

    def _save_pins(self) -> None:
        payload = {"pins": self.pins}
        write_private(self.pin_path, json.dumps(payload, indent=1).encode("utf-8"))

    def _load_overrides(self) -> None:
        if not self.override_path.exists():
            return
        try:
            self.overrides = dict(json.loads(self.override_path.read_text(encoding="utf-8")).get("overrides") or {})
        except Exception:
            self.overrides = {}

    def _save_overrides(self) -> None:
        write_private(self.override_path, json.dumps({"overrides": self.overrides}, indent=1).encode("utf-8"))

    def _gate_project(self, project: Any, owner: str, attempt_id: str, *, generation: Any, override: Any,
                      override_reason: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """workflow.project-state/v1 gate (docs/18). Raises DispatchRefused; never cancels anything."""
        if not isinstance(project, str) or project not in self.project_roots:
            raise DispatchRefused(403, "project_not_authorized", "project is not authorized on this host")
        rec = check_dispatch(self.project_roots[project], project, generation=generation,
                             override=override, override_reason=override_reason)
        ov = None
        if "override_reason" in rec:
            ov = {"reason": rec.pop("override_reason"), "by": owner,
                  "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
                  "project": project, "state": rec["state"], "generation": rec["generation"],
                  "state_at": rec["at"], "state_by": rec["by"], "state_reason": rec["reason"]}
        return rec, ov

    # --- principals and capabilities -------------------------------------
    def _load_caps(self) -> None:
        if not self.cap_path.exists():
            return
        try:
            self.caps = dict(json.loads(self.cap_path.read_text(encoding="utf-8")).get("capabilities") or {})
        except Exception:
            self.caps = {}

    def _save_caps(self) -> None:
        write_private(self.cap_path, json.dumps({"capabilities": self.caps}, indent=1).encode("utf-8"))

    def authenticate(self, bearer: str | None) -> str:
        """Return the principal name for a coordinator key, or raise unauthorized."""
        if not bearer:
            raise ArtifactError("unauthorized", "missing or incorrect bearer token")
        h = _sha256_hex(bearer)
        found = None
        for name, ph in self._principal_hashes.items():  # check every entry; no early exit
            if hmac.compare_digest(h, ph):
                found = name
        if found is None:
            raise ArtifactError("unauthorized", "missing or incorrect bearer token")
        return found

    def _issue_capability(self, attempt_id: str) -> str:
        cap = CAPABILITY_PREFIX + secrets.token_urlsafe(32)
        expires = (self.clock() + self.capability_ttl_s) if self.capability_ttl_s else None
        self.caps[attempt_id] = {"sha256": _sha256_hex(cap), "expires_at": expires, "revoked": False,
                                 "scope": list(CAPABILITY_SCOPE)}
        self._save_caps()
        return cap

    def create_attempt(self, attempt_id: str, *, bearer: str | None, state: str = "queued",
                       source_rev: str | None = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        """POST /v1/attempts shape: the creator is the authenticated principal; the capability is returned once."""
        try:
            owner = self.authenticate(bearer)
            self._validate_id(attempt_id)
        except ArtifactError as exc:
            return exc.http, exc.body()
        kwargs.pop("coordinator", None)  # deprecated request field; never used for auth or attribution
        project = kwargs.pop("project", None)
        generation = kwargs.pop("project_state_generation", None)
        override = kwargs.pop("project_parked_override", False)
        override_reason = kwargs.pop("project_parked_override_reason", None)
        if attempt_id in self.attempts:
            a = self.attempts[attempt_id]
            if a["coordinator"] != owner:
                return 403, ArtifactError("not_owner").body()
            return 200, {"attempt_id": attempt_id, "state": a["state"]}  # idempotent re-submit: no capability again
        gate = ov = None
        if project is not None:
            try:
                gate, ov = self._gate_project(project, owner, attempt_id, generation=generation,
                                              override=override, override_reason=override_reason)
            except DispatchRefused as exc:
                return exc.http, exc.body()
            if ov is not None:
                self.overrides[attempt_id] = ov
                self._save_overrides()  # persisted before the attempt exists
        cap = self.add_attempt(attempt_id, coordinator=owner, state=state, source_rev=source_rev, **kwargs)
        if gate is not None:
            self.attempts[attempt_id]["project"] = project
            self.attempts[attempt_id]["project_state"] = gate
            if ov is not None:
                self.attempts[attempt_id]["project_parked_override"] = ov
        out = {"attempt_id": attempt_id, "state": state, "artifact_capability": cap}
        exp = self.caps[attempt_id]["expires_at"]
        if exp:
            out["artifact_capability_expires_at"] = exp
        return 201, out

    def revoke_capability(self, attempt_id: str, *, bearer: str | None) -> tuple[int, dict[str, Any]]:
        try:
            owner = self.authenticate(bearer)
            a = self.attempts.get(attempt_id)
            if a is None:
                raise ArtifactError("unknown_attempt", "no such attempt")
            if a["coordinator"] != owner:
                raise ArtifactError("not_owner", "caller is not the attempt coordinator")
        except ArtifactError as exc:
            return exc.http, exc.body()
        if attempt_id in self.caps:
            self.caps[attempt_id]["revoked"] = True
            self._save_caps()
        return 200, {"attempt_id": attempt_id, "revoked": True}

    def _check_capability(self, attempt_id: str, capability: str | None, scope: str) -> None:
        if not capability:
            raise ArtifactError("capability_required", "missing attempt capability")
        rec = self.caps.get(attempt_id)
        expected = rec["sha256"] if rec else _sha256_hex("no-capability:" + attempt_id)
        ok = hmac.compare_digest(_sha256_hex(capability), expected)
        if not rec or not ok or scope not in (rec.get("scope") or []):
            raise ArtifactError("capability_invalid", "capability is not valid for this attempt")
        if rec.get("revoked"):
            raise ArtifactError("capability_revoked", "capability was revoked")
        exp = rec.get("expires_at")
        if exp is not None and self.clock() >= exp:
            raise ArtifactError("capability_expired", "capability has expired")

    def add_attempt(
        self,
        attempt_id: str,
        *,
        coordinator: str,
        state: str,
        source_rev: str | None,
        result: dict[str, Any] | None = None,
        result_bytes: bytes | None = None,
        symlink: bool = False,
        mode: int = 0o600,
    ) -> str | None:
        """Seed an attempt owned by principal ``coordinator``. Returns its new capability,
        or None when one was already issued (for example after a restart)."""
        self.attempts[attempt_id] = {
            "attempt_id": attempt_id,
            "coordinator": coordinator,
            "state": state,
            "source_rev": source_rev,
            "brief": {"source_rev": source_rev},
        }
        cap = None if attempt_id in self.caps else self._issue_capability(attempt_id)
        if result is None and result_bytes is None and not symlink:
            return cap
        path = self.results / f"{attempt_id}.result.json"
        if symlink:
            path.symlink_to("/tmp/hyvv-does-not-exist")
            return cap
        data = result_bytes if result_bytes is not None else json.dumps(result, indent=1).encode("utf-8")
        self._write_result(path, data, mode=mode)
        return cap

    def _write_result(self, path: Path, data: bytes, mode: int = 0o600) -> None:
        if mode == 0o600:
            write_private(path, data)
            return
        # Test seam only: deliberately non-private files (never used by the host).
        path.write_bytes(data)
        os.chmod(path, mode)

    def list_attempts(self) -> list[dict[str, Any]]:
        rows = []
        for a in self.attempts.values():
            rows.append({
                "attempt_id": a["attempt_id"],
                "state": a["state"],
                "result_available": (self.results / f"{a['attempt_id']}.result.json").exists(),
                "log_available": False,
            })
        return rows

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        a = self.attempts.get(attempt_id)
        if not a:
            return None
        return {
            "attempt_id": a["attempt_id"],
            "state": a["state"],
            "result_available": (self.results / f"{attempt_id}.result.json").exists(),
            "log_available": False,
        }

    def log_lines(self) -> list[str]:
        return list(self.logs)

    def get_artifact(
        self,
        attempt_id: str,
        *,
        bearer: str | None,
        capability: str | None,
        peer: str = "127.0.0.1",
    ) -> tuple[int, bytes]:
        """Return (http_status, body_bytes). Successful bodies are byte-identical on retry.

        bearer is the caller's coordinator key; capability is the attempt's scoped
        capability (header X-Hyvv-Capability). There is no caller-supplied coordinator."""
        try:
            return 200, self._get(attempt_id, bearer=bearer, capability=capability, peer=peer)
        except ArtifactError as exc:
            return exc.http, _canonical(exc.body())

    def _validate_id(self, attempt_id: str) -> None:
        if not attempt_id or UNSAFE_ID_RE.search(attempt_id) or not ATTEMPT_ID_RE.fullmatch(attempt_id):
            raise ArtifactError("invalid_attempt_id", "malformed or traversing attempt_id")

    def _safe_open(self, path: Path) -> bytes:
        results_real = self.results.resolve()
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise ArtifactError("result_missing", "result is gone and the host no longer has it")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise ArtifactError("artifact_unsafe", "symlink or non-regular result file")
        real = path.resolve()
        if results_real not in real.parents and real.parent != results_real:
            raise ArtifactError("artifact_unsafe", "result path escaped the results directory")
        if st.st_size > self.max_bytes:
            raise ArtifactError("artifact_oversized", f"result exceeds {self.max_bytes} bytes")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ArtifactError("artifact_unsafe", "could not open result without following links") from exc
        try:
            data = os.read(fd, self.max_bytes + 1)
        finally:
            os.close(fd)
        if len(data) > self.max_bytes:
            raise ArtifactError("artifact_oversized", f"result exceeds {self.max_bytes} bytes")
        return data

    def _build_payload(self, attempt: dict[str, Any], raw: bytes) -> dict[str, Any]:
        result_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ArtifactError("artifact_malformed", "not a valid result manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != "orbit.local-worker.result/v1":
            raise ArtifactError("artifact_malformed", "not a valid orbit.local-worker.result/v1 manifest")
        if manifest.get("attempt_id") not in (None, attempt["attempt_id"]):
            raise ArtifactError("artifact_mismatch", "result belongs to another attempt")
        brief_rev = attempt.get("source_rev")
        man_rev = None
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        man_rev = source.get("repo_rev") or source.get("source_rev")
        if man_rev is not None and brief_rev is not None and man_rev != brief_rev:
            raise ArtifactError("artifact_mismatch", "result source_rev does not match the brief")

        patch_obj = manifest.get("patch")
        patch = None
        if isinstance(patch_obj, str):
            patch = patch_obj
        elif isinstance(patch_obj, dict):
            patch = patch_obj.get("diff")
            if patch is None and "text" in patch_obj:
                patch = patch_obj.get("text")
        patch_sha256 = hashlib.sha256(patch.encode("utf-8")).hexdigest() if isinstance(patch, str) else None

        observed_in = manifest.get("observed") if isinstance(manifest.get("observed"), dict) else {}
        files_in = observed_in.get("files") or (patch_obj.get("files") if isinstance(patch_obj, dict) else None) or []
        files = []
        for item in files_in:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or path.startswith("/") or ":" in path.split("/", 1)[0]:
                # Drop absolute or host paths rather than echo them.
                continue
            files.append({
                "path": path,
                "sha256": item.get("sha256"),
                "change": item.get("change"),
                "size": item.get("size"),
                "in_assigned_scope": item.get("in_assigned_scope"),
            })
        in_scope = [f for f in files if f.get("in_assigned_scope") is not None]
        has_scope = bool(in_scope)
        all_in = all(bool(f.get("in_assigned_scope")) for f in in_scope) if has_scope else True
        scope_flags = {
            "has_assigned_scope": has_scope,
            "all_in_scope": all_in,
            "scope_violation": has_scope and not all_in,
        }
        usage = manifest.get("usage")
        if usage is None and isinstance(observed_in.get("usage"), dict):
            # Legacy launcher usage blob is not a bee.usage/v1 record; treat as unknown.
            usage = None
        if usage is not None and not (isinstance(usage, dict) and usage.get("schema_version") == "bee.usage/v1"):
            usage = None
        usage_status = "unknown" if usage is None else usage.get("usage_status") or "unknown"

        claims = manifest.get("worker_claims")
        if claims is None:
            claims = manifest.get("worker_claims_untrusted")

        return {
            "schema": SCHEMA,
            "attempt_id": attempt["attempt_id"],
            "state": attempt["state"],
            "source_rev": brief_rev,
            "patch": patch,
            "patch_sha256": patch_sha256,
            "result_sha256": result_sha256,
            "observed": {
                "base_rev": observed_in.get("local_base_rev") or observed_in.get("base_rev"),
                "tool_calls": observed_in.get("tool_calls"),
                "stop_reason": observed_in.get("stop_reason"),
                "files": files,
                "scope_flags": scope_flags,
            },
            "usage": usage,
            "usage_status": usage_status,
            "worker_claims_untrusted": claims,
        }

    def _get(self, attempt_id: str, *, bearer: str | None, capability: str | None, peer: str) -> bytes:
        if self.disconnected:
            # A disconnected host is 503 only after the caller authenticates.
            self.authenticate(bearer)
            raise ArtifactError("worker_disconnected", "worker host is disconnected")
        self._validate_id(attempt_id)
        principal = self.authenticate(bearer)
        attempt = self.attempts.get(attempt_id)
        if attempt is None:
            raise ArtifactError("unknown_attempt", "no such attempt")
        if principal != attempt.get("coordinator"):
            raise ArtifactError("not_owner", "caller is not the attempt coordinator")
        self._check_capability(attempt_id, capability, "artifact")
        state = attempt["state"]
        path = self.results / f"{attempt_id}.result.json"
        present = path.is_symlink() or path.exists()
        if state in PENDING_STATES and not present:
            raise ArtifactError("result_pending", "attempt is still running")
        if state in TERMINAL_WITHOUT_RESULT and not present:
            raise ArtifactError("no_result_for_state", f"ended without a result (state {state})")
        if not present:
            raise ArtifactError("result_missing", "result is gone and the host no longer has it")
        raw = self._safe_open(path)
        payload = self._build_payload(attempt, raw)
        result_sha256 = payload["result_sha256"]
        pinned = self.pins.get(attempt_id)
        if pinned and pinned.get("result_sha256") != result_sha256:
            raise ArtifactError("artifact_changed", "result changed since first read")
        body = self.bodies.get(attempt_id)
        if body is not None:
            return body
        body = _canonical(payload)
        body_sha256 = hashlib.sha256(body).hexdigest()
        if pinned and pinned.get("body_sha256") not in (None, body_sha256):
            raise ArtifactError("artifact_changed", "artifact bytes changed since first read")
        self.bodies[attempt_id] = body
        if pinned != {"result_sha256": result_sha256, "body_sha256": body_sha256}:
            self.pins[attempt_id] = {"result_sha256": result_sha256, "body_sha256": body_sha256}
            self._save_pins()
        self.logs.append(f"artifact attempt_id={attempt_id} patch_sha256={payload['patch_sha256']} result_sha256={result_sha256}")
        return body


def usage_unknown_template(**overrides: Any) -> dict[str, Any]:
    """A bee.usage/v1 record with every key present and unknown counters left null."""
    rec = {
        "schema_version": "bee.usage/v1",
        "key": "attempt:att_deadbeefcafebabe",
        "attempt_id": "att_deadbeefcafebabe",
        "card_id": "unknown",
        "run_id": "unknown",
        "project": "unknown",
        "role": "work",
        "join_complete": False,
        "verify_of": None,
        "retry_of": None,
        "ts_start": None,
        "ts_end": None,
        "duration_s": None,
        "queue_s": None,
        "trace_id": None,
        "requested_model": None,
        "served_model": None,
        "served_model_source": None,
        "provider": "unknown",
        "route": "unknown",
        "route_source": None,
        "fallback": None,
        "fallback_reason": None,
        "http_429_count": None,
        "retry_count": None,
        "error_type": None,
        "stop_reason": None,
        "tool_calls": None,
        "usage_status": "unknown",
        "input_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "usage_raw": None,
        "cost_usd_list": None,
        "cost_status": "unknown",
        "billing_mode": "unknown",
        "price_table_version": None,
        "price_period": None,
        "go_allowance": None,
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "bee-work",
        "gen_ai.provider.name": None,
        "gen_ai.request.model": None,
        "gen_ai.response.model": None,
        "gen_ai.usage.input_tokens": None,
        "gen_ai.usage.cache_read.input_tokens": None,
        "gen_ai.usage.cache_write.input_tokens": None,
        "gen_ai.usage.output_tokens": None,
        "gen_ai.usage.reasoning.output_tokens": None,
        "launcher": "0.1.15",
        "harness": "opencode",
    }
    rec.update(overrides)
    return rec
