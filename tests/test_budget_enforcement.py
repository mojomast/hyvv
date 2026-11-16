"""budget.timeout_s enforced as an attempt wall clock (kanban t_c16fd52d, incident att_641b8119a39a426d).

A worker that outlives its brief.budget.timeout_s must be stopped by the launcher no later than
the budget plus a small teardown grace, the attempt must land terminal with an honest timeout
detail, and the active-attempt slot must free so an admin apply-update is no longer blocked.

RED-first record (unmodified tree at e48328fe): these tests FAILED - the agy child that ignores
its --print-timeout stayed alive, the attempt stayed state='running' with detail='agy run started'
past the window, and launcher.attempt_budget_s did not exist. The budget only reached the child as
a flag plus a distant timeout+60 poll-time kill inside AgyCuaRunner.events; no attempt wall clock.
Run: python3 tests/test_budget_enforcement.py   (no cua, no Docker, no network, no real agy)"""
import asyncio, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cua", mock.MagicMock())
import launcher  # noqa: E402

# A child that ignores its own deadline: sleeps far past any --print-timeout the launcher passes.
AGY_CHILD_IGNORES_DEADLINE = "import time; time.sleep(600)\n"
BUDGET = 2  # brief.budget.timeout_s used by the wall-clock tests (seconds)
WINDOW = BUDGET + 8  # observation window; on the fixed tree terminal lands ~budget + epsilon


def _cfg(d):
    cp = Path(d, "config.json"); cp.write_text(json.dumps({"bind": "127.0.0.1", "data_dir": d}))
    return launcher.load_config(cp)


def _agy_backend(d):
    """CuaBackend with a mocked cua SDK and a real AgyCuaRunner whose agy is the sleepy fake child."""
    be = launcher.CuaBackend.__new__(launcher.CuaBackend)
    be.cfg = _cfg(d); be.cfg["agy_quota"] = False
    be.cua = mock.MagicMock(); sb = mock.MagicMock(); sb.id.return_value = "sbx-1"
    sbs = mock.MagicMock(); sbs.create = mock.AsyncMock(return_value=sb); sbs.delete = mock.AsyncMock()
    be.cua.embedded.return_value.sandboxes.return_value = sbs
    be.client = mock.MagicMock(); be.client.sandboxes.side_effect = AssertionError("agy-cua must not use the shared default Cua state")
    be._sb, be._sdc = {}, {}
    be.agy = launcher.AgyCuaRunner(be)
    fake = Path(d, "fake_agy.py"); fake.write_text(AGY_CHILD_IGNORES_DEADLINE)
    real = launcher.agy_argv
    return be, (lambda cfg, p, m, t: [sys.executable, str(fake)] + real(cfg, p, m, t)[1:])


async def _run_control_until_terminal(L, a, window):
    """Drive L._control(a) as a task; return (task, seconds until a terminal state or None)."""
    task = asyncio.ensure_future(L._control(a))
    t0 = time.monotonic()
    while a["state"] not in launcher.TERMINAL and time.monotonic() - t0 < window:
        await asyncio.sleep(0.02)
    elapsed = time.monotonic() - t0 if a["state"] in launcher.TERMINAL else None
    if not task.done():
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    return task, elapsed


class AgyChildIgnoresDeadline(unittest.TestCase):
    """The incident shape: agy-cua, child hangs past its own --print-timeout."""

    def test_child_reaped_and_attempt_terminal_at_budget(self):
        with tempfile.TemporaryDirectory() as d:
            be, argv_fn = _agy_backend(d)
            be.cfg["models"] = {"gemini-4-argon": {}}  # host allow-list entry, as an agy host configures
            L = launcher.Launcher(be.cfg, be); L.kick = lambda: None
            grace = getattr(launcher, "BUDGET_STOP_GRACE_S", 15)
            with mock.patch.object(launcher, "AGY_TIMEOUT_S", (1, 2, 4)), \
                 mock.patch.object(launcher, "agy_argv", argv_fn):
                a, _ = L.submit("att_bud00000001", {"objective": "x", "budget": {"timeout_s": BUDGET}}, "complex", "agy-cua")
                L._set(a, "starting", "creating sandbox")
                _, elapsed = asyncio.run(_run_control_until_terminal(L, a, WINDOW))
                r = be.agy.procs.get(a.get("run_id")) or {}
                try:
                    self.assertEqual(a["state"], "failed", f"attempt not terminal: {a.get('detail')!r}")
                    self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                    self.assertLessEqual(elapsed, BUDGET + grace + 5, f"budget not authoritative: terminal after {elapsed:.1f}s")
                    self.assertIn("budget timeout", (a.get("detail") or "").lower())
                    self.assertIn(f"timeout_s={BUDGET}", a.get("detail") or "")
                    self.assertIsNotNone(r.get("p"))
                    self.assertIsNotNone(r["p"].poll(), "child that ignored its deadline was not reaped")
                    self.assertEqual(L._active(), [], "runaway attempt still holds the worker slot")
                finally:
                    try: asyncio.run(be.cleanup(a))
                    except Exception: pass


