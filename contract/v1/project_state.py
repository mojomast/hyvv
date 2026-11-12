"""workflow.project-state/v1 dispatch gate for the /v1 reference.

Implements the hyvv side of workflow docs/18-parking-projects.md (mojomast/workflow
feat/park-project at 2bcf0c5), ported from its reference checker
tools/project-state/check.py. Standard library only. Never cancels anything.

Refusals (DispatchRefused.http / .code):
  409 project_parked        state is parked
  409 project_archived      state is archived
  409 project_state_changed the dispatch's generation is missing or differs from the
                            file, or the file's generation is missing or does not
                            equal its history length
  400 invalid_project_state the file is present but unreadable, oversized, a symlink,
                            or invalid (detail names snapshot_path failures)
  400 override_reason_required  override flag without a non-empty reason
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

STATE_REL = ".workflow/project-state.json"
SCHEMA = "workflow.project-state/v1"
STATES = ("active", "parked", "archived")
BLOCKED = {"parked": "project_parked", "archived": "project_archived"}
TRANSITIONS = {
    ("active", "parked"), ("active", "archived"), ("parked", "active"),
    ("parked", "archived"), ("archived", "active"),
}
TS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$")
ACTOR = re.compile(r"^(human|agent|bot|orbit|hyvv|bundlebees):[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Same text rule as the schema's snapshot_path pattern: relative, no empty, "." or
# ".." segment anywhere, no backslash (the character class excludes it), <= 244.
SAFE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)[.]{1,2}(?:/|$))(?!.*//)(?!.*/$)[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
MAX_STATE_BYTES = 256 * 1024
# Published in error_codes.json under "project_state".
PARK_HTTP = {
    "invalid_project_state": 400,
    "override_reason_required": 400,
    "project_not_authorized": 403,
    "project_parked": 409,
    "project_archived": 409,
    "project_state_changed": 409,
}
FIELDS = {"schema", "schema_version", "project", "display_name", "state", "at", "by",
          "reason", "generation", "snapshot_path", "primary_repo", "history"}


_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link(st: os.stat_result) -> bool:
    """Symlink, or on Windows any reparse point (junctions and mount points too)."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


