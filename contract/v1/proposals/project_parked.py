"""Proposed dispatch gate for parked or archived projects.

Status: proposed, aligns with workflow docs/18-parking-projects.md
(MR.INDIA, in progress). Not used by launcher v0.1.15.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_REL = ".workflow/project-state.json"
BLOCKED = {"parked", "archived"}
CODE = "project_parked"


class ParkedError(Exception):
    def __init__(self, http: int, code: str, message: str, extra: dict[str, Any] | None = None):
        self.http = http
        self.code = code
        self.message = message
        self.extra = extra or {}
        super().__init__(message)

    def body(self) -> dict[str, Any]:
        out = {"code": self.code, "error": self.code, "message": self.message}
        out.update(self.extra)
        return out


def read_project_state(repo_root: Path | None) -> dict[str, Any] | None:
    """Return the state object, or None when the file is missing (treated as active)."""
    if repo_root is None:
        return None
    path = Path(repo_root) / STATE_REL
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("state") not in ("active", "parked", "archived"):
        raise ParkedError(400, "invalid_project_state", "project-state.json is not a valid project state")
    # Every documented field is present; unknown extras are left alone.
    return {
        "state": data["state"],
        "reason": data.get("reason"),
        "at": data.get("at"),
        "by": data.get("by"),
    }


def check_project_dispatch(
    repo_root: Path | None,
    *,
    override: bool = False,
    override_reason: str | None = None,
    by: str | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Return an override record to store on the attempt, or None if no gate applied.

    Raises ParkedError(409, project_parked) when the project is parked or archived
    and no valid override is supplied. An override without a reason is 400.
    """
    state = read_project_state(repo_root)
    if state is None or state["state"] == "active":
        return None
    if state["state"] in BLOCKED:
        if not override:
            raise ParkedError(
                409,
                CODE,
                f"project is {state['state']}",
                extra={k: state[k] for k in ("state", "reason", "at", "by")},
            )
        reason = (override_reason or "").strip()
        if not reason:
            raise ParkedError(400, "override_reason_required", "an explicit override requires a reason")
        return {
            "project_parked_override": True,
            "project_parked_override_reason": reason,
            "project_state": state,
            "project_parked_override_at": now,
            "project_parked_override_by": by,
        }
    return None
