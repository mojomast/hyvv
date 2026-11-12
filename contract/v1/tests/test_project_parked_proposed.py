"""Pending conformance tests for the project_parked dispatch gate.

Proposed, aligns with workflow docs/18-parking-projects.md (MR.INDIA, in progress).
The rules are pinned here against the proposal helper. The launcher v0.1.15
check is pending and skipped until the proposal is accepted and implemented.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from contract.v1.proposals.project_parked import (  # noqa: E402
    CODE,
    STATE_REL,
    ParkedError,
    check_project_dispatch,
)

PROPOSAL_STATUS = "proposed, aligns with workflow docs/18-parking-projects.md (MR.INDIA, in progress)"


def _write_state(root: Path, state: str, reason="paused for review", at="2026-11-12T21:00:00Z", by="coordinator"):
    path = root / STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": state, "reason": reason, "at": at, "by": by}), encoding="utf-8")
    return path


class ProposedProjectParkedRules(unittest.TestCase):
    def test_active_or_missing_state_allows_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(check_project_dispatch(root))
            _write_state(root, "active")
            self.assertIsNone(check_project_dispatch(root))

    def test_parked_or_archived_returns_409_project_parked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for state in ("parked", "archived"):
                _write_state(root, state, reason=f"because {state}")
                with self.assertRaises(ParkedError) as ctx:
                    check_project_dispatch(root)
                err = ctx.exception
                self.assertEqual(err.http, 409)
                self.assertEqual(err.code, "project_parked")
                self.assertEqual(err.code, CODE)
                body = err.body()
                self.assertEqual(body["error"], "project_parked")
                self.assertEqual(body["state"], state)
                self.assertEqual(body["reason"], f"because {state}")
                self.assertEqual(body["at"], "2026-11-12T21:00:00Z")
                self.assertEqual(body["by"], "coordinator")

    def test_override_requires_a_reason_and_is_recorded_on_the_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_state(root, "parked")
            with self.assertRaises(ParkedError) as ctx:
                check_project_dispatch(root, override=True, override_reason="  ")
            self.assertEqual(ctx.exception.http, 400)
            self.assertEqual(ctx.exception.code, "override_reason_required")
            rec = check_project_dispatch(
                root,
                override=True,
                override_reason="hotfix for a live incident",
                by="coordinator-a",
                now="2026-11-12T21:30:00Z",
            )
            self.assertTrue(rec["project_parked_override"])
            self.assertEqual(rec["project_parked_override_reason"], "hotfix for a live incident")
            self.assertEqual(rec["project_state"]["state"], "parked")
            self.assertEqual(rec["project_parked_override_by"], "coordinator-a")
            self.assertEqual(rec["project_parked_override_at"], "2026-11-12T21:30:00Z")

    def test_proposal_is_marked_as_aligning_with_workflow_parking_doc(self):
        text = (ROOT / "contract/v1/proposals/project-parked.md").read_text(encoding="utf-8")
        self.assertIn(PROPOSAL_STATUS, text)
        self.assertIn("docs/18-parking-projects.md", text)
        self.assertIn("409", text)
        self.assertIn("project_parked", text)


class PendingLauncherEnforcement(unittest.TestCase):
    @unittest.skip("pending: proposed, aligns with workflow docs/18-parking-projects.md (MR.INDIA, in progress)")
    def test_PENDING_launcher_create_refuses_parked_projects_with_409_project_parked(self):
        """Launcher v0.1.15 does not yet enforce this proposal."""
        src = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("project_parked", src)
        self.assertIn("project_parked_override", src)
