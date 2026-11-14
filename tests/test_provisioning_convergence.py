"""Provisioning convergence tests: dirty states the v1 tests never created."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import provisioning as P

SHA_A = "a" * 40


def git(args, cwd, check=True):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if check and done.returncode != 0:
        raise AssertionError(f"git {args} failed: {done.stderr}")
    return done


class DirtyStateTests(unittest.TestCase):
    """Each case starts from a persisted record that disagrees with reality."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "fixtures"
        self.root.mkdir()
        self.journal = P.Journal(self.tmp / "journal")
        self.registry = P.MemoryRegistry()
        (self.tmp / "policy.json").write_text(json.dumps({
            "schema": P.SCHEMA,
            "profiles": {"smoke": {"repo_patterns": ["mojomast/smoke-*"], "workspace_root": str(self.root),
                                   "max_provisioned": 2, "ttl_hours": 1}},
        }))
        self.prov = P.Provisioner(
            policy=P.load_policy(self.tmp / "policy.json"),
            journal=self.journal,
            registry=self.registry,
            credential=lambda repo: "ok",
        )

    def request(self, **overrides):
        body = {"repo": "mojomast/smoke-one", "source_rev": SHA_A, "profile": "smoke",
                "job_id": "job_1", "idempotency_key": "k1", "project": "smoke-one"}
        body.update(overrides)
        return body

    def ready_record(self, workspace: Path, **overrides):
        record = {"schema": P.SCHEMA, "idempotency_key": "k1", "job_id": "job_1", "repo": "mojomast/smoke-one",
                  "source_rev": SHA_A, "profile": "smoke", "project": "smoke-one", "workspace": str(workspace),
                  "state": "ready", "created_at": "then",
                  "receipt": {"status": "ready", "project": "smoke-one", "repo": "mojomast/smoke-one",
                              "resolved_revision": SHA_A, "workspace": str(workspace), "profile": "smoke"}}
        record.update(overrides)
        return record

    def test_a_clean_ready_record_without_a_live_registration_is_stale(self):
        workspace = self.root / "smoke-one"
        workspace.mkdir()
        self.journal.write("k1", self.ready_record(workspace))

        with self.assertRaises(P.ProvisionError) as caught:
            self.prov.provision(self.request())

        self.assertEqual(caught.exception.code, "stale_receipt")
        record = self.journal.read("k1")
        self.assertEqual(record["state"], "uncertain")
        self.assertIn("live registry", record["detail"])

    def test_a_cleaned_record_is_never_replayed_as_ready(self):
        workspace = self.root / "smoke-one"
        self.journal.write("k1", self.ready_record(workspace, state="cleaned"))

        with self.assertRaises(P.ProvisionError) as caught:
            self.prov.provision(self.request())
        self.assertEqual(caught.exception.code, "project_retired")

    def test_cleanup_resumes_after_unregistration_but_before_deletion(self):
        workspace = self.root / "smoke-one"
        workspace.mkdir()
        (workspace / "file.txt").write_text("x")
        self.registry.set("smoke-one", {"repo": "mojomast/smoke-one", "path": str(workspace),
                                        "source_rev": SHA_A, "profile": "smoke"})
        self.journal.write("k1", self.ready_record(workspace, state="retiring"))

        result = self.prov.cleanup("smoke-one")

        self.assertEqual(result["state"], "cleaned")
        self.assertFalse(workspace.exists())
        self.assertEqual(self.journal.read("k1")["state"], "cleaned")

    def test_a_cleanup_record_survives_a_crash_before_unregistration(self):
        workspace = self.root / "smoke-one"
        workspace.mkdir()
        self.registry.set("smoke-one", {"repo": "mojomast/smoke-one", "path": str(workspace),
                                        "source_rev": SHA_A, "profile": "smoke"})
        self.journal.write("k1", self.ready_record(workspace, state="retiring"))

        result = self.prov.cleanup("smoke-one")
        self.assertEqual(result["state"], "cleaned")
        self.assertIsNone(self.registry.get("smoke-one"))

    def test_a_quota_is_enforced_for_the_profile(self):
        # Two active records already exist; the third project is refused.
        for i in range(2):
            self.journal.write(f"old{i}", {"schema": P.SCHEMA, "idempotency_key": f"old{i}",
                                           "profile": "smoke", "state": "ready", "project": f"p{i}"})
        with self.assertRaises(P.ProvisionError) as caught:
            self.prov.provision(self.request(project="smoke-three", idempotency_key="k3",
                                             repo="mojomast/smoke-three"))
        self.assertEqual(caught.exception.code, "quota_exhausted")

    def test_a_conflicting_profile_for_the_same_key_is_rejected(self):
        self.journal.write("k1", self.ready_record(self.root / "smoke-one", profile="other"))
        with self.assertRaises(P.ProvisionError) as caught:
            self.prov.provision(self.request())
        self.assertEqual(caught.exception.code, "journal_conflict")


