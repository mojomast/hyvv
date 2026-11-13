"""Worker-side provisioning tests.

Git is injected, so these run hermetically. Windows path semantics are exercised
with PureWindowsPath helpers (the real host is Windows; the containment rules are
the same code the host runs).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PureWindowsPath

import pytest

import provisioning as P

ROOT = "C:\\Users\\kyle\\orbit-worker-fixtures"
SHA = "dde2aa42d09c05e43895b3120cb3312f765eb876"
OTHER = "1" * 40
REPO = "mojomast/rizzstack-smoke"


def policy(root: Path, *, repo_patterns=None, allow_cleanup=True, max_provisioned=5) -> dict:
    raw = {
        "schema": P.SCHEMA,
        "profiles": {
            "smoke": {
                "repo_patterns": repo_patterns or ["mojomast/rizzstack-smoke", "mojomast/smoke-*"],
                "workspace_root": str(root),
                "allow_cleanup": allow_cleanup,
                "max_provisioned": max_provisioned,
                "ttl_hours": 2,
            }
        },
    }
    path = root.parent / "policy.json"
    path.write_text(json.dumps(raw))
    return P.load_policy(path)


class FakeGit:
    """Simulates clone/checkout against a temp directory, recording every call."""

    def __init__(self, *, head=SHA, remote="https://github.com/mojomast/rizzstack-smoke.git", dirty="", fail=None):
        self.calls: list[list[str]] = []
        self.head = head
        self.remote = remote
        self.dirty = dirty
        self.fail = fail or set()

    def __call__(self, args, cwd, timeout=300.0):
        self.calls.append(list(args))
        if args[0] == "clone":
            target = Path(args[-1])
            (target / ".git").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if "clone" in self.fail:
            return subprocess.CompletedProcess(args, 1, "", "fatal: could not read repository")
        if args[0] == "-C":
            repo = Path(args[1])
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 0, self.head + "\n", "")
            if "config" in args and "--get" in args:
                return subprocess.CompletedProcess(args, 0, self.remote + "\n", "")
            if "status" in args:
                return subprocess.CompletedProcess(args, 0, self.dirty, "")
            if "checkout" in args or "fetch" in args:
                return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def make(tmp_path, *, git=None, credential=lambda repo: "token", persist=None, allow_cleanup=True):
    registry: dict = {}
    journal = P.Journal(tmp_path / "journal")
    policy_map = policy(tmp_path / "fixtures", allow_cleanup=allow_cleanup)
    saved: list[dict] = []

    def default_persist(projects):
        saved.append(dict(projects))

    prov = P.Provisioner(
        policy=policy_map,
        journal=journal,
        projects=registry,
        persist_registry=persist or default_persist,
        git=git or FakeGit(),
        credential=credential,
    )
    return prov, registry, saved, journal


def request(**overrides) -> dict:
    body = {
        "repo": REPO,
        "source_rev": SHA,
        "profile": "smoke",
        "job_id": "job_a",
        "idempotency_key": "prov_1",
        "project": "rizzstack-smoke",
    }
    body.update(overrides)
    return body


class Paths:
    __test__ = True

    @pytest.mark.parametrize("bad", ["..", "../escape", "a/b", "a\\b", "", ".", "nul\x00"])
    def test_unsafe_names_are_refused(self, bad):
        with pytest.raises(P.ProvisionError):
            P.child_path(ROOT, bad)

    def test_windows_containment_rules(self):
        assert P._windows_contained(ROOT, ROOT + "\\rizzstack-smoke")
        assert not P._windows_contained(ROOT, "C:\\Windows\\System32")
        assert not P._windows_contained(ROOT, ROOT + "..\\escape")
        assert not P._windows_contained(ROOT, "D:\\fixtures\\x")

    @pytest.mark.parametrize("name", ["a\\b", "a/b", "..", "C:evil", "con", ""])
    def test_windows_unsafe_names_are_rejected(self, name):
        assert not P.is_safe_name(name)

    def test_a_symlink_inside_the_root_is_refused(self, tmp_path):
        root = tmp_path / "fixtures"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "escape"
        link.symlink_to(outside)

        with pytest.raises(P.ProvisionError) as exc:
            P.ensure_contained(root, link)
        assert exc.value.code == "reparse_point"


class Policy:
    __test__ = True

    def test_an_unlisted_repository_is_refused(self, tmp_path):
        prov, registry, saved, _ = make(tmp_path)

        with pytest.raises(P.ProvisionError) as exc:
            prov.provision(request(repo="attacker/evil"))
        assert exc.value.code == "repo_not_allowed"
        assert registry == {} and saved == []

    def test_a_short_sha_is_refused(self, tmp_path):
        prov, *_ = make(tmp_path)
        with pytest.raises(P.ProvisionError) as exc:
            prov.provision(request(source_rev="abc123"))
        assert exc.value.code == "invalid_source_rev"

    def test_an_unknown_profile_is_refused(self, tmp_path):
        prov, *_ = make(tmp_path)
        with pytest.raises(P.ProvisionError) as exc:
            prov.provision(request(profile="prod"))
        assert exc.value.code == "unknown_profile"

    def test_a_missing_credential_fails_before_cloning(self, tmp_path):
        git = FakeGit()
        prov, registry, saved, _ = make(tmp_path, git=git, credential=lambda repo: None)

        with pytest.raises(P.ProvisionError) as exc:
            prov.provision(request())
        assert exc.value.code == "credential_unavailable"
        assert not [c for c in git.calls if c[0] == "clone"]
        assert registry == {}


class Provisioning:
    __test__ = True

    def test_clone_checks_out_the_exact_pinned_sha_and_registers(self, tmp_path):
        git = FakeGit()
        prov, registry, saved, journal = make(tmp_path, git=git)

        receipt = prov.provision(request())

        assert receipt["status"] == "ready"
        assert receipt["project"] == "rizzstack-smoke"
        assert receipt["resolved_revision"] == SHA
        assert registry["rizzstack-smoke"]["source_rev"] == SHA
        assert saved and saved[-1]["rizzstack-smoke"]["path"].endswith("rizzstack-smoke")
        checkout = [c for c in git.calls if "checkout" in c]
        assert checkout and SHA in checkout[0]
        assert journal.read("prov_1")["state"] == "ready"

    def test_a_duplicate_request_reuses_the_ready_record(self, tmp_path):
        git = FakeGit()
        prov, registry, saved, _ = make(tmp_path, git=git)

        first = prov.provision(request())
        second = prov.provision(request())

        assert second["resolved_revision"] == first["resolved_revision"]
        assert len([c for c in git.calls if c[0] == "clone"]) == 1
        assert len(saved) == 1

    def test_a_crash_after_clone_before_registration_resumes(self, tmp_path):
        git = FakeGit()
        prov, registry, saved, journal = make(tmp_path, git=git)

        # Simulate the crash: workspace exists and is correct, journal says preparing.
        target = Path(str(P.child_path(str(tmp_path / "fixtures"), "rizzstack-smoke")))
        (target / ".git").mkdir(parents=True)
        journal.write("prov_1", {
            "schema": P.SCHEMA, "idempotency_key": "prov_1", "job_id": "job_a", "repo": REPO,
            "source_rev": SHA, "profile": "smoke", "project": "rizzstack-smoke",
            "workspace": str(target), "state": "preparing", "created_at": "now",
        })

        receipt = prov.provision(request())

        assert receipt["status"] == "ready"
        assert not [c for c in git.calls if c[0] == "clone"]
        assert registry["rizzstack-smoke"]["source_rev"] == SHA

    def test_a_lost_registration_ack_adopts_the_existing_project(self, tmp_path):
        git = FakeGit()
        prov, registry, saved, journal = make(tmp_path, git=git)
        target = Path(str(P.child_path(str(tmp_path / "fixtures"), "rizzstack-smoke")))
        (target / ".git").mkdir(parents=True)
        registry["rizzstack-smoke"] = {"repo": REPO, "path": str(target), "source_rev": SHA, "profile": "smoke"}
        journal.write("prov_1", {
            "schema": P.SCHEMA, "idempotency_key": "prov_1", "job_id": "job_a", "repo": REPO,
            "source_rev": SHA, "profile": "smoke", "project": "rizzstack-smoke",
            "workspace": str(target), "state": "preparing", "created_at": "now",
        })

        receipt = prov.provision(request())

        assert receipt["status"] == "ready"
        assert receipt.get("adopted") is True
        assert len(saved) == 0  # nothing new was registered

    def test_an_existing_project_for_another_repo_blocks(self, tmp_path):
        git = FakeGit()
        prov, registry, saved, _ = make(tmp_path, git=git)
        registry["rizzstack-smoke"] = {"repo": "mojomast/other", "path": "C:\\Users\\kyle\\orbit-worker-fixtures\\rizzstack-smoke"}

        with pytest.raises(P.ProvisionError) as exc:
            prov.provision(request())
        assert exc.value.code == "project_conflict"

    def test_a_dirty_checkout_is_refused_on_resume(self, tmp_path):
        git = FakeGit(dirty=" M smoke/answer.py\n")
        prov, registry, saved, journal = make(tmp_path, git=git)
        target = Path(str(P.child_path(str(tmp_path / "fixtures"), "rizzstack-smoke")))
        (target / ".git").mkdir(parents=True)
        journal.write("prov_1", {
            "schema": P.SCHEMA, "idempotency_key": "prov_1", "job_id": "job_a", "repo": REPO,
            "source_rev": SHA, "profile": "smoke", "project": "rizzstack-smoke",
            "workspace": str(target), "state": "preparing", "created_at": "now",
        })

        with pytest.raises(P.ProvisionError) as exc:
            prov.provision(request())
        assert exc.value.code == "checkout_mismatch"
        assert registry == {}

    def test_a_wrong_head_is_refused_on_resume(self, tmp_path):
        git = FakeGit(head=OTHER)
        prov, registry, saved, journal = make(tmp_path, git=git)
        target = Path(str(P.child_path(str(tmp_path / "fixtures"), "rizzstack-smoke")))
        (target / ".git").mkdir(parents=True)
        journal.write("prov_1", {
            "schema": P.SCHEMA, "idempotency_key": "prov_1", "job_id": "job_a", "repo": REPO,
            "source_rev": SHA, "profile": "smoke", "project": "rizzstack-smoke",
            "workspace": str(target), "state": "preparing", "created_at": "now",
        })

        with pytest.raises(P.ProvisionError) as exc:
            prov.provision(request())
        assert exc.value.code == "checkout_mismatch"


class Cleanup:
    __test__ = True

    def _ready(self, tmp_path, *, allow_cleanup=True):
        git = FakeGit()
        prov, registry, saved, _ = make(tmp_path, git=git, allow_cleanup=allow_cleanup)
        receipt = prov.provision(request())
        return prov, registry, Path(receipt["workspace"])

    def test_cleanup_removes_the_workspace_and_the_registration(self, tmp_path):
        prov, registry, workspace = self._ready(tmp_path)

        result = prov.cleanup("rizzstack-smoke")

        assert result["state"] == "cleaned"
        assert "rizzstack-smoke" not in registry
        assert not workspace.exists()

    def test_cleanup_refuses_a_project_with_active_attempts(self, tmp_path):
        prov, registry, workspace = self._ready(tmp_path)

        with pytest.raises(P.ProvisionError) as exc:
            prov.cleanup("rizzstack-smoke", active_attempts=lambda name: 1)
        assert exc.value.code == "project_in_use"
        assert registry

    def test_cleanup_refuses_outside_the_root(self, tmp_path):
        prov, registry, workspace = self._ready(tmp_path)
        registry["rizzstack-smoke"]["path"] = str(tmp_path / "elsewhere")

        with pytest.raises(P.ProvisionError) as exc:
            prov.cleanup("rizzstack-smoke")
        assert exc.value.code == "path_escape"

    def test_cleanup_is_refused_when_the_profile_disallows_it(self, tmp_path):
        prov, registry, workspace = self._ready(tmp_path, allow_cleanup=False)
        with pytest.raises(P.ProvisionError) as exc:
            prov.cleanup("rizzstack-smoke")
        assert exc.value.code == "cleanup_not_allowed"
