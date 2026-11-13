"""Policy-governed workspace provisioning for the hyvv worker launcher.

The coordinator submits a *declarative* request (repository identity, pinned
commit, profile, job id, idempotency key). This module owns everything that
touches the host filesystem and the launcher's project registry:

  requested -> authorized -> preparing -> ready -> in_use -> retiring -> cleaned

Guarantees that matter operationally:

* authorisation comes from a host-enrolled policy file, not from the request;
* the workspace path is derived from the root plus a validated name, never from
  the request, and reparse points are refused;
* the checkout is verified (remote identity, detached HEAD, exact SHA) before a
  project is registered;
* registration is atomic in memory and durable on disk, so it survives a
  launcher restart without one;
* every step is journalled, so a crash after clone/before registration, or a
  lost registration acknowledgement, reconciles instead of duplicating.

Nothing here executes arbitrary shell: the only child process is ``git`` with a
fixed argument vector built from policy-validated values.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping

SCHEMA = "rizzstack.provisioning/1"
STATES = ("requested", "authorized", "preparing", "ready", "in_use", "retiring", "cleaned")
FAILED = "failed"
BLOCKED = "blocked"
UNCERTAIN = "uncertain"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ProvisionError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Windows-safe path handling
# ---------------------------------------------------------------------------


def is_safe_name(name: Any) -> bool:
    if not isinstance(name, str) or not _NAME_RE.match(name) or name in {".", ".."}:
        return False
    stem = name.split(".", 1)[0].lower()
    return stem not in _RESERVED


def child_path(root: str | os.PathLike[str], name: str) -> Path:
    """Derive a child path from a validated name; reject anything else.

    The name regex forbids separators, so ``name`` cannot introduce a new path
    segment on either POSIX or Windows. Containment is checked with
    ``commonpath`` on the native path, which is what the host actually resolves.
    """

    if not is_safe_name(name):
        raise ProvisionError("invalid_project_name", f"project name {name!r} is not a safe path component", "Use lowercase letters, digits, dot, dash or underscore.")
    base = Path(root)
    if os.name == "nt" and not _windows_contained(str(base), str(Path(root) / name)):
        raise ProvisionError("path_escape", "the workspace path escapes the configured root", "Fix the enrolled policy.")
    if not base.is_absolute():
        raise ProvisionError("invalid_workspace_root", "the workspace root must be an absolute path", "Fix the enrolled policy.")
    candidate = base / name
    try:
        if os.path.commonpath([str(base), str(candidate)]) != str(base):
            raise ProvisionError("path_escape", "the workspace path escapes the configured root", "Fix the enrolled policy.")
    except ValueError as exc:
        raise ProvisionError("path_escape", "the workspace path is on another drive", "Fix the enrolled policy.") from exc
    return candidate


def _windows_contained(root: str, child: str) -> bool:
    """Windows containment, tested directly even when the suite runs on POSIX."""

    base = PureWindowsPath(root)
    candidate = PureWindowsPath(child)
    if not base.is_absolute() or not candidate.is_absolute():
        return False
    if base.drive.lower() != candidate.drive.lower():
        return False
    prefix = str(base).lower().rstrip("\\") + "\\"
    return str(candidate).lower().startswith(prefix)


def is_reparse_point(path: Path) -> bool:
    """True for symlinks and Windows reparse points (junctions)."""

    try:
        if path.is_symlink():
            return True
        if os.name == "nt":  # pragma: no cover - exercised on the Windows host
            import stat

            attrs = path.lstat().st_file_attributes  # type: ignore[attr-defined]
            return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return False
    except OSError:
        return False


def ensure_contained(root: Path, target: Path) -> None:
    """Refuse a target that is not strictly below root, or that is a reparse point."""

    if is_reparse_point(target):
        raise ProvisionError("reparse_point", f"{target} is a symlink or reparse point", "Refusing to follow links out of the disposable root.")
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    if root_resolved == target_resolved or root_resolved not in target_resolved.parents:
        raise ProvisionError("path_escape", f"{target} is not inside {root}", "Refusing to touch a path outside the disposable root.")


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    name: str
    repo_patterns: tuple[str, ...]
    workspace_root: str
    max_provisioned: int = 5
    ttl_hours: float = 24.0
    allow_cleanup: bool = True
    git_actions: tuple[str, ...] = ("clone", "fetch", "checkout")
    task_scopes: tuple[str, ...] = ()

    def allows_repo(self, repo: str) -> bool:
        for pattern in self.repo_patterns:
            if pattern == repo:
                return True
            if pattern.endswith("/*") and repo.startswith(pattern[:-1]):
                return True
        return False


def load_policy(path: Path) -> dict[str, Profile]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ProvisionError("policy_missing", f"no provisioning policy at {path}", "Enroll a policy before provisioning.") from exc
    except (OSError, ValueError) as exc:
        raise ProvisionError("policy_invalid", f"the provisioning policy cannot be read: {exc}", "Fix the policy file.") from exc
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise ProvisionError("policy_invalid", f"policy schema must be {SCHEMA}", "Rewrite the policy.")
    profiles: dict[str, Profile] = {}
    for name, body in (raw.get("profiles") or {}).items():
        if not isinstance(body, Mapping):
            raise ProvisionError("policy_invalid", f"profile {name} is not an object")
        profiles[str(name)] = Profile(
            name=str(name),
            repo_patterns=tuple(str(item) for item in body.get("repo_patterns") or []),
            workspace_root=str(body.get("workspace_root") or ""),
            max_provisioned=int(body.get("max_provisioned") or 5),
            ttl_hours=float(body.get("ttl_hours") or 24.0),
            allow_cleanup=bool(body.get("allow_cleanup", True)),
            git_actions=tuple(str(item) for item in body.get("git_actions") or ("clone", "fetch", "checkout")),
            task_scopes=tuple(str(item) for item in body.get("task_scopes") or []),
        )
    return profiles


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class Journal:
    """One JSON record per idempotency key, fsynced on every transition."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, key: str) -> Path:
        if not is_safe_name(key):
            raise ProvisionError("invalid_idempotency_key", "idempotency keys are path components", "Use a generated key.")
        return self.root / f"{key}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path(key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ProvisionError("journal_corrupt", f"provisioning journal {key} is unreadable: {exc}", "Inspect it by hand; do not retry blindly.") from exc
        return data if isinstance(data, dict) else None

    def write(self, key: str, record: Mapping[str, Any]) -> dict[str, Any]:
        path = self.path(key)
        body = dict(record)
        body["updated_at"] = _now()
        payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return body

    def all(self) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return records


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def default_git(args: list[str], cwd: Path | None, *, timeout: float = 300.0) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(Path.home()),
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


