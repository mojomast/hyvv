"""HTTP-level provisioning tests against the launcher handler (stdlib unittest)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import launcher
import provisioning as P

SHA = "dde2aa42d09c05e43895b3120cb3312f765eb876"
TOKEN = "test-token"


class FakeGit:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, args, cwd, timeout=300.0):
        with self.lock:
            self.calls.append(list(args))
        if args[0] == "clone":
            target = Path(args[-1])
            (target / ".git").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "-C":
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 0, SHA + "\n", "")
            if "config" in args:
                return subprocess.CompletedProcess(args, 0, "https://github.com/mojomast/rizzstack-smoke.git\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        (tmp / "fixtures").mkdir()
        policy_path = tmp / "policy.json"
        policy_path.write_text(json.dumps({
            "schema": P.SCHEMA,
            "profiles": {"smoke": {"repo_patterns": ["mojomast/rizzstack-smoke"], "workspace_root": str(tmp / "fixtures"), "allow_cleanup": True}},
        }))
        self.config_path = tmp / "config.json"
        existing = {"repo": "mojomast/workflow", "path": str(tmp / "existing")}
        self.config_path.write_text(json.dumps({"projects": {"existing": existing}}))
        self.cfg = {
            "projects": {"existing": dict(existing)},
            "_config_path": str(self.config_path),
            "provisioning": {"enabled": True, "policy": str(policy_path), "journal": str(tmp / "journal"),
                             "credential_ready_repos": ["mojomast/rizzstack-smoke"]},
        }
        self.svc = launcher.build_provisioner(self.cfg)
        self.svc.git = FakeGit()
        stub = SimpleNamespace(cfg=self.cfg, attempts={}, provisioner=self.svc)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), launcher.make_handler(stub, TOKEN))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, path, body, token=TOKEN):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def request(self, **overrides):
        body = {"repo": "mojomast/rizzstack-smoke", "source_rev": SHA, "profile": "smoke",
                "job_id": "job_smoke", "idempotency_key": "prov_1", "project": "rizzstack-smoke"}
        body.update(overrides)
        return body

    def test_provision_registers_durably_without_a_restart(self):
        status, receipt = self.post("/v1/provision/project", self.request())

        self.assertEqual(status, 200)
        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(receipt["resolved_revision"], SHA)
        self.assertEqual(self.cfg["projects"]["rizzstack-smoke"]["source_rev"], SHA)
        on_disk = json.loads(self.config_path.read_text())
        self.assertEqual(on_disk["projects"]["rizzstack-smoke"]["source_rev"], SHA)
        self.assertEqual(on_disk["projects"]["existing"]["repo"], "mojomast/workflow")
        # A fresh process built from the same file sees the registration.
        fresh = launcher.build_provisioner(dict(self.cfg, projects=dict(on_disk["projects"])))
        self.assertIn("rizzstack-smoke", fresh.projects)

    def test_unauthenticated_requests_are_refused(self):
        status, _ = self.post("/v1/provision/project", self.request(), token="wrong")
        self.assertEqual(status, 401)
        self.assertNotIn("rizzstack-smoke", self.cfg["projects"])

    def test_duplicate_concurrent_requests_create_one_fixture(self):
        statuses = []

        def attempt():
            statuses.append(self.post("/v1/provision/project", self.request())[0])

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        clones = [c for c in self.svc.git.calls if c[0] == "clone"]
        self.assertEqual(len(clones), 1, f"expected one clone, saw {len(clones)}")
        self.assertLessEqual(set(statuses), {200, 409})

    def test_a_traversal_project_name_is_refused(self):
        status, body = self.post("/v1/provision/project", self.request(project="../escape"))
        self.assertEqual(status, 400)
        self.assertIn(body["error"], {"invalid_project_name", "path_escape"})
        self.assertNotIn("rizzstack-smoke", self.cfg["projects"])

    def test_a_repository_outside_the_policy_is_refused(self):
        status, body = self.post("/v1/provision/project", self.request(repo="attacker/evil"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "repo_not_allowed")

    def test_cleanup_removes_registration_and_workspace(self):
        self.post("/v1/provision/project", self.request())
        workspace = Path(self.cfg["projects"]["rizzstack-smoke"]["path"])

        status, result = self.post("/v1/provision/cleanup", {"project": "rizzstack-smoke"})

        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "cleaned")
        self.assertNotIn("rizzstack-smoke", self.cfg["projects"])
        self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
