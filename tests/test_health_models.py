"""GET /v1/health advertises the launcher's allow-listed models.

A coordinator validates a brief's model against the health payload before it dispatches (the
bundlebees adapter refuses one the payload omits), so a single `model` field made every other
allow-listed model undispatchable -- including a model the operator added in `config.json`.

POST /v1/config applies the routing-relevant keys to the running launcher's in-memory cfg, so
/v1/health must reflect a hot change immediately, without a restart.
"""
import http.client
import importlib.util
import json
import os
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
ZOO_WIRE = "zoo/glm-5.3-flash"
KEY_VARS = ("ORBIT_WORKER_ZOO_KEY", "DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY")


def _pop_env():
    saved = {}
    for v in KEY_VARS:
        if v in os.environ:
            saved[v] = os.environ.pop(v)
    return saved


def serve(tmp, model="deepseek:deepseek-flash", models=None, hot=False):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": tmp, "projects": {},
           "model": model, "base_url": L.DEEPSEEK_BASE_URL, "wire": "openai-chat", "image": "img",
           "models": models or {}}
    if hot:  # a real config.json on disk, so POST /v1/config can persist its patch
        (Path(tmp) / "config.json").write_text(json.dumps(cfg), "utf-8")
        cfg["_config_path"] = str(Path(tmp) / "config.json")
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


def post(srv, path, body):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request("POST", path, body=json.dumps(body).encode("utf-8"),
              headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
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


class HotConfigHealth(unittest.TestCase):
    """A POST /v1/config change to the routing keys shows up in /v1/health immediately."""

    def setUp(self):
        self._saved = _pop_env()
        self.tmp = tempfile.mkdtemp(prefix="lch-hot-")

    def tearDown(self):
        for v in KEY_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def test_health_reflects_a_hot_model_change_immediately(self):
        la, srv = serve(self.tmp, hot=True)
        try:
            st, body = post(srv, "/v1/config", {"model": "go:deepseek-v4.1-flash"})
            self.assertEqual(st, 200, body)
            st, health = get(srv, "/v1/health")
            self.assertEqual(st, 200)
            self.assertEqual(health["model"], "go:deepseek-v4.1-flash")
            self.assertEqual(health["configured_model"], "go:deepseek-v4.1-flash")
            self.assertIn("go:deepseek-v4.1-flash", health["models"])
        finally:
            srv.shutdown()

    def test_health_reflects_a_hot_prefer_zoo_change(self):
        (Path(self.tmp) / "zoo.key").write_text("fake-zoo-credential", "utf-8")
        la, srv = serve(self.tmp, hot=True)
        try:
            st, before = get(srv, "/v1/health")
            self.assertEqual(before["effective_model"], ZOO_WIRE)
            st, body = post(srv, "/v1/config", {"prefer_zoo": False})
            self.assertEqual(st, 200, body)
            st, after = get(srv, "/v1/health")
            self.assertEqual(after["effective_model"], "deepseek-flash")
            self.assertEqual(after["model"], "deepseek:deepseek-flash")
        finally:
            srv.shutdown()

    def test_health_reflects_hot_models_entries(self):
        la, srv = serve(self.tmp, hot=True)
        try:
            local = {"model": "ops/local", "base_url": "http://127.0.0.1:9/v1", "wire": "openai-chat", "key": "none"}
            st, body = post(srv, "/v1/config", {"models": {"ops:local": local}})
            self.assertEqual(st, 200, body)
            st, health = get(srv, "/v1/health")
            self.assertIn("ops:local", health["models"])
            self.assertIn("zoo:glm-5.3-flash", health["models"])
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
