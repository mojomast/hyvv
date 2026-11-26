"""A launcher reports the build it runs, never the tag it merely sits near.

Regression: the worker PC answered ``version: 0.1.19`` while running 7f73b7c, 25 commits past
tag v0.1.19, so a build off the tag was indistinguishable from the tag itself. Every case here
pins one honesty rule: only a clean checkout exactly at the tag may answer the bare release.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules.setdefault("launcher", L)
spec.loader.exec_module(L)

TAG = "v" + L.VERSION
# The commit the worker PC was actually running when it claimed the tag.
FULL_SHA = "7f73b7c0b2f7a1d4c7f0b9e6a3d2c1b0a9f8e7d6"


def _git(root, *args):
    cmd = ["git", "-c", "user.name=build-identity test", "-c", "user.email=test@example.com",
           "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, cwd=str(root), check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()


def _repo(base, commits_past=0, dirty=False, tagged=True):
    """A real git checkout: one commit, the release tag, then *commits_past* more commits."""
    root = Path(base) / "work"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "launcher.py").write_text("# build under test\n", "utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "first")
    if tagged:
        _git(root, "tag", "-a", TAG, "-m", "release")
    for n in range(commits_past):
        (root / f"change-{n}.txt").write_text(str(n), "utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", f"change {n}")
    if dirty:  # a tracked modification is what `git describe --dirty` reports
        (root / "launcher.py").write_text("# modified after the commit\n", "utf-8")
    return root


class GitDescribe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hyvv-build-git-")
        self.addCleanup(self.tmp.cleanup)

    def test_a_clean_checkout_exactly_at_the_tag_reports_the_release(self):
        ident = L.build_identity(root=_repo(self.tmp.name))
        self.assertEqual(ident["version"], L.VERSION)
        self.assertEqual(ident["release"], L.VERSION)
        self.assertEqual(ident["tag"], TAG)
        self.assertEqual(ident["commits_past_tag"], 0)
        self.assertFalse(ident["dirty"])
        self.assertEqual(ident["source"], "git")
        self.assertRegex(ident["sha"], r"^[0-9a-f]{40}$")

    def test_n_commits_past_the_tag_never_claims_the_tag(self):
        root = _repo(self.tmp.name, commits_past=3)
        short = _git(root, "rev-parse", "--short=7", "HEAD")
        ident = L.build_identity(root=root)
        self.assertEqual(ident["commits_past_tag"], 3)
        self.assertEqual(ident["describe"], f"{TAG}-3-g{short}")
        self.assertEqual(ident["version"], f"{L.VERSION}+3.g{short}")
        self.assertNotEqual(ident["version"], L.VERSION)
        self.assertIn(short, ident["version"])

    def test_a_dirty_tree_past_the_tag_is_marked_dirty(self):
        ident = L.build_identity(root=_repo(self.tmp.name, commits_past=2, dirty=True))
        self.assertTrue(ident["dirty"])
        self.assertTrue(ident["describe"].endswith("-dirty"), ident["describe"])
        self.assertTrue(ident["version"].endswith(".dirty"), ident["version"])
        self.assertNotEqual(ident["version"], L.VERSION)

    def test_a_dirty_tree_exactly_at_the_tag_cannot_claim_it(self):
        ident = L.build_identity(root=_repo(self.tmp.name, dirty=True))
        self.assertEqual(ident["commits_past_tag"], 0)
        self.assertEqual(ident["version"], f"{L.VERSION}+dirty")
        self.assertNotEqual(ident["version"], L.VERSION)

    def test_a_checkout_with_no_reachable_tag_names_the_commit(self):
        ident = L.build_identity(root=_repo(self.tmp.name, tagged=False))
        self.assertIsNone(ident["tag"])
        self.assertIsNone(ident["commits_past_tag"])
        self.assertTrue(ident["version"].startswith(f"{L.VERSION}+g"), ident["version"])
        self.assertNotEqual(ident["version"], L.VERSION)


class WithoutGitMetadata(unittest.TestCase):
    """An installed copy has no .git, so the baked describe or SHA must still name the commit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hyvv-build-installed-")
        self.addCleanup(self.tmp.cleanup)

    def _installed(self, baked=None):
        root = Path(self.tmp.name) / "install"
        root.mkdir()
        if baked is not None:
            (root / L.BUILD_INFO_NAME).write_text(json.dumps(baked), "utf-8")
        return root

    def test_baked_describe_names_the_distance_and_commit(self):
        root = self._installed({"describe": f"{TAG}-25-g7f73b7c", "sha": FULL_SHA, "version": L.VERSION})
        ident = L.build_identity(root=root)
        self.assertEqual(ident["source"], "baked")
        self.assertEqual(ident["version"], f"{L.VERSION}+25.g7f73b7c")
        self.assertEqual(ident["commits_past_tag"], 25)
        self.assertEqual(ident["sha"], FULL_SHA)

    def test_only_a_sha_names_the_sha_not_the_tag(self):
        root = self._installed({"version": L.VERSION, "sha": FULL_SHA})
        ident = L.build_identity(root=root)
        self.assertEqual(ident["source"], "baked")
        self.assertEqual(ident["version"], f"{L.VERSION}+g7f73b7c")
        self.assertNotEqual(ident["version"], L.VERSION)

    def test_no_identity_at_all_still_refuses_the_bare_tag(self):
        ident = L.build_identity(root=self._installed())
        self.assertEqual(ident["source"], "unknown")
        self.assertEqual(ident["version"], f"{L.VERSION}+unknown")
        self.assertNotEqual(ident["version"], L.VERSION)

    def test_the_apply_update_receipt_is_the_last_resort(self):
        root = self._installed()
        (root / L.DEPLOYMENT_RECEIPT_NAME).write_text(json.dumps({
            "deployed_at": "2026-11-18T03:11:06Z", "version": L.VERSION,
            "sha": FULL_SHA, "describe": f"{TAG}-25-g7f73b7c"}), "utf-8")
        ident = L.build_identity(root=root)
        self.assertEqual(ident["source"], "receipt")
        self.assertEqual(ident["version"], f"{L.VERSION}+25.g7f73b7c")
        self.assertEqual(ident["sha"], FULL_SHA)


