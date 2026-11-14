"""Real-Git workspace/collection regressions, with a local sandbox transport.

The controller tests execute the production finish/failed-event path. Only the
paid SDK/model boundary is replaced; source export, Git, collection and journals
are real. No deployed worker or provider credentials are needed.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_acceptance_class import L, _cfg, _local_backend, _src_repo


class WorkspaceEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hyvv-workspace-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.be, self.env = _local_backend(self.tmp.name)
        self.src = _src_repo(self.tmp.name)
        self.rev = self.git(self.src, "rev-parse", "HEAD")
        self.be.cfg = _cfg(self.tmp.name, projects={"proj": {"path": str(self.src)}})

    def git(self, wd, *args):
        return subprocess.check_output(
            ["git", "-C", str(wd), "-c", "user.name=bee", "-c", "user.email=bee@example.invalid", *args],
            env=self.env, stderr=subprocess.PIPE, text=True,
        ).strip()

    def seed(self):
        wd, rev, seed = asyncio.run(self.be._seed_project(
            object(), {"path": str(self.src)}, "proj", source_rev=self.rev))
        self.assertEqual(rev, self.rev)
        self.assertEqual(self.git(wd, "rev-parse", seed + "^{tree}"),
                         self.git(self.src, "rev-parse", self.rev + "^{tree}"))
        return Path(wd), seed

    def attempt(self, wd, seed):
        return {"attempt_id": "att_0000000001", "kind": "opencode", "sandbox": "fixture",
                "workdir": str(wd), "seed_commit": seed, "local_base_rev": self.rev,
                "brief": {"objective": "add b", "project": "proj", "source_rev": self.rev,
                          "assigned_files": ["b.txt"]}}

    def collect(self, wd, seed):
        return asyncio.run(self.be.patch(self.attempt(wd, seed)))

    def assert_collection_error(self, wd, seed, stage):
        with self.assertRaises(L.WorkspaceError) as caught:
            self.collect(wd, seed)
        self.assertEqual(caught.exception.code, "patch_collection_failed")
        self.assertEqual(caught.exception.stage, stage)
        return caught.exception

    def test_non_git_workspace_fails_instead_of_returning_an_empty_patch(self):
        wd, seed = self.seed()
        shutil.rmtree(wd / ".git")
        (wd / "b.txt").write_text("two\n")
        error = self.assert_collection_error(wd, seed, "workspace")
        self.assertNotEqual(error.exit_code, 0)

    def test_missing_seed_object_fails(self):
        wd, _ = self.seed()
        self.assert_collection_error(wd, "0" * 40, "workspace")

    def test_missing_seed_record_never_falls_back_to_current_head(self):
        wd, _ = self.seed()
        self.assert_collection_error(wd, None, "seed")

    def test_collector_cwd_must_be_the_repository_root(self):
        wd, seed = self.seed()
        nested = wd / "wrong-cwd"
        nested.mkdir()
        self.assert_collection_error(nested, seed, "workspace")

    def test_linked_worktree_with_git_file_is_supported(self):
        wd, seed = self.seed()
        linked = self.root / "linked"
        self.git(wd, "worktree", "add", "--detach", str(linked), seed)
        self.assertTrue((linked / ".git").is_file())
        (linked / "b.txt").write_text("two\n")
        patch = self.collect(linked, seed)
        self.assertEqual([row["path"] for row in patch["files"]], ["b.txt"])

    def test_empty_source_commit_can_be_seeded(self):
        self.git(self.src, "rm", "a.txt")
        self.git(self.src, "commit", "-qm", "empty source")
        self.rev = self.git(self.src, "rev-parse", "HEAD")
        wd, seed = self.seed()
        self.assertEqual(self.collect(wd, seed)["diff"], "")

    def test_each_collection_command_must_report_success(self):
        wd, seed = self.seed()
        (wd / "b.txt").write_text("two\n")
        real_sh = self.be._sh
        for fragment, stage in (("git add -A", "stage"), ("--binary", "diff"),
                                ("--name-status", "names"), ("sha256sum", "hash")):
            for status in (1, None):
                with self.subTest(stage=stage, status=status):
                    async def fail(sb, line, timeout_ms=120000):
                        if fragment in line:
                            return "", "fixture command failed", status
                        return await real_sh(sb, line, timeout_ms)
                    self.be._sh = fail
                    self.assert_collection_error(wd, seed, stage)
        self.be._sh = real_sh

    def test_quoted_filename_is_collected_without_shell_interpolation(self):
        wd, seed = self.seed()
        name = "quote' and space.txt"
        (wd / name).write_text("two\n")
        patch = self.collect(wd, seed)
        self.assertEqual([row["path"] for row in patch["files"]], [name])
        self.assertFalse(patch["files"][0]["in_assigned_scope"])

    def sdk_fixture(self):
        run = SimpleNamespace(run_id=lambda: "fixture-run")
        agents = SimpleNamespace(run=mock.AsyncMock(return_value=run))
        sandbox = SimpleNamespace(id=lambda: "fixture", agents=mock.AsyncMock(return_value=agents))
        sandboxes = SimpleNamespace(create=mock.AsyncMock(return_value=sandbox))
        self.be.client = SimpleNamespace(sandboxes=lambda: sandboxes)
        self.be.cua = SimpleNamespace(
            SandboxCreateOptions=lambda **kw: kw,
            ReadinessProbe=lambda **kw: kw,
            AgentRunOptions=lambda **kw: SimpleNamespace(**kw),
        )
        return agents, sandboxes

    def start_attempt(self):
        return {"attempt_id": "att_0000000002", "kind": "opencode", "model": "deepseek-v4.1-flash",
                "brief": {"objective": "add b", "project": "proj", "source_rev": self.rev,
                          "assigned_files": ["b.txt"]}}

    def test_model_launch_receives_verified_seeded_cwd(self):
        agents, _ = self.sdk_fixture()
        attempt = self.start_attempt()
        ids = asyncio.run(self.be.start(attempt, "fixture-key"))
        agents.run.assert_awaited_once()
        options = agents.run.await_args.args[2]
        self.assertEqual(options.cwd, ids["workdir"])
        self.assertEqual(self.git(options.cwd, "rev-parse", "HEAD"), ids["seed_commit"])
        self.assertEqual(attempt["seed_commit"], ids["seed_commit"])

    def test_missing_local_project_refuses_scoped_work_before_sdk_creation(self):
        agents, sandboxes = self.sdk_fixture()
        self.be.cfg["projects"] = {"proj": {"repo": "https://example.invalid/repo.git"}}
        with self.assertRaises(L.WorkspaceError) as caught:
            asyncio.run(self.be.start(self.start_attempt(), "fixture-key"))
        self.assertEqual(caught.exception.stage, "project")
        agents.run.assert_not_awaited()
        sandboxes.create.assert_not_awaited()

    def test_unknown_seed_command_status_prevents_model_launch(self):
        agents, _ = self.sdk_fixture()
        real_sh = self.be._sh
        async def unknown(sb, line, timeout_ms=120000):
            out, err, status = await real_sh(sb, line, timeout_ms)
            return (out, err, None) if "tar -xf" in line else (out, err, status)
        self.be._sh = unknown
        with self.assertRaises(L.WorkspaceError) as caught:
            asyncio.run(self.be.start(self.start_attempt(), "fixture-key"))
        self.assertEqual(caught.exception.stage, "seed")
        agents.run.assert_not_awaited()

    def test_failed_directory_preparation_prevents_upload_and_model_launch(self):
        agents, _ = self.sdk_fixture()
        real_sh = self.be._sh
        async def failed(sb, line, timeout_ms=120000):
            if "rm -rf" in line:
                return "", "fixture preparation denied", 1
            return await real_sh(sb, line, timeout_ms)
        self.be._sh = failed
        with self.assertRaises(L.WorkspaceError) as caught:
            asyncio.run(self.be.start(self.start_attempt(), "fixture-key"))
        self.assertEqual(caught.exception.stage, "prepare")
        agents.run.assert_not_awaited()
        self.assertFalse((self.root / "orbit-src.tar").exists())

    def control(self, wd, seed):
        self.be.environment = mock.AsyncMock(return_value={"fixture": "local shell"})
        self.be.events = mock.AsyncMock(return_value=([
            {"seq": 1, "kind": "turn_ended", "stop_reason": "end_turn"}], 1))
        self.be.result = mock.AsyncMock(return_value={"status": "completed", "error": None, "artifacts": []})
        self.be.cleanup = mock.AsyncMock()
        la = L.Launcher(self.be.cfg, self.be)
        self.addCleanup(la.loop.close)
        la.kick = lambda: None
        attempt = self.attempt(wd, seed)
        a, _ = la.submit(attempt["attempt_id"], attempt["brief"])
        a.update({key: value for key, value in attempt.items() if key != "brief"})
        a.update(run_id="fixture-run", state="running")
        asyncio.run(la._control(a))
        journal = json.loads(la._path(a["attempt_id"]).read_text())
        events = [json.loads(path.read_text()) for path in (la.dir / "events" / "outbox").glob("*.json")]
        self.assertEqual(len(events), 1)
        self.be.cleanup.assert_awaited_once()
        return la, journal, events[0]

    def test_collection_failure_is_durable_failed_attempt_and_terminal_event(self):
        wd, seed = self.seed()
        shutil.rmtree(wd / ".git")
        la, journal, event = self.control(wd, seed)
        self.assertEqual(journal["state"], "failed")
        self.assertEqual(journal["workspace_error"]["code"], "patch_collection_failed")
        self.assertEqual(la.public(journal)["workspace_error"], journal["workspace_error"])
        self.assertEqual(event["data"]["state"], "failed")
        self.assertFalse(event["data"]["result"]["available"])
        self.assertFalse(la.landed_result_path(journal).exists())

    def test_worker_committed_change_survives_successful_controller_finish(self):
        wd, seed = self.seed()
        (wd / "b.txt").write_text("two\n")
        self.git(wd, "add", "b.txt")
        self.git(wd, "commit", "-qm", "worker commit")
        la, journal, event = self.control(wd, seed)
        self.assertEqual(journal["state"], "succeeded")
        self.assertEqual(event["data"]["state"], "succeeded")
        result = json.loads(la.landed_result_path(journal).read_text())
        self.assertEqual(result["observed"]["seed_commit"], seed)
        self.assertEqual(result["patch"]["base"], seed)
        patch_file = self.root / "collected.patch"
        patch_file.write_text(result["patch"]["diff"])
        self.git(self.src, "apply", "--check", str(patch_file))

    def test_valid_no_change_remains_successful_collection(self):
        wd, seed = self.seed()
        la, journal, event = self.control(wd, seed)
        self.assertEqual(journal["state"], "succeeded")
        self.assertTrue(event["data"]["result"]["available"])
        result = json.loads(la.landed_result_path(journal).read_text())
        self.assertEqual(result["patch"], {"diff": "", "files": [], "base": seed})

    def test_structured_error_scrubs_stderr(self):
        error = L.WorkspaceError("workspace_invalid", "seed", "Bearer " + "fixture-token", 128)
        self.assertNotIn("fixture-token", json.dumps(error.to_dict()))
        self.assertEqual(error.to_dict()["exit_code"], 128)


if __name__ == "__main__":
    unittest.main()
