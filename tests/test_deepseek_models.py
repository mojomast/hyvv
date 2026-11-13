"""0.1.15: unprefixed / go: -> OpenCode Go; deepseek: or priority: high -> DeepSeek official."""
import importlib.util, json, os, sys, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)
TOKEN = "t" * 40


def _pop_keys():
    saved = {}
    for k in ("DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY"):
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    return saved


class DeepSeekModels(unittest.TestCase):
    def setUp(self):
        self._saved = _pop_keys()

    def tearDown(self):
        for k in ("DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY"):
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def _cfg(self, **extra):
        cfg = {"model": "deepseek-v4.1-flash", "base_url": L.GO_BASE_URL, "wire": "openai-chat", "models": {}}
        cfg.update(extra)
        return cfg

    def test_version(self):
        self.assertEqual(L.VERSION, "0.1.16")

    def test_deepseek_flash_is_official(self):
        # Unprefixed names go to Go in 0.1.15; official requires the deepseek: prefix (or priority: high).
        spec = L.model_spec(self._cfg(), "deepseek-flash")
        self.assertEqual(spec["key"], "go")
        self.assertEqual(spec["base_url"], L.GO_BASE_URL)
        self.assertEqual(spec["wire"], "openai-chat")
        official = L.model_spec(self._cfg(), "deepseek:deepseek-flash")
        self.assertEqual(official["model"], "deepseek-flash")
        self.assertEqual(official["key"], "deepseek")
        self.assertEqual(official["base_url"], L.DEEPSEEK_BASE_URL)

    def test_old_name_aliases_to_deepseek_direct(self):
        # 0.1.15: bare deepseek-v4.1-flash (and cfg.base_url) go to OpenCode Go.
        spec = L.model_spec(self._cfg(), "deepseek-v4.1-flash")
        self.assertEqual(spec["name"], "deepseek-v4.1-flash")
        self.assertEqual(spec["model"], "deepseek-v4.1-flash")
        self.assertEqual(spec["key"], "go")
        self.assertEqual(spec["base_url"], L.GO_BASE_URL)

    def test_default_cfg_model_is_deepseek(self):
        spec = L.model_spec(self._cfg(model="deepseek-flash"), None)
        self.assertEqual(spec["key"], "go")
        self.assertEqual(spec["base_url"], L.GO_BASE_URL)

    def test_go_opt_in_keeps_go_path(self):
        spec = L.model_spec(self._cfg(), "go:deepseek-v4.1-flash")
        self.assertEqual(spec["model"], "deepseek-v4.1-flash")
        self.assertEqual(spec["key"], "go")
        self.assertEqual(spec["base_url"], L.GO_BASE_URL)

    def test_load_config_defaults_to_deepseek_flash(self):
        tmp = Path(tempfile.mkdtemp(prefix="lch12-"))
        p = tmp / "cfg.json"
        p.write_text(json.dumps({"bind": "127.0.0.1", "port": 1, "data_dir": str(tmp)}))
        cfg = L.load_config(p)
        self.assertEqual(cfg["model"], "deepseek-flash")
        spec = L.model_spec(cfg, None)
        self.assertEqual(spec["key"], "go")
        self.assertEqual(spec["base_url"], L.GO_BASE_URL)

    def test_existing_config_model_alias_still_resolves(self):
        tmp = Path(tempfile.mkdtemp(prefix="lch12-"))
        p = tmp / "cfg.json"
        p.write_text(json.dumps({"bind": "127.0.0.1", "port": 1, "data_dir": str(tmp),
                                 "model": "deepseek-v4.1-flash", "base_url": L.GO_BASE_URL, "wire": "openai-chat"}))
        cfg = L.load_config(p)
        spec = L.model_spec(cfg, cfg["model"])
        self.assertEqual(spec["key"], "go")
        self.assertEqual(spec["base_url"], L.GO_BASE_URL)

    def test_provider_key_from_file_and_env(self):
        tmp = Path(tempfile.mkdtemp(prefix="lch12-"))
        cfg = {"data_dir": str(tmp)}
        self.assertEqual(L.provider_key(cfg, "deepseek"), "")
        (tmp / "deepseek.key").write_text("file-key-ds\n")
        self.assertEqual(L.provider_key(cfg, "deepseek"), "file-key-ds")
        os.environ["DEEPSEEK_API_KEY"] = "env-key-ds"
        self.assertEqual(L.provider_key(cfg, "deepseek"), "env-key-ds")
        # custom path
        other = tmp / "alt.key"
        other.write_text("alt-ds\n")
        os.environ.pop("DEEPSEEK_API_KEY")
        cfg2 = {"data_dir": str(tmp), "deepseek_key_file": str(other)}
        self.assertEqual(L.provider_key(cfg2, "deepseek"), "alt-ds")

    def test_read_secret_back_compat(self):
        tmp = Path(tempfile.mkdtemp(prefix="lch12-"))
        (tmp / "go.key").write_text("gok\n")
        (tmp / "deepseek.key").write_text("dsk\n")
        cfg = {"data_dir": str(tmp)}
        self.assertEqual(L.read_secret(cfg, "ORBIT_WORKER_GO_KEY"), "gok")
        self.assertEqual(L.read_secret(cfg, "DEEPSEEK_API_KEY"), "dsk")

    def test_free_fallback_aliases_to_deepseek(self):
        fb = L.MODELS["deepseek-v4.1-flash-free"]["fallback"]
        self.assertEqual(fb, "deepseek:deepseek-flash")
        spec = L.model_spec(self._cfg(), fb)
        self.assertEqual(spec["key"], "deepseek")
        self.assertEqual(spec["base_url"], L.DEEPSEEK_BASE_URL)


