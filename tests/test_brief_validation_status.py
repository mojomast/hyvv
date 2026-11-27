"""The brief-validation report must distinguish unavailable from invalid and never echo values.

Both halves are about what a coordinator sees on /v1/attempts: a broken deploy (missing
validator or schema) is "unavailable", not a bad brief; and validator problems name the
field path, never the submitted value.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
assert spec is not None and spec.loader is not None
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)


def _v1_brief(**overrides):
    brief = {
        "schema": "rizzstack.brief/1",
        "objective": "Check the validation report.",
        "project": "demo",
        "source_rev": "a" * 40,
        "assigned_files": ["launcher.py"],
        "acceptance": {"criteria": ["the report is honest"]},
        "risk": {"tier": "low"},
    }
    brief.update(overrides)
    return brief


class Unavailable(unittest.TestCase):
    def test_missing_validator_module_reports_unavailable(self):
        with mock.patch.object(L, "_rizzstack_validators", None):
            report = L.brief_validation(_v1_brief())
        self.assertEqual(report["status"], "unavailable")
        self.assertTrue(any("unavailable" in problem for problem in report["problems"]))

    def test_missing_schema_reports_unavailable(self):
        with tempfile.TemporaryDirectory(prefix="hyvv-bvfix-") as tmp:
            with mock.patch.object(L, "_BRIEF_SCHEMA_DIR", Path(tmp)):
                report = L.brief_validation(_v1_brief())
        self.assertEqual(report["status"], "unavailable")
        self.assertTrue(any("unavailable" in problem for problem in report["problems"]))


class NoValueEcho(unittest.TestCase):
    def test_an_enum_problem_names_the_path_not_the_value(self):
        report = L.brief_validation(_v1_brief(risk={"tier": "critical"}))
        self.assertEqual(report["status"], "invalid")
        self.assertTrue(report["problems"])
        joined = "\n".join(report["problems"])
        self.assertIn("$.risk.tier", joined)
        self.assertIn("not in the allowed enum", joined)
        self.assertNotIn("critical", joined, "the submitted value must not be recorded")

    def test_an_unversioned_brief_is_still_unversioned(self):
        report = L.brief_validation({"objective": "Legacy brief.", "project": "demo"})
        self.assertEqual(report["status"], "unversioned")


if __name__ == "__main__":
    unittest.main()
