"""Unit tests for the provisioning primitive (stdlib unittest)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import provisioning as P

ROOT = "C:\\Users\\kyle\\orbit-worker-fixtures"
SHA = "dde2aa42d09c05e43895b3120cb3312f765eb876"
OTHER = "1" * 40
REPO = "mojomast/rizzstack-smoke"


def policy(root: Path, *, allow_cleanup=True, max_provisioned=5) -> dict:
    path = root.parent / "policy.json"
    path.write_text(json.dumps({
        "schema": P.SCHEMA,
        "profiles": {"smoke": {"repo_patterns": ["mojomast/rizzstack-smoke", "mojomast/smoke-*"],
                               "workspace_root": str(root), "allow_cleanup": allow_cleanup,
                               "max_provisioned": max_provisioned, "ttl_hours": 2}},
    }))
    return P.load_policy(path)


class FakeGit:
    """Simulates git; symbolic-ref fails so the checkout reads as detached."""

    def __init__(self, *, head=SHA, remote="https://github.com/mojomast/rizzstack-smoke.git", dirty=""):
        self.calls: list[list[str]] = []
        self.head, self.remote, self.dirty = head, remote, dirty

    def __call__(self, args, cwd, timeout=300.0):
        self.calls.append(list(args))
        if args[0] == "clone":
            (Path(args[-1]) / ".git").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "-C":
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 0, self.head + "\n", "")
            if "symbolic-ref" in args:
                return subprocess.CompletedProcess(args, 1, "", "")
            if "config" in args and "--get" in args:
                return subprocess.CompletedProcess(args, 0, self.remote + "\n", "")
            if "status" in args:
                return subprocess.CompletedProcess(args, 0, self.dirty, "")
        return subprocess.CompletedProcess(args, 0, "", "")


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.tmp / "fixtures").mkdir()

    def make(self, *, git=None, credential=lambda repo: "token", allow_cleanup=True, max_provisioned=5):
        self.reg = P.MemoryRegistry()
        self.journal = P.Journal(self.tmp / "journal")
        self.git = git or FakeGit()
        self.prov = P.Provisioner(
            policy=policy(self.tmp / "fixtures", allow_cleanup=allow_cleanup, max_provisioned=max_provisioned),
            journal=self.journal,
            registry=self.reg,
            git=self.git,
            credential=credential,
        )
        return self.prov

    def request(self, **overrides):
        body = {"repo": REPO, "source_rev": SHA, "profile": "smoke", "job_id": "job_a",
                "idempotency_key": "prov_1", "project": "rizzstack-smoke"}
        body.update(overrides)
        return body


class PathTests(Harness):
    def test_unsafe_names_are_refused(self):
        for bad in ["..", "../escape", "a/b", "a\\b", "", ".", "C:evil", "con", "com1"]:
            with self.subTest(name=bad):
                self.assertFalse(P.is_safe_name(bad))
                with self.assertRaises(P.ProvisionError):
                    P.child_path(ROOT, bad)

    def test_windows_containment_rules(self):
        self.assertTrue(P._windows_contained(ROOT, ROOT + "\\rizzstack-smoke"))
        self.assertFalse(P._windows_contained(ROOT, "C:\\Windows\\System32"))
        self.assertFalse(P._windows_contained(ROOT, "D:\\fixtures\\x"))

    def test_a_symlink_inside_the_root_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        link = self.tmp / "fixtures" / "escape"
        link.symlink_to(outside)
        with self.assertRaises(P.ProvisionError) as caught:
            P.ensure_contained(self.tmp / "fixtures", link)
        self.assertEqual(caught.exception.code, "reparse_point")

    def test_a_basename_only_match_is_not_the_same_path(self):
        self.assertFalse(P.same_path("/a/fixtures/smoke", "/b/fixtures/smoke"))
        self.assertTrue(P.same_path("/a/fixtures/smoke/", "/a/fixtures/./smoke"))


class PolicyTests(Harness):
    def test_an_unlisted_repository_is_refused(self):
        prov = self.make()
        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request(repo="attacker/evil"))
        self.assertEqual(caught.exception.code, "repo_not_allowed")
        self.assertEqual(self.reg.all(), {})

    def test_a_short_sha_is_refused(self):
        prov = self.make()
        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request(source_rev="abc123"))
        self.assertEqual(caught.exception.code, "invalid_source_rev")

    def test_an_unknown_profile_is_refused(self):
        prov = self.make()
        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request(profile="prod"))
        self.assertEqual(caught.exception.code, "unknown_profile")

    def test_a_missing_credential_fails_before_cloning(self):
        prov = self.make(credential=lambda repo: None)
        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request())
        self.assertEqual(caught.exception.code, "credential_unavailable")
        self.assertFalse([c for c in self.git.calls if c[0] == "clone"])


class ProvisionTests(Harness):
    def test_clone_checks_out_the_pinned_sha_and_registers(self):
        prov = self.make()
        receipt = prov.provision(self.request())

        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(receipt["resolved_revision"], SHA)
        self.assertEqual(self.reg.get("rizzstack-smoke")["source_rev"], SHA)
        self.assertTrue(receipt["expires_at"].endswith("Z"))
        self.assertEqual(receipt["task_scopes"], [])
        checkout = [c for c in self.git.calls if "checkout" in c]
        self.assertTrue(checkout and SHA in checkout[0])
        self.assertEqual(self.journal.read("prov_1")["state"], "ready")

    def test_a_duplicate_request_reuses_the_ready_record(self):
        prov = self.make()
        first = prov.provision(self.request())
        second = prov.provision(self.request())
        self.assertEqual(second["resolved_revision"], first["resolved_revision"])
        self.assertEqual(len([c for c in self.git.calls if c[0] == "clone"]), 1)

    def test_a_crash_after_clone_before_registration_resumes(self):
        prov = self.make()
        target = self.tmp / "fixtures" / "rizzstack-smoke"
        (target / ".git").mkdir(parents=True)
        self.journal.write("prov_1", {
            "schema": P.SCHEMA, "idempotency_key": "prov_1", "job_id": "job_a", "repo": REPO,
            "source_rev": SHA, "profile": "smoke", "project": "rizzstack-smoke",
            "workspace": str(target), "state": "preparing", "created_at": "now"})

        receipt = prov.provision(self.request())

        self.assertEqual(receipt["status"], "ready")
        self.assertFalse([c for c in self.git.calls if c[0] == "clone"])
        self.assertEqual(self.reg.get("rizzstack-smoke")["source_rev"], SHA)

    def test_a_lost_registration_ack_adopts_the_existing_project(self):
        prov = self.make()
        target = self.tmp / "fixtures" / "rizzstack-smoke"
        (target / ".git").mkdir(parents=True)
        self.reg.set("rizzstack-smoke", {"repo": REPO, "path": str(target), "source_rev": SHA, "profile": "smoke"})
        self.journal.write("prov_1", {
            "schema": P.SCHEMA, "idempotency_key": "prov_1", "job_id": "job_a", "repo": REPO,
            "source_rev": SHA, "profile": "smoke", "project": "rizzstack-smoke",
            "workspace": str(target), "state": "preparing", "created_at": "now"})

        receipt = prov.provision(self.request())

        self.assertEqual(receipt["status"], "ready")
        self.assertIs(receipt.get("adopted"), True)

    def test_an_existing_project_with_the_same_basename_but_another_path_blocks(self):
        prov = self.make()
        self.reg.set("rizzstack-smoke", {"repo": REPO, "path": str(self.tmp / "other" / "rizzstack-smoke"),
                                         "source_rev": SHA, "profile": "smoke"})
        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request())
        self.assertEqual(caught.exception.code, "project_conflict")

    def test_a_dirty_checkout_is_refused_on_resume(self):
        prov = self.make(git=FakeGit(dirty=" M smoke/answer.py\n"))
        target = self.tmp / "fixtures" / "rizzstack-smoke"
        (target / ".git").mkdir(parents=True)
        self.journal.write("prov_1", {
            "schema": P.SCHEMA, "idempotency_key": "prov_1", "job_id": "job_a", "repo": REPO,
            "source_rev": SHA, "profile": "smoke", "project": "rizzstack-smoke",
            "workspace": str(target), "state": "preparing", "created_at": "now"})
        with self.assertRaises(P.ProvisionError) as caught:
            prov.provision(self.request())
        self.assertEqual(caught.exception.code, "checkout_dirty")


class CleanupTests(Harness):
    def ready(self, *, allow_cleanup=True):
        prov = self.make(allow_cleanup=allow_cleanup)
        receipt = prov.provision(self.request())
        return prov, Path(receipt["workspace"])

    def test_cleanup_removes_the_workspace_and_the_registration(self):
        prov, workspace = self.ready()
        result = prov.cleanup("rizzstack-smoke")
        self.assertEqual(result["state"], "cleaned")
        self.assertIsNone(self.reg.get("rizzstack-smoke"))
        self.assertFalse(workspace.exists())

    def test_cleanup_refuses_a_project_with_active_attempts(self):
        prov, _ = self.ready()
        with self.assertRaises(P.ProvisionError) as caught:
            prov.cleanup("rizzstack-smoke", active_attempts=lambda name: 1)
        self.assertEqual(caught.exception.code, "project_in_use")
        self.assertIsNotNone(self.reg.get("rizzstack-smoke"))

    def test_cleanup_refuses_outside_the_root(self):
        prov, _ = self.ready()
        entry = dict(self.reg.get("rizzstack-smoke"))
        entry["path"] = str(self.tmp / "elsewhere")
        self.reg.cache["rizzstack-smoke"] = entry
        with self.assertRaises(P.ProvisionError) as caught:
            prov.cleanup("rizzstack-smoke")
        self.assertEqual(caught.exception.code, "path_escape")

    def test_cleanup_is_refused_when_the_profile_disallows_it(self):
        prov, _ = self.ready(allow_cleanup=False)
        with self.assertRaises(P.ProvisionError) as caught:
            prov.cleanup("rizzstack-smoke")
        self.assertEqual(caught.exception.code, "cleanup_not_allowed")


if __name__ == "__main__":
    unittest.main()