class OpencodeWorkerOutlivesBudget(unittest.TestCase):
    def _L(self, d):
        L = launcher.Launcher(_cfg(d), launcher.FakeBackend(None)); L.kick = lambda: None
        return L

    def test_worker_stopped_slot_freed_and_queued_attempt_starts(self):
        with tempfile.TemporaryDirectory() as d:
            L = self._L(d)
            grace = getattr(launcher, "BUDGET_STOP_GRACE_S", 15)
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": "fixture-value-not-a-real-key"}):
                a1, _ = L.submit("att_bud00000002", {"objective": "x", "budget": {"timeout_s": BUDGET},
                                                     "fake": {"delay": 10 ** 9}})
                a2, _ = L.submit("att_bud00000003", {"objective": "x", "fake": {}})

                async def go():
                    await L._schedule()  # one worker slot: a1 starts, a2 queues behind it
                    t0 = time.monotonic()
                    while a1["state"] not in launcher.TERMINAL and time.monotonic() - t0 < WINDOW:
                        await asyncio.sleep(0.02)
                    first_elapsed = time.monotonic() - t0 if a1["state"] in launcher.TERMINAL else None
                    await L._schedule()  # freed slot: the queued attempt must start
                    t1 = time.monotonic()
                    while a2["state"] not in launcher.TERMINAL and time.monotonic() - t1 < WINDOW:
                        await asyncio.sleep(0.02)
                    return first_elapsed

                first_elapsed = asyncio.run(go())
                try:
                    self.assertEqual(a1["state"], "failed", f"runaway worker not stopped: {a1.get('detail')!r}")
                    self.assertIsNotNone(first_elapsed)
                    self.assertLessEqual(first_elapsed, BUDGET + grace + 5, f"budget not authoritative: {first_elapsed:.1f}s")
                    self.assertIn("budget timeout", (a1.get("detail") or "").lower())
                    self.assertEqual(L._active(), [], "runaway attempt still holds the worker slot")
                    self.assertEqual(a2["state"], "succeeded", "queued attempt did not start after the slot freed (apply-update stays blocked)")
                finally:
                    for x in (a1, a2):
                        r = L.backend.runs.get(x.get("run_id") or "")
                        if r: r["delay"] = 0

    def test_resumed_attempt_keeps_its_original_budget_clock(self):
        with tempfile.TemporaryDirectory() as d:
            L = self._L(d)
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": "fixture-value-not-a-real-key"}):
                a = {"attempt_id": "att_bud00000004", "kind": "opencode", "state": "running", "detail": "opencode run started",
                     "run_id": "run-resume000001", "cursor": 0, "lseq": 0, "created_at": time.time(), "updated_at": time.time(),
                     "rate_limited": 0, "model": "deepseek-flash", "brief": {"objective": "x", "budget": {"timeout_s": BUDGET}},
                     "budget_started_at": time.time() - (BUDGET + 1)}
                L.attempts[a["attempt_id"]] = a
                L.backend.runs[a["run_id"]] = {"events": [], "delay": 10 ** 9, "t0": time.time(), "cancelled": False}
                _, elapsed = asyncio.run(_run_control_until_terminal(L, a, WINDOW))
                self.assertEqual(a["state"], "failed", f"resumed runaway not stopped: {a.get('detail')!r}")
                self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                self.assertIn("budget timeout", (a.get("detail") or "").lower())

    def test_healthy_run_within_budget_is_not_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            L = self._L(d)
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": "fixture-value-not-a-real-key"}):
                a, _ = L.submit("att_bud00000005", {"objective": "x", "budget": {"timeout_s": 60}, "fake": {}})
                _, elapsed = asyncio.run(_run_control_until_terminal(L, a, WINDOW))
                self.assertEqual(a["state"], "succeeded", f"healthy run disturbed: {a.get('detail')!r}")
                self.assertEqual(a.get("detail"), "turn ended: end_turn")
                self.assertNotIn("budget timeout", (a.get("detail") or "").lower())
                self.assertLess(elapsed, 5)


class BudgetResolution(unittest.TestCase):
    """attempt_budget_s must reuse the existing per-kind clamps, never invent new ones."""

    def test_agy_clamp_preserved(self):
        base = {"kind": "agy-cua", "mode": "ui", "brief": {"objective": "x"}}
        self.assertEqual(launcher.attempt_budget_s(base), launcher.AGY_TIMEOUT_S[1])
        self.assertEqual(launcher.attempt_budget_s({**base, "brief": {"objective": "x", "budget": {"timeout_s": 5}}}), launcher.AGY_TIMEOUT_S[0])
        self.assertEqual(launcher.attempt_budget_s({**base, "brief": {"objective": "x", "budget": {"timeout_s": 99999}}}), launcher.AGY_TIMEOUT_S[2])
        self.assertEqual(launcher.attempt_budget_s({**base, "mode": "code", "brief": {"objective": "x"}}), 1800)

    def test_opencode_enforces_only_a_configured_budget(self):
        oc = {"kind": "opencode", "brief": {"objective": "x"}}
        self.assertIsNone(launcher.attempt_budget_s(oc))
        self.assertEqual(launcher.attempt_budget_s({**oc, "brief": {"objective": "x", "budget": {"timeout_s": 1200}}}), 1200)
        self.assertIsNone(launcher.attempt_budget_s({**oc, "brief": {"objective": "x", "budget": {"timeout_s": 0}}}))
        self.assertIsNone(launcher.attempt_budget_s({**oc, "brief": {"objective": "x", "budget": {"timeout_s": "soon"}}}))

    def test_check_keeps_per_command_clamp(self):
        self.assertEqual(launcher.check_timeout_s({"budget": {"timeout_s": 5}}), launcher.CHECK_TIMEOUT_S[0])
        self.assertEqual(launcher.check_timeout_s({}), launcher.CHECK_TIMEOUT_S[1])
        self.assertEqual(launcher.check_timeout_s({"budget": {"timeout_s": 99999}}), launcher.CHECK_TIMEOUT_S[2])
        attempt = {"kind": "check", "brief": {"objective": "x", "budget": {"timeout_s": 100}, "commands": ["a", "b", "c"]}}
        self.assertGreaterEqual(launcher.attempt_budget_s(attempt), 100 * 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
