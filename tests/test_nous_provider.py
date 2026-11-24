"""Nous Portal (OpenAI-compatible inference API) is a first-class key source and model route.

Owner request (Kyle, Nov 24 2026): get the bees ready to run Hy4 Flash through Nous Portal and
move some tasks to it. Shape pinned here:
  * `nous` key kind: env ORBIT_WORKER_NOUS_KEY, else cfg nous_key_file, else data_dir/nous.key;
    never borrows another provider's key; handed to the worker as OPENAI_API_KEY.
  * `nous:hy4-flash` is allow-listed: model tencent/hy4-flash on the Nous endpoint, key nous,
    with OpenCode Go DeepSeek flash as its fallback.
  * a Nous 429 retries on its backoff schedule and then falls back to DeepSeek flash;
  * a Nous provider failure (auth or 5xx) falls back once instead of failing the attempt;
  * a missing Nous key falls back to DeepSeek flash instead of failing before start;
  * pacing: at most `max_active` nous attempts run at once (the key is limited to 180
    requests/min shared across models); other queued attempts still start;
  * usage is attributed to the `nous` route.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cua", mock.MagicMock())
import launcher  # noqa: E402

NOUS = "nous:hy4-flash"
GO = "go:deepseek-v4.1-flash"
KEY_VARS = ("ORBIT_WORKER_NOUS_KEY", "ORBIT_WORKER_KIMI_KEY", "ORBIT_WORKER_ZOO_KEY",
            "DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY")
FIX = "fixture-value-not-a-real-key"
WINDOW = 10


class NousKeySource(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in KEY_VARS if k in os.environ}
        self.tmp = Path(tempfile.mkdtemp(prefix="nous-key-"))

    def tearDown(self):
        for k in KEY_VARS:
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def test_nous_is_a_first_class_key_source(self):
        self.assertEqual(launcher._KEY_FILE_CFG["nous"], "nous_key_file")
        self.assertEqual(launcher._KEY_FILE_NAME["nous"], "nous.key")
        self.assertEqual(launcher._KEY_ENV["nous"], "ORBIT_WORKER_NOUS_KEY")

    def test_key_from_data_dir_nous_key_file(self):
        (self.tmp / "nous.key").write_text("nous-file-key\n", "utf-8")
        self.assertEqual(launcher.provider_key({"data_dir": str(self.tmp)}, "nous"), "nous-file-key")

    def test_key_file_override(self):
        p = self.tmp / "elsewhere.key"
        p.write_text("nous-alt-key", "utf-8")
        cfg = {"data_dir": str(self.tmp), "nous_key_file": str(p)}
        self.assertEqual(launcher.provider_key(cfg, "nous"), "nous-alt-key")

    def test_env_wins_over_file(self):
        (self.tmp / "nous.key").write_text("from-file", "utf-8")
        os.environ["ORBIT_WORKER_NOUS_KEY"] = "from-env"
        self.assertEqual(launcher.provider_key({"data_dir": str(self.tmp)}, "nous"), "from-env")

    def test_nous_never_borrows_another_providers_key(self):
        for n in ("go.key", "deepseek.key", "zoo.key", "kimi.key"):
            (self.tmp / n).write_text("other-" + n, "utf-8")
        self.assertEqual(launcher.provider_key({"data_dir": str(self.tmp)}, "nous"), "")

    def test_worker_env_sends_the_nous_key_as_openai_api_key(self):
        source = (ROOT / "launcher.py").read_text("utf-8")
        found = re.search(r'if spec\["key"\] in \([^)]*"nous"[^)]*\): env\["OPENAI_API_KEY"\] = key', source)
        self.assertTrue(found, "the worker env must hand a nous key to the OpenAI-compatible wire")


class NousModelRoute(unittest.TestCase):
    def _cfg(self):
        return {"data_dir": tempfile.mkdtemp(prefix="nous-route-"), "model": GO, "base_url": launcher.GO_BASE_URL, "models": {}}

    def test_hy4_flash_is_allow_listed_on_the_nous_endpoint(self):
        spec = launcher.model_spec(self._cfg(), NOUS)
        self.assertEqual(spec["key"], "nous")
        self.assertEqual(spec["model"], "tencent/hy4-flash")
        self.assertEqual(spec["base_url"], launcher.NOUS_BASE_URL)
        self.assertTrue(launcher.NOUS_BASE_URL.startswith("https://inference-api.nousresearch.com/"))
        self.assertEqual(spec["wire"], "openai-chat")
        self.assertEqual(spec["name"], NOUS)

    def test_hy4_flash_falls_back_to_deepseek_flash(self):
        spec = launcher.model_spec(self._cfg(), NOUS)
        self.assertEqual(spec["fallback"], GO)
        fb = launcher.model_spec(self._cfg(), spec["fallback"])
        self.assertEqual(fb["key"], "go")
        self.assertEqual(fb["model"], "deepseek-v4.1-flash")

    def test_hy4_flash_has_a_pacing_cap(self):
        spec = launcher.model_spec(self._cfg(), NOUS)
        self.assertIsInstance(spec.get("max_active"), int)
        self.assertGreaterEqual(spec["max_active"], 1)
        self.assertLessEqual(spec["max_active"], 8)

    def test_config_can_override_the_pacing_cap(self):
        cfg = self._cfg()
        cfg["models"] = {NOUS: {"max_active": 2}}
        self.assertEqual(launcher.model_spec(cfg, NOUS)["max_active"], 2)

    def test_usage_route_is_nous(self):
        self.assertEqual(launcher._usage_route("opencode", NOUS), "nous")


def _launcher(d, max_concurrent=4, models=None):
    Path(d, "config.json").write_text(json.dumps({"backoff_s": [1, 1]}), "utf-8")
    cfg = {"data_dir": d, "image": "img", "model": GO, "base_url": launcher.GO_BASE_URL, "backoff_s": [1, 1],
           "max_concurrent": max_concurrent, "models": models or {}}
    be = launcher.FakeBackend(None)
    L = launcher.Launcher(cfg, be)
    L.kick = lambda: None
    return L, be


def _run_to_terminal(L, attempts, window=WINDOW):
    async def go():
        t0 = time.monotonic()
        while any(a["state"] not in launcher.TERMINAL for a in attempts) and time.monotonic() - t0 < window:
            await L._schedule()
            await asyncio.sleep(0.02)
    asyncio.run(go())


class NousFallback(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in KEY_VARS if k in os.environ}

    def tearDown(self):
        for k in KEY_VARS:
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def _env(self, nous=True):
        env = {"ORBIT_WORKER_GO_KEY": FIX}
        if nous:
            env["ORBIT_WORKER_NOUS_KEY"] = FIX
        return mock.patch.dict(os.environ, env)

    def test_nous_429_retries_then_falls_back_to_deepseek_flash(self):
        with tempfile.TemporaryDirectory() as d, self._env():
            L, be = _launcher(d, models={NOUS: {"backoff_s": [0.05]}})
            a, _ = L.submit("att_nous00000001", {"objective": "x", "model": NOUS,
                                                 "fake": {"rate_limit_models": [NOUS]}})
            _run_to_terminal(L, [a])
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], GO)
            self.assertEqual((a.get("fallback") or {}).get("to"), GO)
            self.assertIn(NOUS, be.models)

    def test_nous_auth_failure_falls_back_once(self):
        with tempfile.TemporaryDirectory() as d, self._env():
            L, be = _launcher(d)
            a, _ = L.submit("att_nous00000002", {"objective": "x", "model": NOUS,
                                                 "fake": {"unavailable_models": [NOUS],
                                                          "unavailable_text": "HTTP 401 Unauthorized: invalid api key"}})
            _run_to_terminal(L, [a])
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], GO)

    def test_nous_server_error_falls_back_once(self):
        with tempfile.TemporaryDirectory() as d, self._env():
            L, be = _launcher(d)
            a, _ = L.submit("att_nous00000003", {"objective": "x", "model": NOUS,
                                                 "fake": {"unavailable_models": [NOUS],
                                                          "unavailable_text": "HTTP 503 Service Unavailable"}})
            _run_to_terminal(L, [a])
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], GO)

    def test_provider_failure_on_a_go_model_does_not_reroute(self):
        # The provider-failure fallback is opt-in per model; Go behaviour is unchanged.
        with tempfile.TemporaryDirectory() as d, self._env():
            L, be = _launcher(d)
            a, _ = L.submit("att_nous00000004", {"objective": "x", "model": GO,
                                                 "fake": {"unavailable_models": [GO],
                                                          "unavailable_text": "HTTP 401 Unauthorized"}})
            _run_to_terminal(L, [a])
            self.assertEqual(a["model"], GO)
            self.assertFalse(a.get("fallback"))

    def test_missing_nous_key_falls_back_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as d, self._env(nous=False):
            L, be = _launcher(d)
            a, _ = L.submit("att_nous00000005", {"objective": "x", "model": NOUS})
            _run_to_terminal(L, [a])
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], GO)
            self.assertNotIn(NOUS, be.models, "no nous run may start without a nous key")

    def test_missing_key_without_opt_in_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False):
            L, be = _launcher(d)
            a, _ = L.submit("att_nous00000006", {"objective": "x", "model": GO})
            _run_to_terminal(L, [a])
            self.assertEqual(a["state"], "failed")
            self.assertIn("provider key not configured", a.get("detail") or "")


class NousPacing(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in KEY_VARS if k in os.environ}

    def tearDown(self):
        for k in KEY_VARS:
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def test_at_most_max_active_nous_attempts_start_and_others_still_run(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": FIX, "ORBIT_WORKER_NOUS_KEY": FIX}):
            L, be = _launcher(d, max_concurrent=6, models={NOUS: {"max_active": 2, "start_spacing_s": 0}})
            fake = {"delay": 0.5}
            nous = [L.submit(f"att_nouspace000{i}", {"objective": "x", "model": NOUS, "fake": fake})[0] for i in range(4)]
            go = L.submit("att_nouspacego01", {"objective": "x", "model": GO, "fake": fake})[0]

            async def first_tick():
                await L._schedule()
                await asyncio.sleep(0)
            asyncio.run(first_tick())
            started = [a for a in nous if a["state"] in ("starting", "running")]
            self.assertEqual(len(started), 2, [a["state"] for a in nous])
            self.assertIn(go["state"], ("starting", "running"), "a non-nous attempt behind the cap must still start")
            _run_to_terminal(L, nous + [go])
            self.assertTrue(all(a["state"] == "succeeded" for a in nous + [go]),
                            [(a["attempt_id"], a["state"], a.get("detail")) for a in nous + [go]])


if __name__ == "__main__":
    unittest.main()
