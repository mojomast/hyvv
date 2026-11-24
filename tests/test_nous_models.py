"""Nous Portal model set (owner, Kyle t1665u, Nov 24 2026): the discounted DeepSeek flash
(`deepseek/deepseek-v4.1-flash-1017`) and the working $0 Nous models are allow-listed on the
`nous` key, and low-risk bees chain 1017 -> Hy4 Flash -> OpenCode Go DeepSeek flash.

Pinned:
  * every nous:* entry uses the nous key, the Nous endpoint and the same pacing cap, so the
    cap applies to all Nous models combined (one key, 180 requests/min);
  * fallback chains hop at most MAX_FALLBACK_HOPS times and only for models that opt in;
  * a 1017 provider failure lands on Hy4; Hy4 failing too lands on Go DeepSeek flash;
  * a model without the opt-in still falls back once at most.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cua", mock.MagicMock())
import launcher  # noqa: E402
from tests.test_nous_provider import KEY_VARS, FIX, _launcher, _run_to_terminal  # noqa: E402

D1017 = "nous:deepseek-v4.1-flash-1017"
HY4 = "nous:hy4-flash"
GO = "go:deepseek-v4.1-flash"
FREE = {"nous:hy4-flash-free": "tencent/hy4-flash:free", "nous:longcat-2.5-free": "meituan/longcat-2.5:free"}


def _cfg():
    return {"data_dir": tempfile.mkdtemp(prefix="nous-models-"), "model": GO,
            "base_url": launcher.GO_BASE_URL, "models": {}}


class NousModelSet(unittest.TestCase):
    def test_discounted_deepseek_flash_1017(self):
        s = launcher.model_spec(_cfg(), D1017)
        self.assertEqual((s["key"], s["model"], s["base_url"]),
                         ("nous", "deepseek/deepseek-v4.1-flash-1017", launcher.NOUS_BASE_URL))
        self.assertEqual(s["fallback"], HY4)

    def test_free_nous_models(self):
        for name, mid in FREE.items():
            s = launcher.model_spec(_cfg(), name)
            self.assertEqual((s["key"], s["model"], s["base_url"]), ("nous", mid, launcher.NOUS_BASE_URL), name)
            self.assertEqual(s["fallback"], HY4, name)

    def test_every_nous_model_shares_one_pacing_cap(self):
        names = [m for m, v in launcher.MODELS.items() if v.get("key") == "nous"]
        self.assertEqual(sorted(names), sorted([D1017, HY4, *FREE]))
        caps = {launcher.model_spec(_cfg(), m)["max_active"] for m in names}
        self.assertEqual(len(caps), 1, caps)
        self.assertTrue(all(m in launcher.MODEL_ABBR for m in names))

    def test_no_batch_skus(self):
        self.assertFalse([m for m, v in launcher.MODELS.items() if ":batch" in (v.get("model") or "")])

    def test_hop_cap(self):
        self.assertEqual(launcher.MAX_FALLBACK_HOPS, 2)


class NousFallbackChain(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in KEY_VARS if k in os.environ}

    def tearDown(self):
        for k in KEY_VARS:
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def test_1017_failure_lands_on_hy4(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": FIX, "ORBIT_WORKER_NOUS_KEY": FIX}):
            L, be = _launcher(d, models={m: {"start_spacing_s": 0} for m in (D1017, HY4)})
            a, _ = L.submit("att_nouschain001", {"objective": "x", "model": D1017,
                                                 "fake": {"unavailable_models": [D1017],
                                                          "unavailable_text": "HTTP 503 Service Unavailable"}})
            _run_to_terminal(L, [a])
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], HY4)

    def test_1017_then_hy4_failure_lands_on_go(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": FIX, "ORBIT_WORKER_NOUS_KEY": FIX}):
            L, be = _launcher(d, models={m: {"start_spacing_s": 0} for m in (D1017, HY4)})
            a, _ = L.submit("att_nouschain002", {"objective": "x", "model": D1017,
                                                 "fake": {"unavailable_models": [D1017, HY4],
                                                          "unavailable_text": "HTTP 502 Bad Gateway"}})
            _run_to_terminal(L, [a])
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], GO)
            self.assertEqual([h["model"] for h in a["model_history"] if h.get("fallback")], [HY4, GO])

    def test_missing_nous_key_chains_straight_to_go(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": FIX}):
            L, be = _launcher(d)
            a, _ = L.submit("att_nouschain003", {"objective": "x", "model": D1017})
            _run_to_terminal(L, [a])
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], GO)
            self.assertFalse([m for m in getattr(be, "models", []) if m.startswith("nous:")])


if __name__ == "__main__":
    unittest.main()
