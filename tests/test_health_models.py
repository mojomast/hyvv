"""GET /v1/health advertises the launcher's allow-listed models.

A coordinator validates a brief's model against the health payload before it dispatches (the
bundlebees adapter refuses one the payload omits), so a single `model` field made every other
allow-listed model undispatchable -- including a model the operator added in `config.json`.
"""
import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules.setdefault("launcher", L)
spec.loader.exec_module(L)
TOKEN = "t" * 40


def serve(tmp, model="deepseek:deepseek-flash", models=None):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": tmp, "projects": {},
           "model": model, "base_url": L.DEEPSEEK_BASE_URL, "wire": "openai-chat", "image": "img",
           "models": models or {}}
    la = L.Launcher(cfg, L.FakeBackend(cfg))
    threading.Thread(target=la.loop.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(la, TOKEN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return la, srv


def get(srv, path):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request("GET", path, headers={"Authorization": "Bearer " + TOKEN})
    r = c.getresponse()
    return r.status, json.loads(r.read())


class HealthModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lch-health-")

    def test_builtin_allow_list_is_advertised(self):
        la, srv = serve(self.tmp)
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(st, 200)
            self.assertIn("zoo:glm-5.3-flash", body["models"])
            self.assertIn("deepseek:deepseek-flash", body["models"])
            self.assertIn("go:deepseek-v4.1-flash", body["models"])
        finally:
            srv.shutdown()

    def test_default_model_is_still_reported_and_present(self):
        la, srv = serve(self.tmp)
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(body["model"], "deepseek:deepseek-flash")
            self.assertIn(body["model"], body["models"])
        finally:
            srv.shutdown()

    def test_a_config_only_model_is_advertised(self):
        la, srv = serve(self.tmp, models={"ops:local": {"model": "ops/local", "key": "none"}})
        try:
            st, body = get(srv, "/v1/health")
            self.assertIn("ops:local", body["models"])
        finally:
            srv.shutdown()

    def test_models_are_sorted_and_unique(self):
        la, srv = serve(self.tmp, models={"zoo:glm-5.3-flash": {"model": "zoo/glm-5.3-flash", "key": "zoo"}})
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(body["models"], sorted(set(body["models"])))
        finally:
            srv.shutdown()

    def test_free_pool_ids_are_not_advertised_as_dispatchable(self):
        # The free pool is chosen by the launcher (pool:free), not named per brief.
        la, srv = serve(self.tmp)
        try:
            st, body = get(srv, "/v1/health")
            self.assertNotIn("pool:free", body["models"])
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