class LiveCheckout(unittest.TestCase):
    def test_the_source_tree_reports_the_commit_it_is_on(self):
        if not (HERE / ".git").exists():
            self.skipTest("no git metadata in this checkout")
        head = _git(HERE, "rev-parse", "HEAD")  # git itself is the oracle, not the code under test
        ident = L.build_identity(root=HERE)
        self.assertEqual(ident["sha"], head)
        try:
            tag = _git(HERE, "describe", "--tags", "--abbrev=0")
        except subprocess.CalledProcessError:
            tag = None  # a shallow CI clone fetches no tags; describe --always names the commit
        if tag is None:
            self.assertIsNone(ident["tag"])
            self.assertTrue(ident["version"].startswith(L.VERSION + "+g"), ident["version"])
            self.assertNotEqual(ident["version"], L.VERSION)
            return
        distance = int(_git(HERE, "rev-list", "--count", f"{tag}..HEAD"))
        self.assertEqual(ident["tag"], tag)
        self.assertEqual(ident["commits_past_tag"], distance)
        if distance:
            self.assertNotEqual(ident["version"], L.VERSION, "a build off the tag claimed the tag")
            self.assertIn(head[:7], ident["version"])


class Surfaces(unittest.TestCase):
    def test_version_health_and_runtime_status_agree_with_the_build(self):
        from tests.test_health_models import get, serve

        with tempfile.TemporaryDirectory(prefix="hyvv-build-surfaces-") as tmp:
            la, srv = serve(tmp)
            try:
                _, version = get(srv, "/v1/version")
                _, health = get(srv, "/v1/health")
            finally:
                srv.shutdown()
                srv.server_close()
            ident = L.build_identity(cfg=la.cfg)
            self.assertEqual(version["version"], ident["version"])
            self.assertEqual(version["release"], L.VERSION)
            self.assertEqual(version["build"], ident)
            self.assertEqual(health["version"], ident["version"])
            self.assertEqual(health["build"], ident)
            snap = json.loads((Path(tmp) / "runtime_status.json").read_text("utf-8"))
            self.assertEqual(snap["version"], ident["version"])
            self.assertEqual(snap["build"], ident)


def _baker():
    spec = importlib.util.spec_from_file_location("bake_build_info", HERE / "scripts" / "bake_build_info.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Baking(unittest.TestCase):
    def test_bake_writes_the_describe_so_an_installed_copy_keeps_its_build(self):
        with tempfile.TemporaryDirectory(prefix="hyvv-build-bake-") as tmp:
            root = _repo(tmp, commits_past=2)
            out = Path(tmp) / "install"
            written = _baker().bake(root, out)
            self.assertEqual(written.name, L.BUILD_INFO_NAME)
            record = json.loads(written.read_text("utf-8"))
            short = _git(root, "rev-parse", "--short=7", "HEAD")
            self.assertEqual(record["describe"], f"{TAG}-2-g{short}")
            self.assertEqual(record["version"], f"{L.VERSION}+2.g{short}")
            self.assertEqual(record["sha"], _git(root, "rev-parse", "HEAD"))
            ident = L.build_identity(root=out)  # no .git: the baked record is the only identity
            self.assertEqual(ident["source"], "baked")
            self.assertEqual(ident["version"], record["version"])

    def test_bake_refuses_a_tree_with_no_git_identity(self):
        with tempfile.TemporaryDirectory(prefix="hyvv-build-nogit-") as tmp:
            with self.assertRaises(SystemExit):
                _baker().bake(Path(tmp), Path(tmp) / "install")


if __name__ == "__main__":
    unittest.main()
