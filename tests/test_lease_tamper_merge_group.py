"""The merge_group path of .github/scripts/lease_tamper.py.

A queue candidate has no pull_request payload: the head is a synthetic merge
commit that may stack several pull requests. The gate does not apply one lease
to the combined group. It proves, per pull request, that the head the group is
testing is the pull request's current head and that ``lease-tamper`` passed at
that exact sha. Any failure to establish that context is a hard failure with the
named reason ``merge_group_pr_unresolved`` -- never a neutral or skipped
conclusion, because GitHub counts a neutral required check as passing.

These cases drive the real CLI against a real temporary git repository and a
loopback stand-in for the GitHub API, so urllib, the authorization header and
the JSON handling are exercised, not mocked.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "lease_tamper.py"
REPO = "mojomast/hyvv"


class GitRepo:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "lease-tamper@example.invalid")
        self.git("config", "user.name", "Lease Tamper Queue Tests")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args], capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise AssertionError("git %s failed: %s\n%s" % (" ".join(args), proc.stdout, proc.stderr))
        return proc

    def write(self, relative, text):
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.rev()

    def rev(self, ref="HEAD"):
        return self.git("rev-parse", ref).stdout.strip()


class FakeGitHubApi:
    """Serves the pull, commit-pulls and check-runs endpoints over real HTTP."""

    def __init__(self):
        self.pulls = {}
        self.commit_pulls = {}
        self.check_runs = {}
        self.status = {}
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                status, payload = api.respond(self.path)
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def add_check_run(self, sha, conclusion="success"):
        self.check_runs.setdefault(sha, []).append(
            {"name": "lease-tamper", "head_sha": sha, "status": "completed", "conclusion": conclusion}
        )

    def respond(self, path):
        clean = (path or "").split("?")[0]
        if clean in self.status:
            return self.status[clean], {"message": "forced by the test"}
        parts = clean.strip("/").split("/")
        if len(parts) < 5 or parts[0] != "repos":
            return 404, {"message": "no route for " + clean}
        if parts[3] == "pulls":
            number = int(parts[4]) if len(parts) == 5 else 0
            if number in self.pulls:
                return 200, self.pulls[number]
            return 404, {"message": "Not Found"}
        if parts[3] == "commits" and len(parts) == 6:
            return self._commit(parts[4], parts[5])
        return 404, {"message": "no route for " + clean}

    def _commit(self, sha, kind):
        if kind == "pulls":
            return 200, self.commit_pulls.get(sha, [])
        if kind == "check-runs":
            runs = self.check_runs.get(sha, [])
            return 200, {"total_count": len(runs), "check_runs": runs}
        return 404, {"message": "bad commit route"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MergeGroupGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hyvv-lease-queue-"))
        self.api = FakeGitHubApi()

    def tearDown(self):
        self.api.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build_stack(self, numbers=(69,), green=(), head_ref=None):
        """Build the merge chain a queue produces, stacking ``numbers`` in order."""
        repo = GitRepo(self.tmp / "queue")
        repo.write("README.md", "base\n")
        base = repo.commit("base")
        current = base
        entries = []
        for number in numbers:
            repo.git("checkout", "-b", "pr-%d" % number, current)
            repo.write("src/slice_%d.py" % number, "implementation %d\n" % number)
            pr_head = repo.commit("implement #%d" % number)
            pull = {
                "number": number,
                "head": {"sha": pr_head},
                "base": {"sha": current},
                "labels": [],
                "body": "",
            }
            self.api.pulls[number] = pull
            self.api.commit_pulls[pr_head] = [pull]
            if number in green:
                self.api.add_check_run(pr_head)
            entries.append({"number": number, "head": pr_head})
            current = pr_head

        repo.git("checkout", "main")
        repo.write("README.md", "main moved after the pull requests opened\n")
        repo.commit("main moves on")
        base_tip = repo.rev()
        for entry in entries:
            repo.git("merge", "--no-ff", "-m", "merge group entry #%d" % entry["number"], entry["head"])
        head = repo.rev()

        if head_ref is None:
            head_ref = "refs/heads/gh-readonly-queue/main/pr-%d-%s" % (numbers[-1], head)
        event = self.tmp / "merge-group.json"
        event.write_text(
            json.dumps(
                {
                    "merge_group": {
                        "head_sha": head,
                        "head_ref": head_ref,
                        "base_sha": base_tip,
                        "base_ref": "refs/heads/main",
                    }
                }
            ),
            encoding="utf-8",
        )
        return types.SimpleNamespace(repo=repo, event=event, head=head, base=base_tip, entries=entries)

    def run_gate(self, queue, token="test-token"):
        env = dict(os.environ)
        env.pop("GITHUB_EVENT_PATH", None)
        env.pop("GH_TOKEN", None)
        env["GITHUB_EVENT_NAME"] = "merge_group"
        env["GITHUB_REPOSITORY"] = REPO
        env["GITHUB_API_URL"] = self.api.url
        if token is None:
            env.pop("GITHUB_TOKEN", None)
        else:
            env["GITHUB_TOKEN"] = token
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(queue.repo.path), str(queue.event)],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_single_pr_group_passes(self):
        queue = self.build_stack(numbers=(69,), green=(69,))

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PR #69 head %s" % queue.entries[0]["head"][:12], proc.stdout)
        self.assertIn("lease-tamper: OK: 1 pull request(s)", proc.stdout)

    def test_stacked_group_checks_every_pull_request(self):
        queue = self.build_stack(numbers=(69, 70), green=(69, 70))

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PR #69", proc.stdout)
        self.assertIn("PR #70", proc.stdout)
        self.assertIn("lease-tamper: OK: 2 pull request(s)", proc.stdout)

    def test_stacked_group_fails_when_an_earlier_entry_is_not_green(self):
        queue = self.build_stack(numbers=(69, 70), green=(70,))

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)
        self.assertIn("[not-green-at-head] pull request #69", proc.stdout)

    def test_unresolvable_branch_name_fails_merge_group_pr_unresolved(self):
        queue = self.build_stack(
            numbers=(69,), green=(69,), head_ref="refs/heads/gh-readonly-queue/main/candidate-x"
        )

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)
        self.assertIn("[no-pr-in-ref]", proc.stdout)
        self.assertNotIn("SKIPPED", proc.stdout)
        self.assertNotIn("::notice", proc.stdout)

    def test_pull_request_head_moved_after_its_pull_request_run_fails(self):
        queue = self.build_stack(numbers=(69,), green=(69,))
        queue.repo.git("checkout", "pr-69")
        queue.repo.write("src/slice_69.py", "pushed after the gate ran\n")
        moved = queue.repo.commit("push after the gate ran")
        self.api.pulls[69]["head"]["sha"] = moved

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("[head-moved]", proc.stdout)
        self.assertIn("pull request #69", proc.stdout)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)

    def test_missing_token_fails_merge_group_pr_unresolved(self):
        queue = self.build_stack(numbers=(69,), green=(69,))

        proc = self.run_gate(queue, token=None)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("[no-credentials]", proc.stdout)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)


if __name__ == "__main__":
    unittest.main()
