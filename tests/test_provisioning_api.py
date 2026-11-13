"""HTTP-level provisioning tests against the launcher handler.

No launcher process is started: the handler is mounted on a stub Launcher, which
is enough to prove routing, auth, duplicate-request behaviour and durable
registration without a restart.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

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
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture()
def service(tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({
        "schema": P.SCHEMA,
        "profiles": {"smoke": {"repo_patterns": ["mojomast/rizzstack-smoke"], "workspace_root": str(root), "allow_cleanup": True}},
    }))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"projects": {"existing": {"repo": "mojomast/workflow", "path": str(tmp_path / "existing")}}}))
    cfg = {"projects": {"existing": {"repo": "mojomast/workflow", "path": str(tmp_path / "existing")}},
           "_config_path": str(config_path),
           "provisioning": {"enabled": True, "policy": str(policy_path), "journal": str(tmp_path / "journal"),
                            "credential_ready_repos": ["mojomast/rizzstack-smoke"]}}

    svc = launcher.build_provisioner(cfg)
    svc.git = FakeGit()
    stub = SimpleNamespace(cfg=cfg, attempts={}, provisioner=svc)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), launcher.make_handler(stub, TOKEN))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", cfg, config_path, svc
    httpd.shutdown()
    httpd.server_close()


def post(url: str, path: str, body: dict, token: str = TOKEN):
    request = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def request(**overrides):
    body = {"repo": "mojomast/rizzstack-smoke", "source_rev": SHA, "profile": "smoke",
            "job_id": "job_smoke", "idempotency_key": "prov_1", "project": "rizzstack-smoke"}
    body.update(overrides)
    return body


class Routes:
    __test__ = True

    def test_provision_registers_durably_without_a_restart(self, service):
        url, cfg, config_path, svc = service

        status, receipt = post(url, "/v1/provision/project", request())

        assert status == 200
        assert receipt["status"] == "ready"
        assert receipt["resolved_revision"] == SHA
        # Live registry (what dispatch reads) and the file on disk both have it.
        assert cfg["projects"]["rizzstack-smoke"]["source_rev"] == SHA
        on_disk = json.loads(config_path.read_text())
        assert on_disk["projects"]["rizzstack-smoke"]["source_rev"] == SHA
        assert on_disk["projects"]["existing"]["repo"] == "mojomast/workflow"  # untouched
        # Simulate a restart: a fresh provisioner built from the same file sees it.
        cfg2 = dict(cfg, projects=dict(on_disk["projects"]))
        svc2 = launcher.build_provisioner(cfg2)
        assert "rizzstack-smoke" in svc2.projects

    def test_unauthenticated_requests_are_refused(self, service):
        url, cfg, config_path, svc = service
        status, _ = post(url, "/v1/provision/project", request(), token="wrong")
        assert status == 401
        assert "rizzstack-smoke" not in cfg["projects"]

    def test_duplicate_concurrent_requests_create_one_fixture(self, service):
        url, cfg, config_path, svc = service
        statuses = []

        def attempt():
            statuses.append(post(url, "/v1/provision/project", request())[0])

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        clones = [c for c in svc.git.calls if c[0] == "clone"]
        assert len(clones) == 1, f"expected one clone, saw {len(clones)}"
        assert set(statuses) <= {200, 409}

    def test_a_traversal_project_name_is_refused(self, service):
        url, cfg, config_path, svc = service
        status, body = post(url, "/v1/provision/project", request(project="../escape"))

        assert status == 400
        assert body["error"] in {"invalid_project_name", "path_escape"}
        assert "rizzstack-smoke" not in cfg["projects"]

    def test_a_repository_outside_the_policy_is_refused(self, service):
        url, cfg, config_path, svc = service
        status, body = post(url, "/v1/provision/project", request(repo="attacker/evil"))
        assert status == 400
        assert body["error"] == "repo_not_allowed"

    def test_cleanup_removes_registration_and_workspace(self, service):
        url, cfg, config_path, svc = service
        post(url, "/v1/provision/project", request())
        workspace = Path(cfg["projects"]["rizzstack-smoke"]["path"])

        status, result = post(url, "/v1/provision/cleanup", {"project": "rizzstack-smoke"})

        assert status == 200
        assert result["state"] == "cleaned"
        assert "rizzstack-smoke" not in cfg["projects"]
        assert not workspace.exists()