class DeepSeekDispatch(unittest.TestCase):
    def setUp(self):
        self._saved = _pop_keys()
        self.tmp = tempfile.mkdtemp(prefix="lch12d-")
        (Path(self.tmp) / "deepseek.key").write_text("fake-deepseek-key")
        (Path(self.tmp) / "go.key").write_text("fake-go-key")

    def tearDown(self):
        for k in ("DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY"):
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def _serve(self):
        cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 2, "data_dir": self.tmp,
               "backoff_s": [0.2], "projects": {"calc": {"path": "/tmp"}},
               "model": "deepseek-v4.1-flash", "base_url": L.GO_BASE_URL, "wire": "openai-chat",
               "image": "cua-bee:test", "runtime": "runc", "memory_mb": 4096}
        la = L.Launcher(cfg, L.FakeBackend(cfg))
        threading.Thread(target=la.loop.run_forever, daemon=True).start()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(la, TOKEN))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return la, srv

    def _post(self, srv, path, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=8)
        c.request("POST", path, body=json.dumps(body).encode(),
                  headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        r = c.getresponse(); raw = r.read(); c.close()
        return r.status, json.loads(raw)

    def test_alias_dispatch_starts(self):
        la, srv = self._serve()
        try:
            st, body = self._post(srv, "/v1/attempts", {
                "attempt_id": "att_ds00000001", "model": "deepseek-v4.1-flash",
                "brief": {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]},
            })
            self.assertIn(st, (200, 201), body)
            t0 = time.time()
            while time.time() - t0 < 8 and la.attempts["att_ds00000001"]["state"] not in L.TERMINAL:
                time.sleep(0.05)
            self.assertEqual(la.attempts["att_ds00000001"]["state"], "succeeded")
            self.assertEqual(la.attempts["att_ds00000001"]["model"], "deepseek-v4.1-flash")
            spec = L.model_spec(la.cfg, "deepseek-v4.1-flash")
            self.assertEqual(spec["key"], "go")
        finally:
            srv.shutdown()

    def test_missing_deepseek_key_fails_fast(self):
        Path(self.tmp, "deepseek.key").unlink()
        la, srv = self._serve()
        try:
            st, body = self._post(srv, "/v1/attempts", {
                "attempt_id": "att_ds00000002", "model": "deepseek:deepseek-flash",
                "brief": {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]},
            })
            self.assertIn(st, (200, 201), body)
            t0 = time.time()
            while time.time() - t0 < 8 and la.attempts["att_ds00000002"]["state"] not in L.TERMINAL:
                time.sleep(0.05)
            a = la.attempts["att_ds00000002"]
            self.assertEqual(a["state"], "failed")
            self.assertIn("provider key not configured", (a.get("error") or "") + " " + (a.get("detail") or ""))
        finally:
            srv.shutdown()

    def test_go_opt_in_accepted(self):
        la, srv = self._serve()
        try:
            st, body = self._post(srv, "/v1/attempts", {
                "attempt_id": "att_ds00000003", "model": "go:deepseek-v4.1-flash",
                "brief": {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]},
            })
            self.assertIn(st, (200, 201), body)
            t0 = time.time()
            while time.time() - t0 < 8 and la.attempts["att_ds00000003"]["state"] not in L.TERMINAL:
                time.sleep(0.05)
            self.assertEqual(la.attempts["att_ds00000003"]["state"], "succeeded")
            self.assertEqual(la.attempts["att_ds00000003"]["model"], "go:deepseek-v4.1-flash")
        finally:
            srv.shutdown()

    def test_prefixed_deepseek_dispatch_starts(self):
        la, srv = self._serve()
        try:
            st, body = self._post(srv, "/v1/attempts", {
                "attempt_id": "att_ds00000004", "model": "deepseek:deepseek-flash",
                "brief": {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]},
            })
            self.assertIn(st, (200, 201), body)
            t0 = time.time()
            while time.time() - t0 < 8 and la.attempts["att_ds00000004"]["state"] not in L.TERMINAL:
                time.sleep(0.05)
            self.assertEqual(la.attempts["att_ds00000004"]["state"], "succeeded")
            spec = L.model_spec(la.cfg, la.attempts["att_ds00000004"]["model"])
            self.assertEqual(spec["key"], "deepseek")
        finally:
            srv.shutdown()

    def test_priority_high_routes_to_deepseek(self):
        la, srv = self._serve()
        try:
            st, body = self._post(srv, "/v1/attempts", {
                "attempt_id": "att_ds00000005", "model": "deepseek-v4.1-flash",
                "brief": {"objective": "x", "deliverables": ["a"], "acceptance": ["b"], "priority": "high"},
            })
            self.assertIn(st, (200, 201), body)
            t0 = time.time()
            while time.time() - t0 < 8 and la.attempts["att_ds00000005"]["state"] not in L.TERMINAL:
                time.sleep(0.05)
            a = la.attempts["att_ds00000005"]
            self.assertEqual(a["state"], "succeeded")
            spec = L.model_spec(la.cfg, a["model"], a["brief"])
            self.assertEqual(spec["key"], "deepseek")
            self.assertEqual(spec["base_url"], L.DEEPSEEK_BASE_URL)
        finally:
            srv.shutdown()

    def test_monthly_limit_429_fallbacks_once_to_deepseek(self):
        la, srv = self._serve()
        try:
            st, body = self._post(srv, "/v1/attempts", {
                "attempt_id": "att_ds00000006", "model": "deepseek-v4.1-flash",
                "brief": {"objective": "x", "deliverables": ["a"], "acceptance": ["b"],
                          "fake": {"monthly_limit": True,
                                   "monthly_limit_text": "HTTP 429 GoUsageLimitError: usage limit reached monthly limitName=monthly"}},
            })
            self.assertIn(st, (200, 201), body)
            t0 = time.time()
            while time.time() - t0 < 12 and la.attempts["att_ds00000006"]["state"] not in L.TERMINAL:
                time.sleep(0.05)
            a = la.attempts["att_ds00000006"]
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a.get("fallback"), "deepseek")
            self.assertEqual(a["model"], "deepseek:deepseek-flash")
            spec = L.model_spec(la.cfg, a["model"], a.get("brief"))
            self.assertEqual(spec["key"], "deepseek")
            # FakeBackend records models used across start() calls
            self.assertIn("deepseek-v4.1-flash", la.backend.models)
            self.assertIn("deepseek:deepseek-flash", la.backend.models)
        finally:
            srv.shutdown()

    def test_generic_429_does_not_set_deepseek_fallback(self):
        la, srv = self._serve()
        try:
            st, body = self._post(srv, "/v1/attempts", {
                "attempt_id": "att_ds00000007", "model": "deepseek-v4.1-flash",
                "brief": {"objective": "x", "deliverables": ["a"], "acceptance": ["b"],
                          "fake": {"rate_limit_first": 99}},
            })
            self.assertIn(st, (200, 201), body)
            t0 = time.time()
            # backoff_s is 0.2, then a second try also 429s and fails after retries
            while time.time() - t0 < 12 and la.attempts["att_ds00000007"]["state"] not in L.TERMINAL:
                time.sleep(0.05)
            a = la.attempts["att_ds00000007"]
            self.assertIn(a["state"], ("failed", "backoff", "queued"))
            self.assertNotEqual(a.get("fallback"), "deepseek")
        finally:
            srv.shutdown()


