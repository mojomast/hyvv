"""HTTP conformance for the implemented /v1 endpoints.

Two targets:

* Default: an in-process hyvv (``launcher.py`` with its scripted ``FakeBackend``)
  bound to 127.0.0.1 on a free port, with a throwaway token. No sandboxes, no
  network beyond loopback, no provider keys.
* Live: set ``HYVV_BASE_URL`` (for example ``http://127.0.0.1:47821``) plus the
  bearer token in ``HYVV_TOKEN_FILE`` (a path) or ``HYVV_TOKEN``. Live runs are
  read-only by default: they never create, review or cancel attempts and never
  change config. Set ``HYVV_CONTRACT_WRITES=1`` to also run the write cases
  against the live host (they create one small attempt).

The token is never printed. Run with ``uv run python -m contract`` (see
``contract/__main__.py``).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "contract" / "v1" / "schemas"

LIVE_URL = (os.environ.get("HYVV_BASE_URL") or "").strip().rstrip("/")
WRITES = (not LIVE_URL) or os.environ.get("HYVV_CONTRACT_WRITES") == "1"
TERMINAL = {"succeeded", "failed", "cancelled", "not_started"}
# Strings that must never appear in public /v1 responses.
LEAK_MARKERS = ("C:\\Users", "C:/Users", "/home/", "/workspace/", "orbit-api.token", "go.key", "deepseek.key")


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool,
          "object": dict, "array": list, "null": type(None)}


def _schema_errors(schema: dict, inst: dict) -> list[str]:
    """Tiny stdlib check: required keys, additionalProperties false, top-level types, const and enum."""
    errs = [f"missing {k}" for k in schema.get("required", []) if k not in inst]
    props = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        errs += [f"extra {k}" for k in inst if k not in props]
    for k, v in inst.items():
        p = props.get(k) or {}
        t = p.get("type")
        if t:
            ts = t if isinstance(t, list) else [t]
            ok = any(isinstance(v, _TYPES[x]) and not (x in ("number", "integer") and isinstance(v, bool)) for x in ts if x in _TYPES)
            if not ok:
                errs.append(f"type {k}")
        if "const" in p and v != p["const"]:
            errs.append(f"const {k}")
        if "enum" in p and v not in p["enum"]:
            errs.append(f"enum {k}")
    return errs


class _Target:
    """Where the suite sends requests. Holds the token privately; never prints it."""

    def __init__(self) -> None:
        self.server = None
        self.launcher = None
        self.tmp = None
        if LIVE_URL:
            self.base = LIVE_URL
            tf = os.environ.get("HYVV_TOKEN_FILE")
            tok = Path(tf).read_text(encoding="utf-8").strip() if tf else os.environ.get("HYVV_TOKEN", "")
            if not tok:
                raise unittest.SkipTest("HYVV_BASE_URL is set but neither HYVV_TOKEN_FILE nor HYVV_TOKEN is")
            self._token = tok
            self.mode = "live"
            return
        spec = importlib.util.spec_from_file_location("hyvv_launcher_contract", ROOT / "launcher.py")
        L = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(L)
        from http.server import ThreadingHTTPServer
        self.tmp = tempfile.mkdtemp(prefix="hyvv-contract-")
        for name in ("go.key", "deepseek.key"):
            (Path(self.tmp) / name).write_text("fake-key-not-real", encoding="utf-8")
        cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 2, "data_dir": self.tmp, "backoff_s": [0.2],
               "model": "deepseek-v4.1-flash", "base_url": "http://fake.invalid", "wire": "openai",
               "image": "cua-bee:contract", "runtime": "runc", "memory_mb": 1024,
               "projects": {"contract-demo": {}}}
        self.L = L
        self.launcher = L.Launcher(cfg, L.FakeBackend(cfg))
        threading.Thread(target=self.launcher.loop.run_forever, daemon=True).start()
        self._token = "contract-" + uuid.uuid4().hex + uuid.uuid4().hex
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(self.launcher, self._token))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.mode = "in-process"

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        # The launcher loop runs on a daemon thread and exits with the process
        # (stopping it mid-tick races the scheduler).

    def request(self, method: str, path: str, body=None, token: str | None = "__default__", raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"}
        tok = self._token if token == "__default__" else token
        if tok:
            headers["Authorization"] = "Bearer " + tok
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                status, text = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read()
        try:
            payload = json.loads(text or b"{}")
        except ValueError:
            payload = {"_non_json": True}
        return status, payload


T: _Target | None = None


def setUpModule() -> None:
    global T
    T = _Target()


def tearDownModule() -> None:
    if T:
        T.close()


class Auth(unittest.TestCase):
    def test_no_token_is_401(self):
        st, b = T.request("GET", "/v1/health", token=None)
        self.assertEqual(st, 401)
        self.assertIsInstance(b.get("error"), str)

    def test_wrong_token_is_401(self):
        st, _ = T.request("GET", "/v1/attempts", token="wrong-" + uuid.uuid4().hex)
        self.assertEqual(st, 401)

    def test_wrong_token_on_post_is_401(self):
        st, _ = T.request("POST", "/v1/attempts", body={"brief": {"objective": "x"}}, token="wrong-" + uuid.uuid4().hex)
        self.assertEqual(st, 401)


class ReadEndpoints(unittest.TestCase):
    def _no_leaks(self, payload):
        blob = json.dumps(payload)
        for m in LEAK_MARKERS:
            self.assertNotIn(m, blob, f"public response leaked {m!r}")

    def test_health_shape(self):
        st, b = T.request("GET", "/v1/health")
        self.assertEqual(st, 200)
        self.assertIs(b.get("ok"), True)
        self.assertIsInstance(b.get("version"), str)
        for k in ("max_concurrent", "active", "queued"):
            self.assertIsInstance(b.get(k), int, k)
        self.assertIn("uptime_s", b)
        self._no_leaks(b)

    def test_projects_are_names_only(self):
        st, b = T.request("GET", "/v1/projects")
        self.assertEqual(st, 200)
        self.assertIsInstance(b.get("projects"), list)
        for p in b["projects"]:
            self.assertEqual(set(p), {"name"})
        self._no_leaks(b)

    def test_containers_profile(self):
        st, b = T.request("GET", "/v1/containers")
        self.assertEqual(st, 200)
        self.assertIsInstance(b.get("containers"), list)
        for c in b["containers"]:
            self.assertLessEqual(set(c), {"image", "runtime", "memory_mb"})

    def test_attempt_listing_matches_schema(self):
        schema = _schema("attempt.json")
        st, b = T.request("GET", "/v1/attempts")
        self.assertEqual(st, 200)
        rows = b.get("attempts")
        self.assertIsInstance(rows, list)
        self.assertLessEqual(len(rows), 200)
        for row in rows:
            self.assertEqual(_schema_errors(schema, row), [], row.get("attempt_id"))
        self._no_leaks(b)

    def test_active_filter(self):
        st, b = T.request("GET", "/v1/attempts?active=1")
        self.assertEqual(st, 200)
        for row in b.get("attempts") or []:
            self.assertNotIn(row["state"], TERMINAL)

    def test_unknown_attempt_is_404(self):
        st, b = T.request("GET", "/v1/attempts/att_contract00000000")
        self.assertEqual(st, 404)
        self.assertIsInstance(b.get("error"), str)

    def test_unknown_route_is_404(self):
        st, _ = T.request("GET", "/v1/no-such-route")
        self.assertEqual(st, 404)

    def test_version_when_implemented(self):
        st, b = T.request("GET", "/v1/version")
        if st == 404:
            self.skipTest("GET /v1/version is planned, not implemented by this host")
        self.assertEqual(st, 200)
        self.assertEqual(_schema_errors(_schema("version.json"), b), [])


class RejectedWrites(unittest.TestCase):
    """Requests the host must refuse before anything is created. Safe against a live host."""

    def test_bad_json_is_400(self):
        st, _ = T.request("POST", "/v1/attempts", raw=b"{not json")
        self.assertEqual(st, 400)

    def test_missing_objective_is_400(self):
        st, b = T.request("POST", "/v1/attempts", body={"brief": {"objective": "  "}})
        self.assertEqual(st, 400)
        self.assertIn("objective", b.get("error", ""))

    def test_list_fields_must_be_string_lists(self):
        st, _ = T.request("POST", "/v1/attempts", body={"brief": {"objective": "x", "assigned_files": "a.py"}})
        self.assertEqual(st, 400)

    def test_unauthorized_project_is_403(self):
        st, _ = T.request("POST", "/v1/attempts", body={"brief": {"objective": "x", "project": "contract-not-authorized-" + uuid.uuid4().hex[:8]}})
        self.assertEqual(st, 403)

    def test_bad_attempt_id_is_400(self):
        st, _ = T.request("POST", "/v1/attempts", body={"attempt_id": "../x", "brief": {"objective": "x", "project": "contract-not-authorized"}})
        self.assertEqual(st, 400)

    def test_config_out_of_range_is_400(self):
        st, _ = T.request("POST", "/v1/config", body={"max_concurrent": 0})
        self.assertEqual(st, 400)
        st, _ = T.request("POST", "/v1/config", body={"max_concurrent": "lots"})
        self.assertEqual(st, 400)

    def test_review_needs_boolean(self):
        st, _ = T.request("POST", "/v1/attempts/att_contract00000000/review", body={"accepted": "yes"})
        self.assertEqual(st, 400)

    def test_cancel_unknown_is_404(self):
        st, _ = T.request("POST", "/v1/attempts/att_contract00000000/cancel", body={})
        self.assertEqual(st, 404)


@unittest.skipUnless(WRITES, "live host: write cases need HYVV_CONTRACT_WRITES=1")
class AttemptLifecycle(unittest.TestCase):
    """Create, idempotent re-submit, events, result, review. In-process uses the scripted fake backend."""

    def _wait(self, aid: str, limit: float = 30.0) -> dict:
        t0 = time.time()
        while True:
            st, a = T.request("GET", f"/v1/attempts/{aid}")
            self.assertEqual(st, 200)
            if a["state"] in TERMINAL or time.time() - t0 > limit:
                return a
            time.sleep(0.2)

    def test_create_events_result_review(self):
        schema = _schema("attempt.json")
        aid = "att_" + uuid.uuid4().hex[:16]
        brief = {"objective": "hyvv contract test: reply done and stop.", "task_id": "contract-lifecycle"}
        if T.mode == "in-process":
            brief["project"] = "contract-demo"
        st, a = T.request("POST", "/v1/attempts", body={"attempt_id": aid, "brief": brief})
        self.assertEqual(st, 201, a)
        self.assertEqual(a["attempt_id"], aid)
        self.assertEqual(_schema_errors(schema, a), [])
        st2, a2 = T.request("POST", "/v1/attempts", body={"attempt_id": aid, "brief": brief})
        self.assertEqual(st2, 200, "re-submitting the same attempt_id must be idempotent")
        self.assertEqual(a2["attempt_id"], aid)
        st, ev = T.request("GET", f"/v1/attempts/{aid}/events?cursor=0")
        self.assertEqual(st, 200)
        self.assertIsInstance(ev.get("events"), list)
        self.assertIn("cursor", ev)
        if T.mode != "in-process":
            return  # live: don't wait on or review a real bee
        done = self._wait(aid)
        self.assertEqual(done["state"], "succeeded", done)
        st, res = T.request("GET", f"/v1/attempts/{aid}/result")
        self.assertEqual(st, 200)
        self.assertEqual(res.get("attempt_id"), aid)
        st, rv = T.request("POST", f"/v1/attempts/{aid}/review", body={"accepted": True, "note": "contract"})
        self.assertEqual(st, 200)
        self.assertIs(rv.get("accepted"), True)
        st, rows = T.request("GET", "/v1/attempts")
        row = next(r for r in rows["attempts"] if r["attempt_id"] == aid)
        self.assertIs(row.get("reviewed"), True)
        self.assertEqual(row.get("task_id"), "contract-lifecycle")

    def test_minted_attempt_id(self):
        if T.mode != "in-process":
            self.skipTest("in-process only")
        st, a = T.request("POST", "/v1/attempts", body={"brief": {"objective": "mint an id", "project": "contract-demo"}})
        self.assertEqual(st, 201)
        self.assertRegex(a["attempt_id"], r"^att_[0-9a-f]{16}$")

    def test_review_unfinished_is_409(self):
        if T.mode != "in-process":
            self.skipTest("in-process only")
        aid = "att_" + uuid.uuid4().hex[:16]
        brief = {"objective": "slow", "project": "contract-demo", "fake": {"delay": 1}}
        st, _ = T.request("POST", "/v1/attempts", body={"attempt_id": aid, "brief": brief})
        self.assertEqual(st, 201)
        st, _ = T.request("POST", f"/v1/attempts/{aid}/review", body={"accepted": True})
        self.assertEqual(st, 409)
        # Not cancelled here: v0.1.15 can race two journal writes of the same attempt
        # (shared .tmp name) when a cancel lands mid-tick. Tracked as a follow-up.
        self.assertEqual(self._wait(aid)["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
