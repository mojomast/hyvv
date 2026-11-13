"""AC-PARK: /v1 dispatch honors .workflow/project-state.json (workflow.project-state/v1).

Matches workflow docs/18-parking-projects.md and tools/project-state/check.py at
mojomast/workflow 2bcf0c5 (branch feat/park-project). Implemented in the /v1
reference (ArtifactStore.create_attempt); launcher 0.1.15 does not enforce it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from contract.v1 import project_state as PS  # noqa: E402
from contract.v1.reference import ArtifactStore  # noqa: E402
from contract.v1.tests.test_artifact_ac import OWNER, OWNER_KEY, PRINCIPALS, REV, assert_owner_only  # noqa: E402

PROJECT = "demo"


def _symlink_or_skip(tc, target, link, **kw):
    try:
        os.symlink(target, link, **kw)
    except (OSError, NotImplementedError) as exc:
        tc.skipTest(f"symlinks unavailable here: {exc}")


def _history(*states):
    hist, prev = [], None
    for i, s in enumerate(states):
        by = "human:owner" if (s == "active" or prev is None) else "agent:hermes-forge"
        hist.append({"state": s, "at": f"2026-11-{10 + i:02d}T12:00:00Z", "by": by, "reason": f"step {i} {s}"})
        prev = s
    return hist


def _doc(*states, **over):
    hist = _history(*states)
    last = hist[-1]
    d = {"schema": "workflow.project-state/v1", "schema_version": "v1", "project": PROJECT,
         "state": last["state"], "at": last["at"], "by": last["by"], "reason": last["reason"],
         "generation": len(hist), "history": hist}
    if last["state"] in ("parked", "archived"):
        d["snapshot_path"] = "PARKED.md"
    d.update(over)
    return d


class Gate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        (self.repo / ".workflow").mkdir(parents=True)
        (self.repo / "PARKED.md").write_text("# Parked\nResume: run the tests.\n", encoding="utf-8")
        self.store = ArtifactStore(root=base / "host", principals=PRINCIPALS, project_roots={PROJECT: self.repo})
        self.n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, doc):
        (self.repo / ".workflow/project-state.json").write_text(json.dumps(doc), encoding="utf-8")

    def create(self, generation=..., **kw):
        self.n += 1
        aid = f"att_park{self.n:012d}"
        if generation is not ...:
            kw["project_state_generation"] = generation
        st, body = self.store.create_attempt(aid, bearer=OWNER_KEY, source_rev=REV, project=PROJECT, **kw)
        return aid, st, body

    def assertRefused(self, res, http, code, detail=None):
        aid, st, body = res
        self.assertEqual((st, body.get("code")), (http, code), body)
        self.assertNotIn(aid, self.store.attempts, "a refused dispatch must not create an attempt")
        self.assertNotIn("artifact_capability", body)
        if detail:
            self.assertEqual(body.get("detail"), detail, body)
        return body


class AC_PARK_Dispatch(Gate):
    def test_absent_file_defaults_active_and_is_recorded(self):
        aid, st, body = self.create(generation=0)
        self.assertEqual(st, 201, body)
        rec = self.store.attempts[aid]["project_state"]
        self.assertEqual((rec["state"], rec["generation"], rec["defaulted"]), ("active", 0, True))

    def test_active_with_matching_generation_is_allowed_and_recorded(self):
        self.write(_doc("active", "parked", "active"))
        aid, st, body = self.create(generation=3)
        self.assertEqual(st, 201, body)
        rec = self.store.attempts[aid]["project_state"]
        self.assertEqual((rec["state"], rec["generation"], rec["defaulted"]), ("active", 3, False))

    def test_parked_is_409_project_parked(self):
        self.write(_doc("active", "parked"))
        body = self.assertRefused(self.create(generation=2), 409, "project_parked")
        for k in ("project", "state", "at", "by", "reason", "generation", "message"):
            self.assertIn(k, body)
        self.assertEqual((body["state"], body["generation"], body["project"]), ("parked", 2, PROJECT))

    def test_archived_is_409_project_archived(self):
        self.write(_doc("active", "archived"))
        body = self.assertRefused(self.create(generation=2), 409, "project_archived")
        self.assertEqual(body["state"], "archived")

    def test_in_flight_attempts_are_never_cancelled_by_a_park(self):
        aid, st, _ = self.create(generation=0)
        self.store.attempts[aid]["state"] = "running"
        self.write(_doc("active", "parked"))
        self.assertRefused(self.create(generation=2), 409, "project_parked")
        self.assertEqual(self.store.attempts[aid]["state"], "running")

    def test_unknown_project_is_refused(self):
        st, body = self.store.create_attempt("att_parkunknown01", bearer=OWNER_KEY, project="not-configured",
                                             project_state_generation=0)
        self.assertEqual((st, body["code"]), (403, "project_not_authorized"))


class AC_PARK_Override(Gate):
    def test_flag_without_reason_is_400(self):
        self.write(_doc("active", "parked"))
        for reason in (None, "", "   "):
            with self.subTest(reason=reason):
                self.assertRefused(self.create(generation=2, project_parked_override=True,
                                               project_parked_override_reason=reason), 400, "override_reason_required")

    def test_reason_without_flag_is_not_an_override(self):
        self.write(_doc("active", "parked"))
        self.assertRefused(self.create(generation=2, project_parked_override_reason="urgent fix"), 409, "project_parked")

    def test_override_with_reason_is_allowed_attributed_and_persisted(self):
        self.write(_doc("active", "archived"))
        aid, st, body = self.create(generation=2, project_parked_override=True,
                                    project_parked_override_reason="  hotfix for a live incident ",
                                    coordinator="someone-else")
        self.assertEqual(st, 201, body)
        rec = self.store.attempts[aid]
        ov = rec["project_parked_override"]
        self.assertEqual(ov["reason"], "hotfix for a live incident")
        self.assertEqual(ov["by"], OWNER, "actor is the authenticated principal, never a request field")
        self.assertEqual((ov["state"], ov["generation"]), ("archived", 2))
        for k in ("at", "state_at", "state_by", "state_reason"):
            self.assertIn(k, ov)
        # Persisted on disk, owner-only, and reloaded by a new store.
        audit = self.store.root / "dispatch-overrides.json"
        self.assertTrue(audit.is_file())
        assert_owner_only(self, audit)
        again = ArtifactStore(root=self.store.root, principals=PRINCIPALS, project_roots={PROJECT: self.repo})
        self.assertEqual(again.overrides[aid]["reason"], "hotfix for a live incident")
        self.assertEqual(again.overrides[aid]["by"], OWNER)

    def test_override_does_not_bypass_invalid_state_or_generation(self):
        self.write(_doc("active", "parked", snapshot_path="../outside.md"))
        self.assertRefused(self.create(generation=2, project_parked_override=True, project_parked_override_reason="x"),
                           400, "invalid_project_state", "snapshot_path_invalid")
        self.write(_doc("active", "parked"))
        self.assertRefused(self.create(generation=1, project_parked_override=True, project_parked_override_reason="x"),
                           409, "project_state_changed")


class AC_PARK_Generation(Gate):
    def test_generation_is_required(self):
        self.write(_doc("active"))
        self.assertRefused(self.create(), 409, "project_state_changed")
        self.assertRefused(self.create(generation=None), 409, "project_state_changed")
        self.assertRefused(self.create(generation="1"), 409, "project_state_changed")
        self.assertRefused(self.create(generation=True), 409, "project_state_changed")

    def test_stale_generation_is_409_even_when_now_parked(self):
        """AC-PARK-22: read active at N, parked at N+1 before start -> project_state_changed."""
        self.write(_doc("active", "parked"))
        self.assertRefused(self.create(generation=1), 409, "project_state_changed")

    def test_file_generation_must_equal_history_length(self):
        for bad in (1, 2, 4, 0, None, "3"):
            with self.subTest(generation=bad):
                self.write(_doc("active", "parked", "active", generation=bad) if bad is not None
                           else {k: v for k, v in _doc("active", "parked", "active").items() if k != "generation"})
                self.assertRefused(self.create(generation=bad if isinstance(bad, int) else 3), 409, "project_state_changed")

    def test_absent_file_needs_generation_zero(self):
        self.assertRefused(self.create(generation=1), 409, "project_state_changed")


class AC_PARK_InvalidState(Gate):
    def test_invalid_files_fail_closed(self):
        good = _doc("active", "parked")
        cases = {
            "unknown field": dict(good, extra=1),
            "old changed_at name": {**{k: v for k, v in good.items() if k != "at"}, "changed_at": good["at"]},
            "wrong schema": dict(good, schema="workflow.project-state/v0"),
            "last entry mismatch": dict(good, reason="different"),
            "bad transition": _doc("active", "active"),
            "resume by agent": dict(_doc("active", "parked", "active"),
                                    history=_history("active", "parked")[:2] + [{"state": "active", "at": "2026-11-12T12:00:00Z", "by": "agent:x", "reason": "step 2 active"}],
                                    by="agent:x"),
            "parked without snapshot": {k: v for k, v in good.items() if k != "snapshot_path"},
        }
        for name, doc in cases.items():
            with self.subTest(case=name):
                self.write(doc)
                self.assertRefused(self.create(generation=doc.get("generation", 2)), 400, "invalid_project_state")

    def test_unreadable_and_oversized(self):
        p = self.repo / ".workflow/project-state.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertRefused(self.create(generation=0), 400, "invalid_project_state")
        p.write_text(" " * (PS.MAX_STATE_BYTES + 1), encoding="utf-8")
        self.assertRefused(self.create(generation=0), 400, "invalid_project_state")

    def test_state_file_or_dir_symlink(self):
        real = self.repo / "real-state.json"
        real.write_text(json.dumps(_doc("active")), encoding="utf-8")
        _symlink_or_skip(self, real, self.repo / ".workflow/project-state.json")
        self.assertRefused(self.create(generation=1), 400, "invalid_project_state")


class AC_PARK_SnapshotPath(Gate):
    def test_unsafe_text_is_refused(self):
        for rel in ("../x.md", "a/../PARKED.md", "./PARKED.md", "a/./PARKED.md", ".", "..", "", "a//PARKED.md",
                    "PARKED.md/", "/etc/passwd", "C:\\x.md", "C:/x.md", "docs\\PARKED.md", "x" * 245):
            with self.subTest(rel=rel):
                self.write(_doc("active", "parked", snapshot_path=rel))
                self.assertRefused(self.create(generation=2), 400, "invalid_project_state", "snapshot_path_invalid")

    def test_missing_and_non_regular(self):
        self.write(_doc("active", "parked", snapshot_path="NOPE.md"))
        self.assertRefused(self.create(generation=2), 400, "invalid_project_state", "snapshot_missing")
        (self.repo / "docs").mkdir()
        self.write(_doc("active", "parked", snapshot_path="docs"))
        self.assertRefused(self.create(generation=2), 400, "invalid_project_state", "snapshot_path_invalid")

    def test_symlink_file_component(self):
        _symlink_or_skip(self, self.repo / "PARKED.md", self.repo / "LINK.md")
        self.write(_doc("active", "parked", snapshot_path="LINK.md"))
        self.assertRefused(self.create(generation=2), 400, "invalid_project_state", "snapshot_path_symlink")

    def test_symlink_directory_component(self):
        (self.repo / "real").mkdir()
        (self.repo / "real/PARKED.md").write_text("x", encoding="utf-8")
        _symlink_or_skip(self, self.repo / "real", self.repo / "via", target_is_directory=True)
        self.write(_doc("active", "parked", snapshot_path="via/PARKED.md"))
        self.assertRefused(self.create(generation=2), 400, "invalid_project_state", "snapshot_path_symlink")

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_windows_junction_component_is_refused(self):
        import subprocess
        (self.repo / "real").mkdir()
        (self.repo / "real/PARKED.md").write_text("x", encoding="utf-8")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.repo / "via"), str(self.repo / "real")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("mklink /J failed")
        self.write(_doc("active", "parked", snapshot_path="via/PARKED.md"))
        self.assertRefused(self.create(generation=2), 400, "invalid_project_state", "snapshot_path_symlink")

    def test_resolved_path_must_stay_inside_the_repo(self):
        self.write(_doc("active", "parked"))
        outside = Path(self._tmp.name) / "outside.md"
        outside.write_text("x", encoding="utf-8")
        real = os.path.realpath

        def fake(p, *a, **k):
            return str(outside) if str(p).endswith("PARKED.md") else real(p, *a, **k)

        with mock.patch.object(PS.os.path, "realpath", side_effect=fake):
            self.assertRefused(self.create(generation=2), 400, "invalid_project_state", "snapshot_path_escape")

    def test_a_safe_nested_snapshot_is_accepted_for_override(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs/PARKED.md").write_text("x", encoding="utf-8")
        self.write(_doc("active", "parked", snapshot_path="docs/PARKED.md"))
        aid, st, body = self.create(generation=2, project_parked_override=True, project_parked_override_reason="ok")
        self.assertEqual(st, 201, body)



class AC_PARK_Published(unittest.TestCase):
    def test_error_codes_json_matches_the_gate(self):
        codes = json.loads((ROOT / "contract/v1/error_codes.json").read_text(encoding="utf-8"))["project_state"]
        published = {c: int(h) for h, cs in codes["http"].items() for c in cs}
        self.assertEqual(published, PS.PARK_HTTP)

    def test_status_label_is_reference_and_backed_by_these_tests(self):
        status = json.loads((ROOT / "contract/v1/STATUS.json").read_text(encoding="utf-8"))
        key = next(k for k in status["features"] if k.startswith("project-state dispatch gate"))
        self.assertEqual(status["features"][key], "reference")
        self.assertEqual(status["reference_evidence"][key], ["ArtifactStore._gate_project", "AC_PARK_Dispatch"])
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertNotIn("project_state_generation", launcher, "relabel launcher-0.1.15 if the launcher gains the gate")


if __name__ == "__main__":
    unittest.main()
