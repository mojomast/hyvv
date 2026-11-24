"""Contract for the TestPyPI rehearsal (rizzstack decision 7, PyPI prep only).

The rehearsal exercises the same trusted-publishing path as the real release,
but only by manual dispatch, never from a tag, and only to test.pypi.org. The
real publish stays tag-driven behind the `pypi` environment, whose required
reviewer (the owner) GitHub enforces outside the repo.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REHEARSAL = ROOT / ".github/workflows/testpypi-rehearsal.yml"
RELEASE = ROOT / ".github/workflows/release.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestPyPIRehearsalTests(unittest.TestCase):
    def test_manual_dispatch_only_never_a_tag(self):
        y = _read(REHEARSAL)
        on = y[y.index("\non:"):y.index("\npermissions:")]
        self.assertIn("workflow_dispatch:", on)
        for trigger in ("push:", "pull_request", "tags:", "release:", "schedule:", "workflow_run"):
            self.assertNotIn(trigger, on)
        self.assertIn("!startsWith(github.ref, 'refs/tags/')", y)

    def test_publishes_only_to_test_pypi_with_trusted_publishing(self):
        y = _read(REHEARSAL)
        self.assertIn("repository-url: https://test.pypi.org/legacy/", y)
        self.assertEqual(re.findall(r"https://(?:upload\.)?pypi\.org", y), [])
        self.assertIn("environment: testpypi", y)
        self.assertIn("id-token: write", y)
        self.assertIn("packages-dir: dist", y)
        self.assertNotIn("password:", y)
        self.assertNotIn("secrets.", y)

    def test_no_release_tag_or_write_token(self):
        y = _read(REHEARSAL)
        self.assertNotIn("gh release", y)
        self.assertNotIn("git tag", y)
        self.assertNotIn("git push", y)
        self.assertNotRegex(y, r"contents:\s*write")
        self.assertIn("persist-credentials: false", y)
        self.assertRegex(y, r"(?m)^permissions:\s*\{\}\s*$")

    def test_unique_dev_version_and_same_dist_check_as_release(self):
        y = _read(REHEARSAL)
        self.assertIn(".dev${GITHUB_RUN_ID}", y)
        self.assertIn("python scripts/build_release_assets.py --dist dist --out release-assets", y)

    def test_every_action_is_pinned_to_a_full_sha(self):
        for ref in re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", _read(REHEARSAL)):
            self.assertRegex(ref, r"@[0-9a-f]{40}$", ref)

    def test_real_publish_stays_tag_driven_behind_the_pypi_environment(self):
        y = _read(RELEASE)
        self.assertIn('tags: ["v*"]', y)
        self.assertIn("environment: pypi", y)
        self.assertNotIn("test.pypi.org", y)


if __name__ == "__main__":
    unittest.main()
