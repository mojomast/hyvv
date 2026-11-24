"""RED-first: a provider that refuses to serve a model at all must roll the fallback.

Incident (2026-11-17, ~09:15 EST): every bundlebees dispatch started failing with
`[error] Internal error: Zoo Pass Pilot models are available in the Zoo Code IDE only.`
The launcher's controller loop only knew three error classes - free-tier denial,
Zoo 402 admission, and 429 rate limits - so this refusal fell through to the next
event (`turn_ended`) and `_finish` marked the attempt failed after zero model work.
The configured fallback (`nous:deepseek-v4.1-flash-1017`) never engaged, and the whole
bee pipeline went down even though a healthy route was configured.

These tests fail on the unmodified tree at 129191f (no MODEL_UNAVAILABLE pattern,
no `_fallback_unavailable`, FakeBackend cannot simulate a per-model refusal) and
pass once the fallback branch exists.

Shape pinned here:
  * the Zoo IDE-only refusal switches the attempt to the model spec's fallback,
    records model_history, clears the sandbox, and re-queues (not fails);
  * the fallback run then completes normally (succeeded);
  * a refusal on 1017 rolls to Hy4 on the second and final fallback hop;
  * unrelated error text still takes the pre-existing paths (no fallback).

Run: python3 -m pytest tests/test_model_unavailable_fallback.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
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

ZOO = "zoo:glm-5.3-flash"
D1017 = "nous:deepseek-v4.1-flash-1017"
HY4 = "nous:hy4-flash"
IDE_ONLY = "Internal error: Zoo Pass Pilot models are available in the Zoo Code IDE only."
WINDOW = 10  # seconds; the fake backend is instant


def _cfg(d: str) -> dict:
    cp = Path(d, "config.json")
    cp.write_text(json.dumps({"backoff_s": [1, 1]}), "utf-8")
    return {"data_dir": d, "image": "img", "model": ZOO, "backoff_s": [1, 1],
            "max_concurrent": 2, "models": {ZOO: {}, D1017: {}, HY4: {}}}


def _run_to_terminal(L, a, window=WINDOW):
    async def go():
        t0 = time.monotonic()
        while a["state"] not in launcher.TERMINAL and time.monotonic() - t0 < window:
            await L._schedule()  # a fallback re-queues the attempt; the scheduler must pick it up
            await asyncio.sleep(0.02)
        return time.monotonic() - t0
    return asyncio.run(go())


class ModelUnavailableFallback(unittest.TestCase):
    def test_zoo_ide_only_refusal_falls_back_and_completes(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_ZOO_KEY": "fixture-value-not-a-real-key",
                                              "ORBIT_WORKER_NOUS_KEY": "fixture-value-not-a-real-key"}):
                be = launcher.FakeBackend(None)
                L = launcher.Launcher(_cfg(d), be)
                L.kick = lambda: None
                a, _ = L.submit("att_mu0000000001", {"objective": "x",
                                                     "fake": {"unavailable_models": [ZOO]}})
                _run_to_terminal(L, a)
                try:
                    self.assertEqual(a["state"], "succeeded", f"detail={a.get('detail')!r}")
                    self.assertEqual(a["model"], D1017)
                    route = launcher.model_spec(L.cfg, a["model"], a.get("brief"))
                    self.assertEqual(route["key"], "nous")
                    self.assertEqual(route["base_url"], launcher.NOUS_BASE_URL)
                    history = a.get("model_history") or []
                    self.assertTrue(any((h.get("fallback") or "") == f"{ZOO}->{D1017}"
                                        or "IDE only" in (h.get("after") or "")
                                        for h in history), f"no fallback recorded: {history}")
                    self.assertEqual(be.models[:2], [ZOO, D1017],
                                     f"fallback not exercised first: {be.models}")
                finally:
                    asyncio.run(be.cleanup(a))

    def test_refusal_on_the_fallback_is_terminal_not_a_loop(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_ZOO_KEY": "fixture-value-not-a-real-key",
                                              "ORBIT_WORKER_NOUS_KEY": "fixture-value-not-a-real-key"}):
                be = launcher.FakeBackend(None)
                L = launcher.Launcher(_cfg(d), be)
                L.kick = lambda: None
                a, _ = L.submit("att_mu0000000002", {"objective": "x",
                                                     "fake": {"unavailable_models": [ZOO, D1017]}})
                _run_to_terminal(L, a)
                try:
                    self.assertEqual(a["state"], "succeeded", f"detail={a.get('detail')!r}")
                    self.assertEqual(a["model"], HY4)
                    self.assertEqual(be.models, [ZOO, D1017, HY4])
                    self.assertEqual(be.starts, 3, f"unexpected retry loop: {be.starts} starts")
                finally:
                    asyncio.run(be.cleanup(a))

    def test_unrelated_error_text_does_not_trigger_the_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_ZOO_KEY": "fixture-value-not-a-real-key",
                                              "ORBIT_WORKER_NOUS_KEY": "fixture-value-not-a-real-key"}):
                be = launcher.FakeBackend(None)
                L = launcher.Launcher(_cfg(d), be)
                L.kick = lambda: None
                a, _ = L.submit("att_mu0000000003", {"objective": "x",
                                                     "fake": {"fail_start": False,
                                                              "error_first": 1,
                                                              "error_text": "Internal error: unexpected EOF"}})
                _run_to_terminal(L, a)
                try:
                    self.assertEqual(a["model"], ZOO, "an unrelated error must not switch models")
                    self.assertFalse(a.get("fallback"), f"unexpected fallback: {a.get('fallback')!r}")
                finally:
                    asyncio.run(be.cleanup(a))


class StandardTierNoLongerRoutesToZoo(unittest.TestCase):
    """Owner routing change 2026-11-17: Zoo's free pass refuses non-IDE API traffic, so the
    "standard" tier must resolve to the host default (OpenCode Go deepseek-v4.1-flash) and an
    unnamed/tier-standard attempt must never be handed to zoo/glm-5.3-flash."""

    def test_standard_tier_maps_to_go_deepseek(self):
        self.assertEqual(launcher.TIERS["standard"], "go:deepseek-v4.1-flash")
        spec = launcher.model_spec(_cfg(tempfile.mkdtemp(prefix="tier-")), "standard")
        self.assertEqual(spec["name"], "go:deepseek-v4.1-flash")

    def test_standard_tier_attempt_runs_on_go_not_zoo(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture-value-not-a-real-key"}):
                be = launcher.FakeBackend(None)
                L = launcher.Launcher(_cfg(d), be)
                L.kick = lambda: None
                a, _ = L.submit("att_tier0000001", {"objective": "x", "tier": "standard"})
                _run_to_terminal(L, a)
                try:
                    self.assertEqual(a["model"], "go:deepseek-v4.1-flash", f"detail={a.get('detail')!r}")
                    self.assertNotIn("zoo:glm-5.3-flash", getattr(be, "models", []), f"zoo used: {getattr(be, 'models', [])}")
                finally:
                    asyncio.run(be.cleanup(a))


if __name__ == "__main__":
    unittest.main()
