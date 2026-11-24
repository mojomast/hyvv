"""RED for wave A slice HY-1 (review Fix now #2): every attempt has a wall clock.

Today an opencode brief without budget.timeout_s gets no attempt wall clock,
so a stuck bee holds a concurrency slot forever. The host applies a default
(DEFAULT_ATTEMPT_TIMEOUT_S, overridable as config default_timeout_s), and a
host that sets require_budget refuses briefs without a positive budget.
Refusal is opt-in so POST /v1/attempts stays additive for existing clients.
Run: python3 -m pytest -q tests/test_default_wall_clock.py   (no cua, no network)
"""
import asyncio, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cua", mock.MagicMock())
import launcher  # noqa: E402

KEY_ENV = {"ORBIT_WORKER_GO_KEY": "fixture-value-not-a-real-key"}


def _cfg(d, **extra):
    cp = Path(d, "config.json"); cp.write_text(json.dumps({"bind": "127.0.0.1", "data_dir": d, **extra}))
    return launcher.load_config(cp)


class DefaultResolution(unittest.TestCase):
    def test_constant_is_a_sane_ceiling(self):
        self.assertTrue(60 <= launcher.DEFAULT_ATTEMPT_TIMEOUT_S <= 24 * 3600)

    def test_load_config_sets_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_cfg(d)["default_timeout_s"], launcher.DEFAULT_ATTEMPT_TIMEOUT_S)
            self.assertEqual(_cfg(d, default_timeout_s=900)["default_timeout_s"], 900)

    def test_load_config_clamps_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_cfg(d, default_timeout_s=1)["default_timeout_s"], 60)
            self.assertEqual(_cfg(d, default_timeout_s=10 ** 9)["default_timeout_s"], 24 * 3600)
            self.assertEqual(_cfg(d, default_timeout_s="soon")["default_timeout_s"], launcher.DEFAULT_ATTEMPT_TIMEOUT_S)

    def test_opencode_without_budget_gets_the_host_default(self):
        oc = {"kind": "opencode", "brief": {"objective": "x"}}
        self.assertEqual(launcher.attempt_budget_s(oc), launcher.DEFAULT_ATTEMPT_TIMEOUT_S)
        self.assertEqual(launcher.attempt_budget_s(oc, {"default_timeout_s": 900}), 900)

    def test_invalid_explicit_budget_falls_back_to_the_default(self):
        for raw in (0, -5, "soon", None):
            oc = {"kind": "opencode", "brief": {"objective": "x", "budget": {"timeout_s": raw}}}
            self.assertEqual(launcher.attempt_budget_s(oc, {"default_timeout_s": 900}), 900, raw)

    def test_explicit_budget_still_wins(self):
        oc = {"kind": "opencode", "brief": {"objective": "x", "budget": {"timeout_s": 1200}}}
        self.assertEqual(launcher.attempt_budget_s(oc, {"default_timeout_s": 900}), 1200)


class RequireBudget(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIsNone(launcher.validate_brief({"objective": "x"}, {}))
        self.assertIsNone(launcher.validate_brief({"objective": "x"}))

    def test_refuses_missing_budget_when_required(self):
        err = launcher.validate_brief({"objective": "x"}, {"require_budget": True})
        self.assertIsNotNone(err)
        self.assertIn("budget.timeout_s", err)

    def test_refuses_non_positive_budget_when_required(self):
        for raw in (0, -1, "soon"):
            err = launcher.validate_brief({"objective": "x", "budget": {"timeout_s": raw}}, {"require_budget": True})
            self.assertIsNotNone(err, raw)

    def test_accepts_positive_budget_when_required(self):
        self.assertIsNone(launcher.validate_brief({"objective": "x", "budget": {"timeout_s": 60}}, {"require_budget": True}))


class UnbudgetedRunawayIsStopped(unittest.TestCase):
    """The incident shape with no budget at all: the host default must stop it."""

    def test_runaway_without_budget_lands_terminal_at_the_host_default(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d)
            cfg["default_timeout_s"] = 2  # below the config clamp on purpose: the test drives the clock
            L = launcher.Launcher(cfg, launcher.FakeBackend(None)); L.kick = lambda: None
            with mock.patch.dict(os.environ, KEY_ENV):
                a, _ = L.submit("att_dflt0000001", {"objective": "x", "fake": {"delay": 10 ** 9}})

                async def go():
                    task = asyncio.ensure_future(L._control(a))
                    t0 = time.monotonic()
                    while a["state"] not in launcher.TERMINAL and time.monotonic() - t0 < 30:
                        await asyncio.sleep(0.05)
                    if not task.done():
                        task.cancel()
                        try: await task
                        except asyncio.CancelledError: pass

                try:
                    asyncio.run(go())
                    self.assertEqual(a["state"], "failed", a.get("detail"))
                    self.assertIn("budget timeout", (a.get("detail") or "").lower())
                finally:
                    r = L.backend.runs.get(a.get("run_id") or "")
                    if r: r["delay"] = 0


if __name__ == "__main__":
    unittest.main(verbosity=2)