class RealGitTests(unittest.TestCase):
    """Real repositories, real subprocesses — the paths FakeGit cannot prove."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "fixtures"
        self.root.mkdir()

        work = self.tmp / "work"
        work.mkdir()
        git(["init", "-q", "-b", "main"], work)
        (work / "README.md").write_text("base\n")
        git(["add", "-A"], work)
        git(["commit", "-q", "-m", "base"], work)
        self.sha_base = git(["rev-parse", "HEAD"], work).stdout.strip()
        git(["checkout", "-q", "-b", "side"], work)
        (work / "side.txt").write_text("side\n")
        git(["add", "-A"], work)
        git(["commit", "-q", "-m", "side"], work)
        self.sha_side = git(["rev-parse", "HEAD"], work).stdout.strip()
        git(["checkout", "-q", "main"], work)

        self.origin = self.tmp / "origin.git"
        git(["init", "-q", "--bare", str(self.origin)], self.tmp)
        git(["push", "-q", str(self.origin), "main", "side"], work)

        (self.tmp / "policy.json").write_text(json.dumps({
            "schema": P.SCHEMA,
            "profiles": {"smoke": {"repo_patterns": ["mojomast/smoke-*"], "workspace_root": str(self.root),
                                   "max_provisioned": 3, "ttl_hours": 1}},
        }))
        self.made = []

    def provisioner(self, **overrides):
        prov = P.Provisioner(
            policy=P.load_policy(self.tmp / "policy.json"),
            journal=P.Journal(self.tmp / "journal"),
            registry=P.MemoryRegistry(),
            credential=lambda repo: "ok",
            url_for=lambda repo: f"file://{self.origin}",
            **overrides,
        )
        self.made.append(prov)
        return prov

    def request(self, **overrides):
        body = {"repo": "mojomast/smoke-real", "source_rev": self.sha_side, "profile": "smoke",
                "job_id": "job_1", "idempotency_key": "k1", "project": "smoke-real"}
        body.update(overrides)
        return body

    def test_a_pinned_commit_outside_the_default_branch_is_fetched_and_checked_out(self):
        receipt = self.provisioner().provision(self.request())
        workspace = Path(receipt["workspace"])

        self.assertEqual(receipt["resolved_revision"], self.sha_side)
        self.assertEqual(git(["rev-parse", "HEAD"], workspace).stdout.strip(), self.sha_side)
        # Detached HEAD: symbolic-ref must fail.
        self.assertNotEqual(git(["symbolic-ref", "-q", "HEAD"], workspace, check=False).returncode, 0)

    def test_a_dirty_recovered_checkout_is_refused(self):
        prov = self.provisioner()
        receipt = prov.provision(self.request())
        workspace = Path(receipt["workspace"])
        (workspace / "untracked.txt").write_text("dirty\n")

        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request())
        self.assertEqual(caught.exception.code, "stale_receipt")
        self.assertIn("modifications", caught.exception.message)

    def test_a_checkout_at_the_wrong_revision_is_refused(self):
        prov = self.provisioner()
        receipt = prov.provision(self.request())
        workspace = Path(receipt["workspace"])
        git(["checkout", "-q", "--detach", self.sha_base], workspace)

        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request())
        self.assertEqual(caught.exception.code, "stale_receipt")
        self.assertIn("HEAD", caught.exception.message)

    def test_a_remote_that_is_not_the_requested_repository_is_refused(self):
        prov = self.provisioner()
        receipt = prov.provision(self.request())
        workspace = Path(receipt["workspace"])
        git(["remote", "set-url", "origin", "https://example.invalid/other/repo.git"], workspace)

        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request())
        self.assertEqual(caught.exception.code, "stale_receipt")
        self.assertIn("remote", caught.exception.message)

    def test_reprovisioning_after_cleanup_creates_a_fresh_checkout(self):
        prov = self.provisioner()
        first = prov.provision(self.request())
        prov.cleanup("smoke-real")
        self.assertFalse(Path(first["workspace"]).exists())

        second = prov.provision(self.request(idempotency_key="k2"))

        self.assertEqual(second["resolved_revision"], self.sha_side)
        self.assertTrue(Path(second["workspace"]).exists())

    def test_two_keys_for_the_same_project_cannot_both_register(self):
        prov = self.provisioner()
        prov.provision(self.request())

        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request(idempotency_key="k2", source_rev=self.sha_base))
        self.assertIn(caught.exception.code, {"journal_conflict", "project_conflict", "stale_receipt", "checkout_mismatch"})

    def test_a_killed_provisioner_does_not_wedge_its_key(self):
        _pid, script = self._holding_child()
        child = subprocess.Popen([sys.executable, str(script)], cwd=str(Path(__file__).parent.parent))
        deadline = time.time() + 15
        lock = self.tmp / "journal" / "k1.lock"
        while time.time() < deadline and not lock.exists():
            time.sleep(0.05)
        self.assertTrue(lock.exists(), "the child never acquired the lock")
        child.kill()
        child.wait(timeout=10)
        self.assertFalse(P.pid_alive(child.pid))

        receipt = self.provisioner().provision(self.request())

        self.assertEqual(receipt["status"], "ready")

    def _holding_child(self):
        script = self.tmp / "hold_lock.py"
        script.write_text(
            "import sys, time\n"
            "from pathlib import Path as _P\n"
            f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
            "import provisioning as P\n"
            f"lock = P.KeyLock(_P({str(self.tmp / 'journal')!r}), 'k1')\n"
            "lock.acquire()\n"
            "print('held', flush=True)\n"
            "time.sleep(120)\n"
        )
        return None, script


if __name__ == "__main__":
    unittest.main()
