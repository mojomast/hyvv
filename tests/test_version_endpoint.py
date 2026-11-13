"""GET /v1/version returns the package version and the /v1 contract version."""
from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)
TOKEN = "t" * 40
SCHEMA = json.loads((HERE / "contract/v1/schemas/version.json").read_text(encoding="utf-8"))


def _schema_errors(inst):
    errs = [f"missing {k}" for k in SCHEMA.get("required", []) if k not in inst]
    props = SCHEMA.get("properties", {})
    if SCHEMA.get("additionalProperties") is False:
        errs += [f"extra {k}" for k in inst if k not in props]
    for k, v in inst.items():
        p = props.get(k) or {}
        if "const" in p and v != p["const"]:
            errs.append(f"const {k}")
        if p.get("type") == "string" and not isinstance(v, str):
            errs.append(f"type {k}")
        if p.get("type") == "array" and not isinstance(v, list):
            errs.append(f"type {k}")
        if "enum" in (p.get("items") or {}) and isinstance(v, list):
            allowed = set(p["items"]["enum"])
            errs += [f"enum {x}" for x in v if x not in allowed]
    return errs


class VersionEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        tmp = tempfile.mkdtemp(prefix="hyvv-version-")
        for n in ("go.key", "deepseek.key"):
            (Path(tmp) / n).write_text("fake-key-not-real", encoding="utf-8")
        cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": tmp, "backoff_s": [0.2],
               "model": "deepseek-v4.1-flash", "base_url": "http://x", "wire": "openai",
               "image": "cua-bee:test", "runtime": "runc", "memory_mb": 512, "projects": {}}
        cls.la = L.Launcher(cfg, L.FakeBackend(cfg))
        threading.Thread(target=cls.la.loop.run_forever, daemon=True).start()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(cls.la, TOKEN))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _req(self, path="/v1/version", token=TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        c.request("GET", path, headers=headers)
        r = c.getresponse()
        raw = r.read()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        return r.status, body

    def test_version_returns_package_and_contract_versions(self):
        st, b = self._req()
        self.assertEqual(st, 200)
        self.assertEqual(_schema_errors(b), [])
        self.assertEqual(b["api_version"], "v1")
        self.assertEqual(b["version"], L.VERSION)
        from hyvv import __version__
        self.assertEqual(b["version"], __version__)
        # Honest advertisement: only capabilities the launcher actually has.
        caps = set(b["capabilities"])
        self.assertTrue({"priority_routing", "go_fallback"} <= caps)
        self.assertNotIn("leases", caps)

    def test_version_requires_bearer_token(self):
        st, b = self._req(token=None)
        self.assertEqual(st, 401)
        self.assertIsInstance(b.get("error"), str)
