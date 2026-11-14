"""Policy-governed workspace provisioning for the hyvv worker launcher.

The coordinator submits a *declarative* request (repository identity, pinned
commit, profile, job id, idempotency key). This module owns everything that
touches the host filesystem and the launcher's project registry.

Lifecycle: requested -> authorized -> preparing -> ready -> in_use -> retiring
-> cleaned, plus blocked / failed / uncertain for outcomes a human must inspect.

Design rules enforced here:

* authorisation comes from a host-enrolled policy file, never the request;
* paths are derived from the root plus a validated name, and reparse points are
  refused before any Git or filesystem operation;
* a checkout is verified (remote, detached HEAD, exact SHA, clean) before any
  project is registered — for fresh clones and recovered workspaces alike;
* the idempotency key binds the whole immutable request;
* registration goes through a file-locked read-modify-write store, so concurrent
  provisioning cannot lose entries;
* cleanup is journalled and recoverable, and a cleaned project is never
  rediscovered as ready.

The only child process is ``git`` with a fixed argument vector built from
policy-validated values. No shell, no arbitrary command execution.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterator, Mapping

SCHEMA = "rizzstack.provisioning/1"
STATES = ("requested", "authorized", "preparing", "ready", "in_use", "retiring", "cleaned")
ACTIVE_STATES = {"authorized", "preparing", "ready", "in_use"}
FAILED, BLOCKED, UNCERTAIN = "failed", "blocked", "uncertain"

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
# Path handling (Windows-aware; tested on POSIX through pure-path helpers)
# ---------------------------------------------------------------------------


def is_safe_name(name: Any) -> bool:
    if not isinstance(name, str) or not _NAME_RE.match(name) or name in {".", ".."}:
        return False
    return name.split(".", 1)[0].lower() not in _RESERVED


def _windows_contained(root: str, child: str) -> bool:
    base, candidate = PureWindowsPath(root), PureWindowsPath(child)
    if not base.is_absolute() or not candidate.is_absolute():
        return False
    if base.drive.lower() != candidate.drive.lower():
        return False
    return str(candidate).lower().startswith(str(base).lower().rstrip("\\") + "\\")


def child_path(root: str | os.PathLike[str], name: str) -> Path:
    if not is_safe_name(name):
        raise ProvisionError("invalid_project_name", f"project name {name!r} is not a safe path component", "Use lowercase letters, digits, dot, dash or underscore.")
    base = Path(root)
    if not base.is_absolute():
        raise ProvisionError("invalid_workspace_root", "the workspace root must be an absolute path", "Fix the enrolled policy.")
    if os.name == "nt" and not _windows_contained(str(base), str(base / name)):
        raise ProvisionError("path_escape", "the workspace path escapes the configured root", "Fix the enrolled policy.")
    try:
        if os.path.commonpath([str(base), str(base / name)]) != str(base):
            raise ProvisionError("path_escape", "the workspace path escapes the configured root", "Fix the enrolled policy.")
    except ValueError as exc:
        raise ProvisionError("path_escape", "the workspace path is on another drive", "Fix the enrolled policy.") from exc
    return base / name


def is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":  # pragma: no cover - executed on the Windows host
            import stat

            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return False
    except OSError:
        return False


def ensure_contained(root: Path, target: Path) -> None:
    if is_reparse_point(target):
        raise ProvisionError("reparse_point", f"{target} is a symlink or reparse point", "Refusing to follow links out of the disposable root.")
    root_resolved, target_resolved = root.resolve(), target.resolve()
    if root_resolved == target_resolved or root_resolved not in target_resolved.parents:
        raise ProvisionError("path_escape", f"{target} is not inside {root}", "Refusing to touch a path outside the disposable root.")


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """Full canonical comparison, not a basename comparison."""

    def canon(value: str | os.PathLike[str]) -> str:
        text = os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))
        return os.path.normpath(text.rstrip("\\/"))

    try:
        return canon(a) == canon(b)
    except (OSError, ValueError):
        return False


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
    mirrors: tuple[tuple[str, str], ...] = ()

    def mirror_for(self, repo: str) -> str | None:
        for name, url in self.mirrors:
            if name == repo:
                return url
        return None

    def allows_repo(self, repo: str) -> bool:
        for pattern in self.repo_patterns:
            if pattern == repo:
                return True
            if pattern.endswith("*") and repo.startswith(pattern[:-1]):
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
            mirrors=tuple(sorted((str(k), str(v)) for k, v in (body.get("mirrors") or {}).items())),
        )
    return profiles


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class Journal:
    """One JSON record per idempotency key, fsynced and atomically replaced."""

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
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
        return body

    def all(self) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return records


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - directories are not fsync-able on Windows
        return
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Cross-process key lock with crash recovery
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - executed on Windows
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE; a reaped process keeps a stale object
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class KeyLock:
    """A per-key lock that survives a killed owner.

    The lock file records pid + host + timestamp. A lock whose owner is dead (or
    whose host is this one and whose pid is gone) is stolen with an atomic
    rename, so exactly one of several concurrent recovery processes wins and no
    operation is performed twice.
    """

    def __init__(self, root: Path, key: str, *, stale_after: float = 300.0) -> None:
        self.path = Path(root) / f"{key}.lock"
        self.stale_after = stale_after
        self.key = key
        self.acquired = False

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _owner_dead(self, record: Mapping[str, Any]) -> bool:
        if not record:
            return True
        if record.get("host") == socket.gethostname() and not pid_alive(int(record.get("pid") or 0)):
            return True
        age = time.time() - float(record.get("at") or 0)
        return age > self.stale_after

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "at": time.time()}).encode()
        for _ in range(50):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                record = self._read()
                if not self._owner_dead(record):
                    raise ProvisionError("provision_in_progress", f"another provisioning request holds {self.key}", "Wait for it to finish; do not clone twice.")
                stale = self.path.with_name(f"{self.path.name}.stale.{os.getpid()}")
                try:
                    os.replace(self.path, stale)   # exactly one stealer wins
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                with contextlib.suppress(OSError):
                    stale.unlink()
                continue
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return
        raise ProvisionError("provision_lock_contention", f"could not acquire the lock for {self.key}", "Retry after the current owner finishes.")

    def release(self) -> None:
        if not self.acquired:
            return
        record = self._read()
        if record.get("pid") == os.getpid() and record.get("host") == socket.gethostname():
            with contextlib.suppress(OSError):
                self.path.unlink()
        self.acquired = False

    def __enter__(self) -> "KeyLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Registry store: file-locked read-modify-write
# ---------------------------------------------------------------------------


class RegistryStore:
    """The launcher's project registry, persisted atomically and without lost updates.

    Every mutation re-reads the file under an exclusive ``.lock`` file, applies
    one change, writes a tmp file, fsyncs it, then replaces the target. Concurrent
    provisioning of *different* keys therefore cannot drop each other's entries,
    and a crash leaves either the old or the new file — never a partial one.
    """

    def __init__(self, config_path: Path, cache: dict[str, dict[str, Any]] | None = None) -> None:
        self.config_path = Path(config_path)
        self.cache = cache if cache is not None else self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        projects = raw.get("projects")
        return dict(projects) if isinstance(projects, dict) else {}

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        lock = self.config_path.with_name(self.config_path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = None
        for _ in range(400):
            try:
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if time.time() - lock.stat().st_mtime > 30:
                        lock.unlink()  # abandoned by a crashed writer
                        continue
                time.sleep(0.01)
        if fd is None:
            raise ProvisionError("registry_busy", "the project registry is locked by another writer", "Retry shortly.")
        try:
            yield
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock.unlink()

    def _write(self, projects: dict[str, dict[str, Any]]) -> None:
        raw = {}
        if self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            except ValueError as exc:
                raise ProvisionError("config_invalid", f"{self.config_path} is not valid JSON: {exc}", "Repair the config before provisioning.") from exc
        raw["projects"] = projects
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.config_path)
        _fsync_dir(self.config_path.parent)

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self.cache)

    def get(self, name: str) -> dict[str, Any] | None:
        return self.cache.get(name)

    def set(self, name: str, entry: dict[str, Any]) -> None:
        with self._locked():
            current = self._load()
            current[name] = entry
            self._write(current)
            self.cache.clear()
            self.cache.update(current)

    def delete(self, name: str) -> None:
        with self._locked():
            current = self._load()
            if name in current:
                del current[name]
                self._write(current)
            self.cache.clear()
            self.cache.update(current)


class MemoryRegistry(RegistryStore):
    """Tests and simulations: same interface, no file."""

    def __init__(self, seed: dict[str, dict[str, Any]] | None = None) -> None:
        self.cache = dict(seed or {})
        self.config_path = None  # type: ignore[assignment]

    def _load(self) -> dict[str, dict[str, Any]]:
        return dict(self.cache)

    def _write(self, projects: dict[str, dict[str, Any]]) -> None:
        self.cache = dict(projects)

    def set(self, name: str, entry: dict[str, Any]) -> None:
        self.cache[name] = dict(entry)

    def delete(self, name: str) -> None:
        self.cache.pop(name, None)


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
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, timeout=timeout, env=env)


_SECRETISH = re.compile(r"(x-access-token|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})[^\s]*")


def _tail(done: subprocess.CompletedProcess) -> str:
    return _SECRETISH.sub("<redacted>", (done.stderr or done.stdout or "git failed").strip()[:300])


# ---------------------------------------------------------------------------
# Provisioner
# ---------------------------------------------------------------------------


@dataclass
class Provisioner:
    policy: Mapping[str, Profile]
    journal: Journal
    registry: RegistryStore
    git: Callable[..., subprocess.CompletedProcess] = default_git
    credential: Callable[[str], str | None] = lambda repo: None
    url_for: Callable[[str], str] = lambda repo: f"https://github.com/{repo}.git"
    _remotes: dict[str, str] = field(default_factory=dict)
    _transport: dict[str, str] = field(default_factory=dict)
    now: Callable[[], float] = time.time
    lock_stale_after: float = 300.0

    # -- request validation -------------------------------------------------

    def _validate(self, request: Mapping[str, Any]) -> tuple[Profile, str, str, str, str]:
        repo, source_rev = request.get("repo"), request.get("source_rev")
        profile_name, job_id = request.get("profile"), request.get("job_id")
        key, project = request.get("idempotency_key"), request.get("project")

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
        if project is not None and not is_safe_name(project):
            raise ProvisionError("invalid_project_name", "project must be a safe path component", "Use a generated name.")

        profile = self.policy[profile_name]
        if not profile.allows_repo(repo):
            raise ProvisionError("repo_not_allowed", f"profile {profile_name} does not permit {repo}", "Expand the enrolled policy deliberately.")
        if "clone" not in profile.git_actions:
            raise ProvisionError("git_action_not_allowed", "profile does not permit clone", "Fix the policy.")
        return profile, repo, source_rev, str(job_id), str(project or repo.split("/")[-1])

    def _request_identity(self, request: Mapping[str, Any], repo: str, source_rev: str, job_id: str, project: str) -> dict[str, Any]:
        return {"repo": repo, "source_rev": source_rev, "profile": str(request["profile"]), "job_id": job_id, "project": project}

    # -- main entry ---------------------------------------------------------

    def provision(self, request: Mapping[str, Any]) -> dict[str, Any]:
        profile, repo, source_rev, job_id, project = self._validate(request)
        key = str(request["idempotency_key"])
        identity = self._request_identity(request, repo, source_rev, job_id, project)

        with KeyLock(self.journal.root, key, stale_after=self.lock_stale_after):
            existing = self.journal.read(key)
            if existing:
                self._check_idempotency(key, existing, identity)
                if existing.get("state") in ("retiring", "cleaned"):
                    raise ProvisionError("project_retired", f"{key} was cleaned; use a new key to provision again", "Request a new key.")
                if existing.get("state") == "ready":
                    return self._replay_ready(existing)

            self._enforce_quota(profile, key)
            trail = self._provision_steps(existing, identity, profile, key)
            return trail

    def _check_idempotency(self, key: str, existing: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
        for field_name, value in identity.items():
            if existing.get(field_name) not in (None, value):
                raise ProvisionError("journal_conflict", f"this idempotency key was used with a different {field_name}", f"Use a new key for a different request.")

    def _enforce_quota(self, profile: Profile, key: str) -> None:
        active = [r for r in self.journal.all()
                  if r.get("profile") == profile.name and r.get("state") in ACTIVE_STATES and r.get("idempotency_key") != key]
        if len(active) >= profile.max_provisioned:
            raise ProvisionError("quota_exhausted", f"profile {profile.name} already holds {len(active)} provisioned projects (max {profile.max_provisioned})", "Clean up an unused workspace or raise the enrolled limit deliberately.")

    def _provision_steps(self, existing: Mapping[str, Any] | None, identity: Mapping[str, Any], profile: Profile, key: str) -> dict[str, Any]:
        project = identity["project"]
        target = child_path(profile.workspace_root, project)
        record = dict(existing or {})
        record.update(identity)
        record.update({
            "schema": SCHEMA, "idempotency_key": key, "workspace": str(target),
            "state": "authorized" if not existing else record.get("state", "authorized"),
        })
        record.setdefault("created_at", _now())
        self.journal.write(key, record)

        # Reparse points and escapes are refused before any filesystem action.
        ensure_contained(Path(profile.workspace_root), target) if target.exists() else None
        if target.exists():
            record["state"] = "preparing"
            self.journal.write(key, record)
            self._verify_checkout(target, identity["repo"], identity["source_rev"])
        else:
            record["state"] = "preparing"
            self.journal.write(key, record)
            self._clone(target, identity["repo"], identity["source_rev"], profile)

        registered = self.registry.get(project)
        adopted = False
        if registered:
            if not self._registration_matches(registered, identity, target):
                record.update(state=BLOCKED, failure="project_conflict", detail="an existing project disagrees with this request")
                self.journal.write(key, record)
                raise ProvisionError("project_conflict", f"project {project} is already registered for a different repository, revision, profile or path", "Choose another project name or clean the stale registration.")
            adopted = True
        else:
            self.registry.set(project, {
                "repo": identity["repo"], "path": str(target), "source_rev": identity["source_rev"],
                "profile": profile.name, "job_id": identity["job_id"],
            })

        ready_at = self.now()
        receipt = self._receipt_fields(project, identity["repo"], identity["source_rev"], str(target), profile, adopted, ready_at)
        record.update(state="ready", ready_at=_iso(ready_at), receipt=receipt)
        self.journal.write(key, record)
        return receipt

    # -- replay -------------------------------------------------------------

    def _replay_ready(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """A ready record is only usable while it still matches reality."""

        key = str(record["idempotency_key"])
        project, repo, source_rev = str(record["project"]), str(record["repo"]), str(record["source_rev"])
        workspace = str(record.get("workspace") or "")
        profile = self.policy.get(str(record.get("profile") or ""))

        registered = self.registry.get(project)
        reasons = []
        if profile is None:
            reasons.append("the profile is no longer enrolled")
        if not workspace:
            reasons.append("the record has no workspace")
        if not registered:
            reasons.append("the project is not in the live registry")
        elif not self._registration_matches(registered, record, Path(workspace or ".")):
            reasons.append("the registry entry no longer matches the record")
        if workspace and not Path(workspace).exists():
            reasons.append("the workspace is missing")
        elif workspace and profile is not None:
            try:
                self._verify_checkout(Path(workspace), repo, source_rev)
            except ProvisionError as exc:
                reasons.append(exc.message)

        if reasons:
            record = dict(record, state=UNCERTAIN, failure="stale_receipt", detail="; ".join(reasons))
            self.journal.write(key, record)
            raise ProvisionError("stale_receipt", f"ready record {key} is stale: {'; '.join(reasons)}", "Re-provision with a new key after inspecting the workspace.")
        return dict(record["receipt"])

    # -- steps --------------------------------------------------------------

    def _clone(self, target: Path, repo: str, source_rev: str, profile: "Profile | None" = None) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if is_reparse_point(target.parent):
            raise ProvisionError("reparse_point", f"{target.parent} is a reparse point", "Refusing to clone through a link.")
        if self.credential(repo) is None:
            raise ProvisionError("credential_unavailable", f"the host has no credential for {repo}", "Configure a repository-scoped read-only credential on the worker host.")
        mirror = profile.mirror_for(repo) if profile is not None else None
        url = mirror or self.url_for(repo)
        self._remotes[str(target)] = url
        self._transport[str(target)] = "mirror" if mirror else "origin"
        done = self.git(["clone", "--no-checkout", "--filter=blob:none", url, str(target)], None)
        if done.returncode != 0:
            raise ProvisionError("clone_failed", _tail(done), "Check the repository and host credentials.")

        # The pinned commit may not be reachable from the branch tips the clone
        # fetched. For a governed mirror the accepted refs are the transport, so
        # fetch that namespace explicitly: servers need not serve raw SHAs.
        if mirror:
            fetched = self.git(["-C", str(target), "fetch", "origin",
                                "+refs/bb/accepted/*:refs/bb/accepted/*"], None)
        else:
            fetched = self.git(["-C", str(target), "fetch", "--depth=1", "origin", source_rev], None)
            if fetched.returncode != 0:
                fetched = self.git(["-C", str(target), "fetch", "origin", source_rev], None)
        if fetched.returncode != 0:
            raise ProvisionError("fetch_failed", _tail(fetched), "Verify the pinned revision exists in the repository.")
        checkout = self.git(["-C", str(target), "checkout", "--detach", source_rev], None)
        if checkout.returncode != 0:
            raise ProvisionError("checkout_failed", _tail(checkout), "Verify the pinned revision exists in the repository.")
        self._verify_checkout(target, repo, source_rev)

    def _verify_checkout(self, target: Path, repo: str, source_rev: str) -> None:
        ensure_contained(Path(self._root_for(target)), target)
        head = self.git(["-C", str(target), "rev-parse", "HEAD"], None)
        if head.returncode != 0 or head.stdout.strip() != source_rev:
            raise ProvisionError("checkout_mismatch", f"HEAD is {head.stdout.strip()[:12]!r}, expected {source_rev[:12]!r}", "Inspect the workspace; do not retry blindly.")
        symbolic = self.git(["-C", str(target), "symbolic-ref", "-q", "HEAD"], None)
        if symbolic.returncode == 0:
            raise ProvisionError("checkout_mismatch", "the checkout is on a branch, not a detached HEAD", "Re-checkout the pinned revision.")
        remote = self.git(["-C", str(target), "config", "--get", "remote.origin.url"], None)
        expected = self._remotes.get(str(target)) or self.url_for(repo)
        if remote.returncode != 0 or not self._remote_matches(remote.stdout.strip(), repo, expected):
            raise ProvisionError("remote_mismatch", f"remote origin is {remote.stdout.strip()!r}, expected {repo!r}", "Re-provision from the correct repository.")
        state = self.git(["-C", str(target), "status", "--porcelain"], None)
        if state.returncode != 0 or state.stdout.strip():
            raise ProvisionError("checkout_dirty", "the checkout has local modifications", "Inspect the workspace; do not retry blindly.")

    def _root_for(self, target: Path) -> Path:
        for profile in self.policy.values():
            root = Path(profile.workspace_root)
            try:
                if os.path.commonpath([str(root), str(target)]) == str(root):
                    return root
            except ValueError:
                continue
        raise ProvisionError("path_escape", f"{target} is not inside any enrolled workspace root", "Refusing to touch it.")

    def _remote_matches(self, remote: str, repo: str, expected: str | None = None) -> bool:
        """The remote must be the repository we were told to fetch, by either
        its canonical identity or the exact URL this host uses for it."""

        def canon(value: str) -> str:
            text = value.strip().rstrip("/")
            if text.endswith(".git"):
                text = text[:-4]
            return text.lower()

        if not remote:
            return False
        return canon(remote) in {canon(repo), canon(expected or self.url_for(repo))}

    def _registration_matches(self, entry: Mapping[str, Any], identity: Mapping[str, Any], target: Path) -> bool:
        return (
            str(entry.get("repo") or "") == identity["repo"]
            and str(entry.get("source_rev") or "") == identity["source_rev"]
            and str(entry.get("profile") or "") == str(identity.get("profile") or "")
            and same_path(str(entry.get("path") or ""), target)
        )

    def _receipt_fields(self, project: str, repo: str, source_rev: str, workspace: str, profile: Profile, adopted: bool, ready_at: float) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "status": "ready",
            "project": project,
            "launcher_project": project,
            "repo": repo,
            "resolved_revision": source_rev,
            "workspace": workspace,
            "profile": profile.name,
            "task_scopes": list(profile.task_scopes),
            "host": socket.gethostname(),
            "fixture": PureWindowsPath(workspace).name,
            "ttl_hours": profile.ttl_hours,
            "expires_at": _iso(ready_at + profile.ttl_hours * 3600),
            "verified": {"head": source_rev, "detached": True, "clean": True},
            "transport": self._transport.get(workspace, "origin"),
            "adopted": adopted,
        }


    # -- capability advertisement and receipt lookup ------------------------

    def capabilities(self) -> dict[str, Any]:
        """Advertise only what this host actually implements and has enabled.

        The launcher calls this with the live policy; a disabled host advertises
        ``enabled: false`` and an empty profile list rather than pretending.
        """
        return {
            "schema": SCHEMA,
            "backend": "hyvv",
            "enabled": True,
            "profiles": sorted(self.policy),
            "states": list(STATES),
            "supports_cleanup": any(profile.allow_cleanup for profile in self.policy.values()),
            "supports_receipt_lookup": True,
            "mirrors": sorted({name for profile in self.policy.values() for name, _ in profile.mirrors}),
            "idempotency": "idempotency_key binds repo, source_rev, profile, project, job_id",
        }

    def lookup_receipt(self, key: str) -> dict[str, Any]:
        """Read-only receipt lookup that revalidates against reality.

        A stored receipt is not authority by itself: the live registry, the full
        workspace path and the checkout must still agree, or the record is marked
        uncertain and refused.
        """
        record = self.journal.read(key)
        if record is None:
            raise ProvisionError("receipt_unknown", f"no provisioning record for {key}", "Provision first; do not guess.")
        if record.get("state") in ("retiring", "cleaned"):
            raise ProvisionError("receipt_retired", f"{key} was cleaned", "Use a new idempotency key.")
        if record.get("state") != "ready":
            raise ProvisionError("receipt_not_ready", f"{key} is {record.get('state')!r}", "Reconcile the provisioning state.")
        try:
            return self._replay_ready(record)
        except ProvisionError as exc:
            raise ProvisionError("receipt_stale", exc.message, "Re-provision with a new key after inspecting the host.") from exc

    # -- cleanup ------------------------------------------------------------

    def cleanup(self, project: str, *, active_attempts: Callable[[str], int] = lambda name: 0) -> dict[str, Any]:
        entry = self.registry.get(project)
        if not entry:
            # A crash between unregistering and deleting leaves a journal record.
            for record in self.journal.all():
                if record.get("project") == project and record.get("state") in ("retiring", "cleaned"):
                    entry = {"repo": record.get("repo"), "path": record.get("workspace"), "profile": record.get("profile"), "source_rev": record.get("source_rev")}
                    break
            if not entry:
                raise ProvisionError("unknown_project", f"{project} is not provisioned", "Nothing to clean.")

        profile = self.policy.get(str(entry.get("profile") or ""))
        if profile is None or not profile.allow_cleanup:
            raise ProvisionError("cleanup_not_allowed", f"profile {entry.get('profile')!r} does not permit cleanup", "Require explicit approval.")
        if active_attempts(project):
            raise ProvisionError("project_in_use", f"{project} has nonterminal attempts", "Retry when idle.")

        workspace = Path(str(entry.get("path") or ""))
        root = Path(profile.workspace_root)
        ensure_contained(root, workspace)

        record = next((r for r in self.journal.all() if r.get("project") == project and r.get("state") in ("ready", "in_use", "retiring")), None)
        key = record["idempotency_key"] if record else f"cleanup-{project}"
        if record is None:
            record = {"schema": SCHEMA, "idempotency_key": key, "project": project, "repo": entry.get("repo"),
                      "source_rev": entry.get("source_rev"), "profile": entry.get("profile"), "workspace": str(workspace), "created_at": _now()}

        record.update(state="retiring", cleanup_started_at=_now())
        self.journal.write(key, record)

        self.registry.delete(project)                   # unregister first: no dispatch into a dying tree

        _rmtree(workspace)   # then delete the checkout

        record.update(state="cleaned", cleaned_at=_now())
        self.journal.write(key, record)
        return {"project": project, "state": "cleaned", "workspace": str(workspace), "evidence_preserved": True}


def _rmtree(path: Path) -> None:
    """Delete a checkout, coping with read-only files (git objects on Windows)."""

    import shutil
    import stat as _stat

    def retry(func, failed, _exc):
        try:
            os.chmod(failed, _stat.S_IWRITE)
            func(failed)
        except OSError:
            raise

    shutil.rmtree(path, onerror=retry)


def _now() -> float:
    return time.time()


def _iso(stamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