class DispatchRefused(Exception):
    def __init__(self, http: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.http, self.code, self.message, self.extra = http, code, message, extra

    def body(self) -> dict[str, Any]:
        out = {"code": self.code, "error": self.code, "message": self.message}
        out.update(self.extra)
        return out


def _invalid(message: str, detail: str | None = None) -> DispatchRefused:
    extra = {"detail": detail} if detail else {}
    return DispatchRefused(400, "invalid_project_state", message, **extra)


def _changed(message: str, **extra: Any) -> DispatchRefused:
    return DispatchRefused(409, "project_state_changed", message, **extra)


def safe_snapshot(repo_root: Path, rel: Any) -> Path:
    """Resolve rel inside repo_root: text rule, no symlink component, regular file, contained."""
    if not isinstance(rel, str) or len(rel) > 244 or not SAFE_PATH.match(rel):
        raise _invalid("snapshot_path is not a safe repo-relative path", "snapshot_path_invalid")
    root = Path(os.path.realpath(repo_root))
    cur = root
    for part in rel.split("/"):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            raise _invalid("snapshot_path does not exist", "snapshot_missing") from None
        if _is_link(st):
            raise _invalid("snapshot_path has a symlink component", "snapshot_path_symlink")
    if not stat.S_ISREG(os.lstat(cur).st_mode):
        raise _invalid("snapshot_path is not a regular file", "snapshot_path_invalid")
    real = Path(os.path.realpath(cur))
    if real != root and root not in real.parents:
        raise _invalid("snapshot_path resolves outside the repository", "snapshot_path_escape")
    return real


def _entry_ok(e: Any, where: str) -> None:
    if not isinstance(e, dict) or e.get("state") not in STATES:
        raise _invalid(f"{where}: state is invalid")
    if not isinstance(e.get("at"), str) or not TS.match(e["at"]):
        raise _invalid(f"{where}: at is invalid")
    if not isinstance(e.get("by"), str) or not ACTOR.match(e["by"]):
        raise _invalid(f"{where}: by is invalid")
    r = e.get("reason")
    if not isinstance(r, str) or not r.strip() or len(r) > 500:
        raise _invalid(f"{where}: reason is invalid")


def validate(doc: Any, repo_root: Path) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise _invalid("state file is not an object")
    extra = set(doc) - FIELDS
    if extra:
        raise _invalid("unknown fields: " + ", ".join(sorted(extra)))
    if doc.get("schema") != SCHEMA or doc.get("schema_version") != "v1":
        raise _invalid("not workflow.project-state/v1")
    if not isinstance(doc.get("project"), str) or not PROJECT.match(doc["project"]):
        raise _invalid("project id is invalid")
    _entry_ok(doc, "state")
    hist = doc.get("history")
    if not isinstance(hist, list) or not hist:
        raise _invalid("history is missing or empty")
    for i, e in enumerate(hist):
        _entry_ok(e, f"history[{i}]")
        if set(e) - {"state", "at", "by", "reason"}:
            raise _invalid(f"history[{i}] has unknown fields")
    for i, (a, b) in enumerate(zip(hist, hist[1:]), start=1):
        if (a["state"], b["state"]) not in TRANSITIONS:
            raise _invalid(f"history[{i}]: {a['state']} -> {b['state']} is not allowed")
        if b["at"] < a["at"] and b["at"].endswith("Z") and a["at"].endswith("Z"):
            raise _invalid(f"history[{i}] is older than the entry before it")
        if b["state"] == "active" and not b["by"].startswith("human:"):
            raise _invalid(f"history[{i}]: resume must be by a human")
    last = hist[-1]
    if any(last[k] != doc[k] for k in ("state", "at", "by", "reason")):
        raise _invalid("last history entry does not match the current state")
    gen = doc.get("generation")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen != len(hist):
        raise _changed("state file generation must equal its history length")
    if doc["state"] in BLOCKED:
        if "snapshot_path" not in doc:
            raise _invalid("snapshot_path is required when parked or archived")
        safe_snapshot(repo_root, doc["snapshot_path"])
    elif "snapshot_path" in doc and (not isinstance(doc["snapshot_path"], str) or not SAFE_PATH.match(doc["snapshot_path"])):
        raise _invalid("snapshot_path is not a safe repo-relative path", "snapshot_path_invalid")
    return doc


def read_state(repo_root: Path) -> dict[str, Any] | None:
    """The validated state document, or None when the file is absent (defaults to active)."""
    root = Path(repo_root)
    sdir = root / ".workflow"
    path = sdir / "project-state.json"
    for p in (sdir, path):
        if os.path.lexists(p) and _is_link(os.lstat(p)):
            raise _invalid("state file path is a symlink")
    if not os.path.lexists(path):
        return None
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_STATE_BYTES:
        raise _invalid("state file is not a small regular file")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid("state file is unreadable") from None
    return validate(doc, root)


def check_dispatch(repo_root: Path, project: str, *, generation: Any, override: Any = False,
                   override_reason: Any = None) -> dict[str, Any]:
    """Decide one dispatch. Returns the gate record to store on the attempt, or raises
    DispatchRefused. Order: validity, generation, then parked/archived (override)."""
    doc = read_state(repo_root)
    current = 0 if doc is None else doc["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise _changed("project_state_generation is required", generation=current)
    if generation != current:
        raise _changed("project state changed since the dispatcher read it", generation=current,
                       **({"state": doc["state"]} if doc else {}))
    if doc is None:
        return {"state": "active", "generation": 0, "defaulted": True}
    rec = {"state": doc["state"], "generation": doc["generation"], "defaulted": False,
           "at": doc["at"], "by": doc["by"], "reason": doc["reason"]}
    if doc["state"] in BLOCKED:
        if override is not True:
            raise DispatchRefused(409, BLOCKED[doc["state"]], f"project is {doc['state']}", project=project,
                                  state=doc["state"], at=doc["at"], by=doc["by"], reason=doc["reason"],
                                  generation=doc["generation"])
        reason = override_reason.strip() if isinstance(override_reason, str) else ""
        if not reason:
            raise DispatchRefused(400, "override_reason_required", "an override needs a non-empty reason")
        rec["override_reason"] = reason
    return rec
