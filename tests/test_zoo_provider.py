"""Zoo gateway (OpenAI-compatible): the `zoo` key source, `zoo:glm-5.3-flash` routing, and a
backoff that honours a provider `Retry-After` on 429 and retries Zoo's 402 admission error.

Zoo facts this pins (benchmarked Nov 15):
  * `zoo/glm-5.3-flash` is the free pass model; `zai/glm-5.3-flash` is the paid one.
  * 16 concurrent runs are clean, 20 is the edge -> the launcher's own ceiling (10) caps it.
  * 429 "Too many concurrent requests" carries Retry-After 5-7s.
  * 402 "Insufficient credits" is an admission error and is retried, not fatal.
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)

ZOO = "zoo:glm-5.3-flash"
KEY_VARS = ("ORBIT_WORKER_ZOO_KEY", "DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY")


def _pop_keys():
    saved = {}
    for k in KEY_VARS:
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    return saved


class ZooKeySource(unittest.TestCase):
    def setUp(self):
        self._saved = _pop_keys()
        self.tmp = Path(tempfile.mkdtemp(prefix="zoo-key-"))

    def tearDown(self):
        for k in KEY_VARS:
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def test_zoo_is_a_first_class_key_source(self):
        self.assertEqual(L._KEY_FILE_CFG["zoo"], "zoo_key_file")
        self.assertEqual(L._KEY_FILE_NAME["zoo"], "zoo.key")
        self.assertEqual(L._KEY_ENV["zoo"], "ORBIT_WORKER_ZOO_KEY")

    def test_key_from_data_dir_zoo_key_file(self):
        (self.tmp / "zoo.key").write_text("zoo-file-key\n", "utf-8")
        self.assertEqual(L.provider_key({"data_dir": str(self.tmp)}, "zoo"), "zoo-file-key")

    def test_key_file_override(self):
        p = self.tmp / "elsewhere.key"
        p.write_text("zoo-alt-key", "utf-8")
        cfg = {"data_dir": str(self.tmp), "zoo_key_file": str(p)}
        self.assertEqual(L.provider_key(cfg, "zoo"), "zoo-alt-key")

    def test_env_wins_over_file(self):
        (self.tmp / "zoo.key").write_text("from-file", "utf-8")
        os.environ["ORBIT_WORKER_ZOO_KEY"] = "from-env"
        self.assertEqual(L.provider_key({"data_dir": str(self.tmp)}, "zoo"), "from-env")

    def test_zoo_never_borrows_another_providers_key(self):
        (self.tmp / "go.key").write_text("go-key", "utf-8")
        (self.tmp / "deepseek.key").write_text("ds-key", "utf-8")
        self.assertEqual(L.provider_key({"data_dir": str(self.tmp)}, "zoo"), "")

    def test_worker_env_sends_the_zoo_key_as_openai_api_key(self):
        # The OpenAI-compatible wire reads OPENAI_API_KEY; a zoo spec must be included.
        source = (HERE / "launcher.py").read_text("utf-8")
        self.assertIn('if spec["key"] in ("go", "deepseek", "zoo"): env["OPENAI_API_KEY"] = key', source)


class ZooRouting(unittest.TestCase):
    def _cfg(self, **extra):
        cfg = {"model": "deepseek-v4.1-flash", "base_url": L.GO_BASE_URL, "wire": "openai-chat",
               "models": {}, "data_dir": tempfile.mkdtemp(prefix="zoo-cfg-")}
        cfg.update(extra)
        return cfg

    def test_builtin_spec(self):
        spec = L.model_spec(self._cfg(), ZOO)
        self.assertEqual(spec["model"], "zoo/glm-5.3-flash")
        self.assertEqual(spec["base_url"], "https://www.zoocode.dev/api/gateway/v1")
        self.assertEqual(spec["wire"], "openai-chat")
        self.assertEqual(spec["key"], "zoo")
        self.assertEqual(spec["fallback"], "go:deepseek-v4.1-flash")
        self.assertTrue(spec.get("backoff_s"))

    def test_config_entry_extends_the_builtin(self):
        cfg = self._cfg(models={ZOO: {"model": "zoo/glm-5.3-flash",
                                      "base_url": "https://www.zoocode.dev/api/gateway/v1",
                                      "wire": "openai-chat", "key": "zoo",
                                      "fallback": "go:deepseek-v4.1-flash",
                                      "backoff_s": [15, 30]}})
        spec = L.model_spec(cfg, ZOO)
        self.assertEqual(spec["backoff_s"], [15, 30])
        self.assertEqual(spec["key"], "zoo")

    def test_standard_tier_is_zoo(self):
        self.assertEqual(L.TIERS["standard"], ZOO)
        self.assertEqual(L.model_spec(self._cfg(), "standard")["key"], "zoo")

    def test_high_priority_stays_deepseek_direct(self):
        spec = L.model_spec(self._cfg(), ZOO, {"priority": "high"})
        self.assertEqual(spec["key"], "deepseek")
        self.assertEqual(spec["base_url"], L.DEEPSEEK_BASE_URL)

    def test_zoo_is_not_in_the_free_pool(self):
        self.assertNotIn(ZOO, L.FREE_POOL)

    def test_zoo_spec_is_recognised(self):
        self.assertTrue(L.is_zoo_model(self._cfg(), ZOO))
        self.assertFalse(L.is_zoo_model(self._cfg(), "go:deepseek-v4.1-flash"))
        self.assertFalse(L.is_zoo_model(self._cfg(), "deepseek:deepseek-flash"))

    def test_launcher_ceiling_is_under_the_zoo_limit(self):
        # Zoo: 16 clean, 20 is the edge, cap at 12 in flight. The launcher cannot exceed its ceiling.
        self.assertLessEqual(L.MAX_WORKERS, 12)


class RetryAfterParsing(unittest.TestCase):
    def test_header_forms(self):
        cases = [("HTTP 429 Too many concurrent requests\nRetry-After: 6", 6),
                 ("retry-after=5", 5),
                 ("Retry_After 7s", 7),
                 ("HTTP 429 Too Many Requests", None),
                 ("", None)]
        for text, want in cases:
            self.assertEqual(L.retry_after_s(text), want, text)

    def test_clamped_to_the_cap(self):
        self.assertEqual(L.retry_after_s("Retry-After: 99999"), L.ZOO_RETRY_AFTER_MAX)

    def test_http_date_is_not_guessed(self):
        self.assertIsNone(L.retry_after_s("Retry-After: Wed, 21 Oct 2026 07:28:00 GMT"))

    def test_zoo_admission_error_matches(self):
        for text in ("HTTP 402 Insufficient credits", "402 insufficient credits (admission)",
                     '{"error":"Insufficient credits"}', '{"code":402,"error":"admission"}'.replace('"error"', "error")):
            self.assertTrue(L.ZOO_ADMISSION.search(text), text)
        self.assertFalse(L.ZOO_ADMISSION.search("HTTP 429 Too Many Requests"))


def mk(tmp, backoff=(0.2,)):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": tmp,
           "backoff_s": list(backoff), "projects": {}, "model": "deepseek-v4.1-flash",
           "base_url": L.GO_BASE_URL, "wire": "openai-chat", "image": "fake-image",
           # The zoo spec ships its own schedule ([15, 30, 60]); a config entry overrides it, which is
           # also how the deployed launcher shortens it. Tests keep it sub-second.
           "models": {ZOO: {"backoff_s": list(backoff)}}}
    la = L.Launcher(cfg, L.FakeBackend(cfg))
    threading.Thread(target=la.loop.run_forever, daemon=True).start()
    return la


def wait_for(pred, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class ZooBackoff(unittest.TestCase):
    def setUp(self):
        self._saved = _pop_keys()
        self.tmp = tempfile.mkdtemp(prefix="zoo-att-")
        (Path(self.tmp) / "zoo.key").write_text("fake-zoo-key", "utf-8")
        (Path(self.tmp) / "go.key").write_text("fake-go-key", "utf-8")  # the Go fallback needs its own key
        self.la = mk(self.tmp)

    def tearDown(self):
        for k in KEY_VARS:
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def _brief(self, aid, **fake):
        return (aid, {"objective": "x", "model": ZOO, "deliverables": ["a"], "acceptance": ["b"],
                      "fake": fake})

    def test_429_retry_after_is_honoured_instead_of_the_fixed_schedule(self):
        aid, brief = self._brief("att_zoo00000001", rate_limit_first=1,
                                 rate_limit_text="HTTP 429 Too many concurrent requests\nRetry-After: 1")
        self.la.submit(aid, brief)
        self.assertTrue(wait_for(lambda: self.la.attempts[aid]["state"] == "backoff"), "never backed off")
        a = self.la.attempts[aid]
        self.assertEqual(a["retry_after_s"], 1)
        wait = a["retry_at"] - time.time()
        self.assertGreater(wait, 0.6, wait)   # the 0.2s schedule would fail this
        self.assertLess(wait, 1.5, wait)
        self.assertTrue(wait_for(lambda: self.la.attempts[aid]["state"] in L.TERMINAL))
        a = self.la.attempts[aid]
        self.assertEqual(a["state"], "succeeded", a.get("detail"))
        self.assertEqual(self.la.backend.starts, 2)

    def test_402_admission_error_is_retried(self):
        aid, brief = self._brief("att_zoo00000002", error_first=1,
                                 error_text='HTTP 402 {"error":"Insufficient credits","detail":"admission"}')
        self.la.submit(aid, brief)
        self.assertTrue(wait_for(lambda: self.la.attempts[aid]["state"] in L.TERMINAL))
        a = self.la.attempts[aid]
        self.assertEqual(a["state"], "succeeded", a.get("detail"))
        self.assertEqual(self.la.backend.starts, 2)
        self.assertEqual(a["model"], ZOO)

    def test_402_backoff_carries_a_reason(self):
        aid, brief = self._brief("att_zoo00000003", error_first=99,
                                 error_text="HTTP 402 Insufficient credits")
        self.la.submit(aid, brief)
        self.assertTrue(wait_for(lambda: self.la.attempts[aid]["state"] == "backoff"), "never backed off")
        self.assertIn("zoo admission 402", self.la.attempts[aid]["detail"])

    def test_persistent_429_falls_back_to_go(self):
        # Zoo rate limits that never clear: exhaust the retries, then switch to Go and finish there.
        aid, brief = self._brief("att_zoo00000004", rate_limit_models=[ZOO])
        self.la.submit(aid, brief)
        self.assertTrue(wait_for(lambda: self.la.attempts[aid]["state"] in L.TERMINAL, timeout=20))
        a = self.la.attempts[aid]
        self.assertEqual(a["state"], "succeeded", a.get("detail"))
        self.assertEqual(a["model"], "go:deepseek-v4.1-flash")
        self.assertEqual(a["fallback"]["to"], "go:deepseek-v4.1-flash")

    def test_402_on_a_non_zoo_model_is_not_swallowed(self):
        aid, brief = self._brief("att_zoo00000005", error_first=1, error_text="HTTP 402 Insufficient credits")
        brief["model"] = "go:deepseek-v4.1-flash"
        self.la.submit(aid, brief)
        time.sleep(0.8)
        a = self.la.attempts[aid]
        self.assertEqual(a["state"], "running", a.get("detail"))  # no retry, no admission branch
        self.assertEqual(self.la.backend.starts, 1)
        self.la.cancel(aid)


if __name__ == "__main__":
    unittest.main()