class RoutingUnit(unittest.TestCase):
    def _cfg(self):
        return {"model": "deepseek-v4.1-flash", "base_url": L.GO_BASE_URL, "wire": "openai-chat", "models": {}}

    def test_go_prefix(self):
        spec = L.model_spec(self._cfg(), "go:deepseek-v4.1-flash")
        self.assertEqual(spec["key"], "go")
        self.assertEqual(spec["base_url"], L.GO_BASE_URL)

    def test_deepseek_prefix(self):
        spec = L.model_spec(self._cfg(), "deepseek:deepseek-flash")
        self.assertEqual(spec["key"], "deepseek")
        self.assertEqual(spec["model"], "deepseek-flash")
        self.assertEqual(spec["base_url"], L.DEEPSEEK_BASE_URL)

    def test_priority_high_overrides_bare_and_go_prefix(self):
        for name in ("deepseek-v4.1-flash", "go:deepseek-v4.1-flash", "deepseek-flash"):
            spec = L.model_spec(self._cfg(), name, {"priority": "high"})
            self.assertEqual(spec["key"], "deepseek", name)
            self.assertEqual(spec["base_url"], L.DEEPSEEK_BASE_URL, name)

    def test_is_monthly_limit(self):
        self.assertTrue(L.is_monthly_limit("HTTP 429 GoUsageLimitError: usage limit reached monthly limitName=monthly"))
        self.assertFalse(L.is_monthly_limit("HTTP 429 Too Many Requests"))
        self.assertFalse(L.is_monthly_limit("usage limit reached monthly"))  # no 429/rate-limit token
        self.assertFalse(L.is_monthly_limit("HTTP 429 rate limit"))  # no monthly hint

    def test_free_models_unchanged(self):
        spec = L.model_spec(self._cfg(), "deepseek-v4.1-flash-free")
        self.assertTrue(spec.get("free"))
        self.assertEqual(spec["key"], "none")



if __name__ == "__main__":
    unittest.main()