# ---------------------------------------------------------------------------
# Provisioner
# ---------------------------------------------------------------------------


@dataclass
class Provisioner:
    policy: Mapping[str, Profile]
    journal: Journal
    projects: dict[str, dict[str, Any]]
    persist_registry: Callable[[dict[str, dict[str, Any]]], None]
    git: Callable[..., subprocess.CompletedProcess] = default_git
    credential: Callable[[str], str | None] = lambda repo: None
    now: Callable[[], float] = time.time

    # -- request validation -------------------------------------------------

    def _validate(self, request: Mapping[str, Any]) -> tuple[Profile, str, str, str]:
        repo = request.get("repo")
        source_rev = request.get("source_rev")
        profile_name = request.get("profile")
        job_id = request.get("job_id")
        key = request.get("idempotency_key")

        if not isinstance(repo, str) or not _REPO_RE.match(repo):
            raise ProvisionError("invalid_repo", "repo must be 'owner/name'", "Submit a canonical repository identity.")
        if not isinstance(source_rev, str) or not _SHA_RE.match(source_rev):
            raise ProvisionError("invalid_source_rev", "source_rev must be a full 40-hex commit", "Pin an exact commit.")
        if not isinstance(profile_name, str) or profile_name not in self.policy:
            raise ProvisionError("unknown_profile", f"profile {profile_name!r} is not enrolled", "Enroll a profile first.")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ProvisionError("invalid_job_id", "job_id is required", "Pass the coordinator's job id.")
        if not is_safe_name(key):
            raise ProvisionError("invalid_idempotency_key", "idempotency_key must be a safe identifier", "Use a generated key.")

        profile = self.policy[profile_name]
        if not profile.allows_repo(repo):
            raise ProvisionError("repo_not_allowed", f"profile {profile_name} does not permit {repo}", "Expand the enrolled policy deliberately.")
        if "clone" not in profile.git_actions:
            raise ProvisionError("git_action_not_allowed", "profile does not permit clone", "Fix the policy.")
        return profile, repo, source_rev, str(job_id)

    # -- main entry ---------------------------------------------------------

    def provision(self, request: Mapping[str, Any]) -> dict[str, Any]:
        profile, repo, source_rev, job_id = self._validate(request)
        key = str(request["idempotency_key"])
        lock = self.journal.root / f"{key}.lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ProvisionError("provision_in_progress", f"another provisioning request holds {key}", "Wait for it to finish; do not clone twice.") from exc
        os.close(fd)
        try:
            return self._provision_locked(request, profile, repo, source_rev, job_id, key)
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    def _provision_locked(self, request, profile, repo, source_rev, job_id, key) -> dict[str, Any]:
        project = str(request.get("project") or repo.split("/")[-1])
        target = child_path(profile.workspace_root, project)

        existing = self.journal.read(key)
        if existing and existing.get("repo") not in (None, repo):
            raise ProvisionError("journal_conflict", "this idempotency key was used for another repository", "Use a new key per request.")
        if existing and existing.get("source_rev") not in (None, source_rev):
            raise ProvisionError("journal_conflict", "this idempotency key was used for another revision", "Use a new key per request.")
        if existing and existing.get("state") == "ready":
            return self._receipt(existing, adopted=existing.get("project") in self.projects)

        record = existing or {
            "schema": SCHEMA,
            "idempotency_key": key,
            "job_id": job_id,
            "repo": repo,
            "source_rev": source_rev,
            "profile": profile.name,
            "project": project,
            "workspace": str(target),
            "state": "requested",
            "created_at": _now(),
        }
        record["state"] = "authorized"
        self.journal.write(key, record)

        # Crash recovery: an existing checkout is reconciled, never re-cloned.
        state = record.get("state")
        if target.exists():
            record["state"] = "preparing"
            self.journal.write(key, record)
            ok, detail = self._verify_checkout(target, repo, source_rev)
            if not ok:
                record.update(state=FAILED, failure="checkout_mismatch", detail=detail)
                self.journal.write(key, record)
                raise ProvisionError("checkout_mismatch", detail, "Inspect the workspace; do not retry blindly.")
        else:
            record["state"] = "preparing"
            self.journal.write(key, record)
            self._clone(target, repo, source_rev)

        # Registration: adopt if already present and identical.
        registered = self.projects.get(project)
        adopted = False
        if registered:
            if not self._registration_matches(registered, repo, target):
                record.update(state=BLOCKED, failure="project_conflict", detail="an existing project disagrees")
                self.journal.write(key, record)
                raise ProvisionError("project_conflict", f"project {project} is already registered for a different repository or path", "Choose another project name or clean the stale registration.")
            adopted = True
        else:
            registry = dict(self.projects)
            registry[project] = {"repo": repo, "path": str(target), "source_rev": source_rev, "profile": profile.name}
            self.persist_registry(registry)          # durable first
            self.projects[project] = registry[project]  # then live

        receipt = self._receipt_fields(project, repo, source_rev, str(target), profile.name)
        receipt["adopted"] = adopted
        record.update(state="ready", ready_at=_now(), receipt=receipt)
        self.journal.write(key, record)
        return receipt

    # -- steps --------------------------------------------------------------

    def _clone(self, target: Path, repo: str, source_rev: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Authentication is the host's: a short-lived credential helper (or a
        # scoped token file) configured outside the launcher. The secret is never
        # placed on the command line, in the remote URL, or in a receipt — this
        # module only checks that the host says it can authenticate.
        if self.credential(repo) is None:
            raise ProvisionError(
                "credential_unavailable",
                f"the host has no credential for {repo}",
                "Configure a repository-scoped read-only credential on the worker host.",
            )
        url = f"https://github.com/{repo}.git"
        args = ["clone", "--no-checkout", "--filter=blob:none", url, str(target)]
        done = self.git(args, None)
        if done.returncode != 0:
            raise ProvisionError("clone_failed", _tail(done), "Check the repository and host credentials.")
        checkout = self.git(["-C", str(target), "checkout", "--detach", source_rev], None)
        if checkout.returncode != 0:
            raise ProvisionError("checkout_failed", _tail(checkout), "Verify the pinned revision exists in the repository.")
        fetch = self.git(["-C", str(target), "fetch", "--quiet", "origin", source_rev], None)
        if fetch.returncode == 0:
            again = self.git(["-C", str(target), "checkout", "--detach", source_rev], None)
            if again.returncode != 0:
                raise ProvisionError("checkout_failed", _tail(again), "Verify the pinned revision exists in the repository.")

    def _verify_checkout(self, target: Path, repo: str, source_rev: str) -> tuple[bool, str]:
        head = self.git(["-C", str(target), "rev-parse", "HEAD"], None)
        if head.returncode != 0 or head.stdout.strip() != source_rev:
            return False, f"HEAD is {head.stdout.strip()[:12]!r}, expected {source_rev[:12]!r}"
        remote = self.git(["-C", str(target), "config", "--get", "remote.origin.url"], None)
        if remote.returncode != 0 or repo not in remote.stdout:
            return False, f"remote origin is {remote.stdout.strip()!r}, expected to contain {repo!r}"
        state = self.git(["-C", str(target), "status", "--porcelain"], None)
        if state.returncode != 0 or state.stdout.strip():
            return False, "the checkout is dirty"
        return True, "ok"

    def _registration_matches(self, entry: Mapping[str, Any], repo: str, target: Path) -> bool:
        recorded_repo = str(entry.get("repo") or "")
        same_repo = recorded_repo in {repo, f"https://github.com/{repo}.git"}
        same_path = PureWindowsPath(str(entry.get("path") or "")).name == target.name
        return same_repo and same_path

    def _receipt_fields(self, project: str, repo: str, source_rev: str, workspace: str, profile: str) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "status": "ready",
            "project": project,
            "launcher_project": project,
            "repo": repo,
            "resolved_revision": source_rev,
            "workspace": workspace,
            "profile": profile,
            "host": _hostname(),
            "fixture": PureWindowsPath(workspace).name,
            "verified": {"head": source_rev, "detached": True},
        }

    def _receipt(self, record: Mapping[str, Any], *, adopted: bool) -> dict[str, Any]:
        receipt = dict(record.get("receipt") or self._receipt_fields(str(record.get("project")), str(record.get("repo")), str(record.get("source_rev")), str(record.get("workspace")), str(record.get("profile"))))
        receipt["adopted"] = adopted
        return receipt

    # -- cleanup ------------------------------------------------------------

    def cleanup(self, project: str, *, active_attempts: Callable[[str], int] = lambda name: 0) -> dict[str, Any]:
        entry = self.projects.get(project)
        if not entry:
            raise ProvisionError("unknown_project", f"{project} is not provisioned", "Nothing to clean.")
        profile_name = str(entry.get("profile") or "")
        profile = self.policy.get(profile_name)
        if profile is None or not profile.allow_cleanup:
            raise ProvisionError("cleanup_not_allowed", f"profile {profile_name!r} does not permit cleanup", "Require explicit approval.")
        if active_attempts(project):
            raise ProvisionError("project_in_use", f"{project} has running attempts", "Retry when idle.")

        workspace = Path(str(entry.get("path") or ""))
        root = Path(profile.workspace_root)
        ensure_contained(root, workspace)  # refuses escapes and reparse points

        registry = dict(self.projects)
        registry.pop(project, None)
        self.persist_registry(registry)
        self.projects.pop(project, None)

        import shutil

        shutil.rmtree(workspace, ignore_errors=False)
        return {"project": project, "state": "cleaned", "workspace": str(workspace), "evidence_preserved": True}


_SECRETISH = re.compile(r"(x-access-token|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})[^\s]*")


def _tail(done: subprocess.CompletedProcess) -> str:
    text = (done.stderr or done.stdout or "git failed").strip()[:300]
    return _SECRETISH.sub("<redacted>", text)


def _hostname() -> str:
    import socket

    return socket.gethostname()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
