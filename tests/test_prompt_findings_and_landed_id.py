"""RED for wave A slice HY-3 (review Fix now #5 and the hyvv half of #13).

#5: the research gate requires accepted findings before a retry, but
render_prompt never prints them, so the retried bee repeats the failure. The
approved test ids (brief.tests) are dropped the same way. Render both.
#13: the bee-landed dataschema names a misspelled host. Use the workflow
schema's own $id.
Run: python3 -m pytest -q tests/test_prompt_findings_and_landed_id.py
"""
import sys, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cua", mock.MagicMock())
import launcher  # noqa: E402

FINDINGS = {
    "failure_id": "fail_0001",
    "verdict": "code-problem",
    "summary": "the parser drops the trailing newline",
    "sources": [{"kind": "test", "ref": "tests/test_parse.py::test_trailing_newline"}],
    "rejected_alternatives": ["raise the timeout"],
}


class FindingsInPrompt(unittest.TestCase):
    def _prompt(self, **extra):
        return launcher.render_prompt({"objective": "fix the parser", **extra})

    def test_findings_are_rendered(self):
        p = self._prompt(findings=FINDINGS)
        self.assertIn("# Research findings", p)
        self.assertIn("fail_0001", p)
        self.assertIn("code-problem", p)
        self.assertIn("the parser drops the trailing newline", p)
        self.assertIn("tests/test_parse.py::test_trailing_newline", p)
        self.assertIn("raise the timeout", p)

    def test_findings_come_before_the_rules(self):
        p = self._prompt(findings=FINDINGS)
        self.assertLess(p.index("# Research findings"), p.index("# Rules"))

    def test_rendering_is_bounded(self):
        big = dict(FINDINGS, summary="y" * 50_000)
        self.assertLess(len(self._prompt(findings=big)), 12_000)

    def test_non_mapping_findings_are_ignored_not_crashing(self):
        self.assertNotIn("# Research findings", self._prompt(findings="not an object"))
        self.assertNotIn("# Research findings", self._prompt())

    def test_approved_tests_are_rendered(self):
        p = self._prompt(tests=["tests/test_parse.py::test_trailing_newline", "tests/test_parse.py::test_empty"])
        self.assertIn("# Tests that must pass", p)
        self.assertIn("tests/test_parse.py::test_empty", p)

    def test_acceptance_dict_tests_are_rendered(self):
        b = launcher.normalize_brief({"objective": "x", "acceptance": {"criteria": ["c"], "tests": ["tests/test_a.py"]}})
        self.assertIn("tests/test_a.py", launcher.render_prompt(b))


class LandedEventIds(unittest.TestCase):
    CANONICAL = "https://github.com/mojomast/workflow/schemas/bee-landed.schema.json"

    def test_dataschema_is_the_workflow_schema_id(self):
        self.assertEqual(launcher.LANDED_EVENT_SCHEMA, self.CANONICAL)

    def test_type_and_data_schema_are_unchanged(self):
        self.assertEqual(launcher.LANDED_EVENT_TYPE, "rizzstack.bee.landed.v1")
        self.assertEqual(launcher.LANDED_DATA_SCHEMA, "rizzstack.bee-landed/1")

    def test_no_misspelled_host_left(self):
        for rel in ("launcher.py", "docs/bee-landed.md"):
            self.assertNotIn("rizstack.dev", (ROOT / rel).read_text("utf-8"), rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
