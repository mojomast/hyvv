"""budget.timeout_s enforced as an attempt wall clock (kanban t_c16fd52d, incident att_641b8119a39a426d).

A worker that outlives its brief.budget.timeout_s must be stopped by the launcher no later than
the budget plus a small teardown grace, the attempt must land terminal with an honest timeout
detail, and the active-attempt slot must free so an admin apply-update is no longer blocked.

RED-first record (unmodified tree at e48328fe): these tests FAILED - the agy child that ignores
its --print-timeout stayed alive, the attempt stayed state='running' with detail='agy run started'
past the window, and launcher.attempt_budget_s did not exist. The budget only reached the child as
a flag plus a distant timeout+60 poll-time kill inside AgyCuaRunner.events; no attempt wall clock.
Run: python3 tests/test_budget_enforcement.py   (no cua, no Docker, no network, no real agy)

RED-first record for the #18 review followups (unmodified tree at b7c724d1, base of this branch):
RunFinishedInsideBudgetWasForceFailed FAILED with the reviewer's repro shape - a ~1.5 s run against
the 2 s budget landed state='failed' with detail='budget timeout: worker outlived
brief.budget.timeout_s=2s (launcher stopped the run after 2s)' and no result; both
BudgetStopCrashWindow tests FAILED with the attempt wedged at state='running' forever after a
restart (budget_stop marked, terminal write never happened, nothing ever finishes it).

RED-first record for the #20 review followups (unmodified tree at 58610f3, base of this branch):
HangingResultCollectionAfterBudgetStop FAILED - a drain terminal event plus a backend.result()
that never returns left the attempt state='running' with budget_stop set past the window, no
terminal write, no cleanup, slot never freed (the exact failure class #18 fixed, reopened by the
unbounded _finish wait); MultiPageEventDrain FAILED with the reviewer's repro shape - a finished
run whose events span more than one SDK page (CuaBackend requests 200 per call) landed
state='failed' with detail='budget timeout: worker outlived brief.budget.timeout_s=2s (launcher
stopped the run after 2s)' and no result manifest; SuccessPathCleanupCount FAILED - the
budget-stop success path tore the worker down twice; RestoreVerdictMatchesTerminalWriteRule
FAILED - a status completed / stop_reason max_tokens manifest restored as succeeded while the
live terminal write lands the same shape failed."""
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


async def _run_control_until_terminal(L, a, window, settle=0.0):
    """Drive L._control(a) as a task; return (task, seconds until a terminal state or None).

    settle: seconds to keep the loop alive after the terminal state (or the cancel) so a shielded
    teardown running outside the observed task can finish before the caller asserts on it."""
    task = asyncio.ensure_future(L._control(a))
    t0 = time.monotonic()
    while a["state"] not in launcher.TERMINAL and time.monotonic() - t0 < window:
        await asyncio.sleep(0.02)
    elapsed = time.monotonic() - t0 if a["state"] in launcher.TERMINAL else None
    if not task.done():
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    if settle: await asyncio.sleep(settle)
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


class _HungBackend:
    """The watchdog's reason to exist: a backend call that never returns, so the controller's
    polling loop - and its own fast-path deadline check with it - can never fire again."""

    def __init__(self):
        self.cleaned = []

    async def start(self, a, key): return {"sandbox": "fake", "run_id": "run-hang0000001"}
    async def events(self, a, cursor): await asyncio.Future()  # wedged backend call
    async def interrupt(self, a): pass
    async def cleanup(self, a): self.cleaned.append(a.get("run_id"))


class _CountingBackend(launcher.FakeBackend):
    """FakeBackend plus a cleanup counter: teardown-once claims are measured, not trusted."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cleaned = []

    async def cleanup(self, a): self.cleaned.append(a.get("run_id"))


class _PagedBackend(_CountingBackend):
    """CuaBackend pages worker events (launcher.py: 200 per backend.events call): one poll can
    leave a terminal event sitting in the next page."""

    PAGE = 200

    async def events(self, a, cursor):
        r = self.runs[a["run_id"]]
        if r["cancelled"]: return [], cursor
        if time.time() - r["t0"] < r["delay"]: return [], cursor
        out = [e for e in r["events"] if e["seq"] > cursor][:self.PAGE]
        return out, (out[-1]["seq"] if out else cursor)


class _CollectHangBackend(_CountingBackend):
    """The drain finds the terminal event, then backend.result() never returns: the exact shape
    that left budget-stop attempts running forever before the finish wait was bounded."""

    async def result(self, a):
        await asyncio.Future()  # collection never returns


class _ConcurrentTeardownBackend(launcher.FakeBackend):
    """A backend whose calls really suspend, so two racing teardowns interleave the way they do
    against the real SDK (every backend call crosses the network and yields); a fully inline fake
    would serialize the racers and never reach the guard under test. Crash-window shape: the
    worker is gone, so events and interrupt raise."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cleaned = []

    async def events(self, a, cursor):
        await asyncio.sleep(0)  # real suspension point, like the SDK's network calls
        raise KeyError("no such run")

    async def interrupt(self, a):
        await asyncio.sleep(0)

    async def cleanup(self, a):
        await asyncio.sleep(0)
        self.cleaned.append(a.get("run_id"))


def _crash_window_attempt(aid, project=None):
    """The journal exactly as a crash between the budget_stop mark and the terminal write leaves it:
    the deadline had been reached (budget_stop was saved), the worker is gone with the dead process,
    and no terminal state was ever recorded."""
    now = time.time()
    return {"attempt_id": aid, "kind": "opencode", "state": "running", "detail": "opencode run started",
            "run_id": "run-crash" + aid[-8:], "cursor": 0, "lseq": 3, "created_at": now - 10, "updated_at": now - 1,
            "rate_limited": 0, "model": "deepseek-flash",
            "brief": {"objective": "x", "budget": {"timeout_s": BUDGET}, **({"project": project} if project else {})},
            "budget_started_at": now - (BUDGET + 1), "budget_stop": True}


class RunFinishedInsideBudgetWasForceFailed(unittest.TestCase):
    """F1 (event-drain race): the deadline check precedes the event drain, so a poll loop that
    wakes at/after the deadline force-fails a run that finished INSIDE its budget - a false
    'worker outlived' detail, and the collected result is lost. Reviewer repro: a ~1.5 s run
    against a 2 s budget force-failed at ~2.01 s."""

    def test_run_that_finishes_inside_its_budget_lands_succeeded_with_its_result(self):
        with tempfile.TemporaryDirectory() as d:
            L = launcher.Launcher(_cfg(d), launcher.FakeBackend(None)); L.kick = lambda: None
            grace = getattr(launcher, "BUDGET_STOP_GRACE_S", 15)
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": "fixture-value-not-a-real-key"}):
                # worker needs 1.5 s (inside the 2 s budget); the poll loop's 1 s cadence puts the
                # next drain at ~2.0 s, straddling the deadline - exactly the reviewer's repro.
                a, _ = L.submit("att_bud00000006", {"objective": "x", "budget": {"timeout_s": BUDGET},
                                                    "fake": {"delay": 1.5}})
                _, elapsed = asyncio.run(_run_control_until_terminal(L, a, WINDOW))
                man = Path(d, "attempts", "att_bud00000006.result.json")
                try:
                    self.assertEqual(a["state"], "succeeded",
                                     f"a run that finished inside its budget was force-failed: {a.get('detail')!r}")
                    self.assertEqual(a.get("detail"), "turn ended: end_turn")
                    self.assertNotIn("budget timeout", (a.get("detail") or "").lower())
                    self.assertNotIn("outlived", (a.get("detail") or "").lower())
                    self.assertTrue(man.exists(), "the finished run's result was never collected")
                    self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                    self.assertLessEqual(elapsed, BUDGET + 5, f"budget not authoritative: terminal after {elapsed:.1f}s")
                    self.assertEqual(L._active(), [], "finished attempt still holds the worker slot")
                finally:
                    r = L.backend.runs.get(a.get("run_id") or "")
                    if r: r["delay"] = 0
                    L.loop.close()


class BudgetStopCrashWindow(unittest.TestCase):
    """F2 (crash-window wedge): a crash (or kill) between the budget_stop mark and the terminal
    write leaves the attempt non-terminal forever - it is not in _active(), so no restart path
    ever finishes it, and the nonterminal attempt keeps apply-update blocked."""

    def _restart(self, d, a, result=None):
        (Path(d, "attempts")).mkdir(parents=True, exist_ok=True)
        (Path(d, "attempts", a["attempt_id"] + ".json")).write_text(json.dumps(a), "utf-8")
        if result is not None:
            (Path(d, "attempts", a["attempt_id"] + ".result.json")).write_text(json.dumps(result, indent=1), "utf-8")
        L = launcher.Launcher(_cfg(d), launcher.FakeBackend(None)); L.kick = lambda: None
        loaded = L.attempts[a["attempt_id"]]

        async def go():
            await L._schedule()  # startup recovery path: serve() kicks once, this is that kick
            t0 = time.monotonic()
            while loaded["state"] not in launcher.TERMINAL and time.monotonic() - t0 < WINDOW:
                await asyncio.sleep(0.02)
            return time.monotonic() - t0 if loaded["state"] in launcher.TERMINAL else None

        return L, loaded, asyncio.run(go())

    def test_restart_completes_a_budget_stop_the_crash_interrupted(self):
        with tempfile.TemporaryDirectory() as d:
            a = _crash_window_attempt("att_bud00000010", project="proj")
            L, a, elapsed = self._restart(d, a)
            try:
                self.assertEqual(a["state"], "failed", f"crash-window attempt wedged forever: {a.get('detail')!r}")
                self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                self.assertIn("budget timeout", (a.get("detail") or "").lower())
                self.assertIn(f"timeout_s={BUDGET}", a.get("detail") or "")
                self.assertNotIn("outlived", (a.get("detail") or "").lower(),
                                 "the worker's fate is unknowable after the crash; the detail must not claim it")
                self.assertEqual(launcher.active_attempts_for(L.attempts, "proj"), 0,
                                 "wedged nonterminal attempt keeps apply-update blocked")
                self.assertEqual(L._active(), [], "crash-window attempt still holds the worker slot")
            finally:
                L.loop.close()

    def test_restart_restores_an_already_collected_result(self):
        with tempfile.TemporaryDirectory() as d:
            a = _crash_window_attempt("att_bud00000012", project="proj")
            result = {"schema": "orbit.local-worker.result/v1", "attempt_id": a["attempt_id"],
                      "source": {"kind": "opencode", "harness": "opencode"},
                      "observed": {"status": "completed", "stop_reason": "end_turn", "error": None}}
            L, a, elapsed = self._restart(d, a, result)
            try:
                self.assertEqual(a["state"], "succeeded",
                                 f"a collected result was thrown away by the crash window: {a.get('detail')!r}")
                self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                self.assertIn(f"timeout_s={BUDGET}", a.get("detail") or "")
                self.assertEqual(launcher.active_attempts_for(L.attempts, "proj"), 0,
                                 "wedged nonterminal attempt keeps apply-update blocked")
                self.assertEqual(L._active(), [], "crash-window attempt still holds the worker slot")
            finally:
                L.loop.close()

    def test_restore_verdict_matches_the_terminal_write_rule(self):
        """The restore rule must judge a manifest the way the live terminal write judges the same
        shape: the terminal write lands stop_reason max_tokens FAILED, so restoring a status
        completed / stop_reason max_tokens manifest as succeeded is a false success."""
        with tempfile.TemporaryDirectory() as d:
            a = _crash_window_attempt("att_bud00000013", project="proj")
            result = {"schema": "orbit.local-worker.result/v1", "attempt_id": a["attempt_id"],
                      "source": {"kind": "opencode", "harness": "opencode"},
                      "observed": {"status": "completed", "stop_reason": "max_tokens", "error": None}}
            L, a, elapsed = self._restart(d, a, result)
            try:
                self.assertEqual(a["state"], "failed",
                                 f"a max_tokens manifest restored as succeeded: {a.get('detail')!r}")
                self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                self.assertIn(f"timeout_s={BUDGET}", a.get("detail") or "")
                self.assertNotIn("outlived", (a.get("detail") or "").lower(),
                                 "the manifest proves the run finished; the detail must not claim outliving")
                self.assertEqual(launcher.active_attempts_for(L.attempts, "proj"), 0,
                                 "wedged nonterminal attempt keeps apply-update blocked")
            finally:
                L.loop.close()


class HangingResultCollectionAfterBudgetStop(unittest.TestCase):
    """F3 (bounded-wait): at the budget stop the drain finds a terminal event and _finish runs -
    but backend.result() hangs, and the old tree awaited it unbounded. The attempt stayed
    state='running' with budget_stop set forever: no terminal write, no cleanup, slot never
    freed - the exact failure class #18 fixed, reopened through the success branch."""

    def test_hanging_result_collection_is_bounded_and_still_cleans_up_once(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d); be = _CollectHangBackend(cfg)
            L = launcher.Launcher(cfg, be)
            kicks = []; L.kick = lambda: kicks.append(1)
            grace = getattr(launcher, "BUDGET_STOP_GRACE_S", 15)
            now = time.time()
            a = {"attempt_id": "att_bud00000020", "kind": "opencode", "state": "running",
                 "detail": "opencode run started", "run_id": "run-hangresult01", "cursor": 0, "lseq": 0,
                 "created_at": now, "updated_at": now, "rate_limited": 0, "model": "deepseek-flash",
                 "brief": {"objective": "x", "budget": {"timeout_s": BUDGET}}, "budget_started_at": now - (BUDGET + 1)}
            L.attempts[a["attempt_id"]] = a
            L.backend.runs[a["run_id"]] = {"events": [{"kind": "turn_started", "seq": 1},
                                                      {"kind": "message", "text": "done", "seq": 2},
                                                      {"kind": "turn_ended", "stop_reason": "end_turn", "seq": 3}],
                                           "delay": 0, "t0": time.time(), "cancelled": False}
            with mock.patch.object(launcher, "BUDGET_STOP_GRACE_S", 1):  # bound the finish wait fast in-test
                _, elapsed = asyncio.run(_run_control_until_terminal(L, a, WINDOW, settle=1.0))
            try:
                self.assertEqual(a["state"], "failed",
                                 f"a hanging collection wedged the budget stop forever: {a.get('detail')!r}")
                self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                self.assertLessEqual(elapsed, BUDGET + 2 * grace + 5, f"budget stop unbounded: terminal after {elapsed:.1f}s")
                self.assertIn("budget timeout", (a.get("detail") or "").lower())
                self.assertIn("collecting its result failed", (a.get("detail") or "").lower())
                self.assertIn(f"timeout_s={BUDGET}", a.get("detail") or "")
                self.assertTrue(a.get("budget_stop"), "the stop was not marked budget_stop")
                self.assertEqual(len(be.cleaned), 1, f"teardown cleaned up {len(be.cleaned)} times, not exactly once")
                self.assertGreaterEqual(len(kicks), 1, "the slot kick never happened after the stop")
                self.assertEqual(L._active(), [], "budget-stop attempt still holds the worker slot")
            finally:
                L.loop.close()


class MultiPageEventDrain(unittest.TestCase):
    """F4 (single-page drain): CuaBackend pages worker events (200 per call) and the budget-stop
    drain polled exactly once - a run that finished INSIDE its budget whose terminal event sits
    past the first page got a false 'worker outlived' verdict and its result was never collected.
    Reviewer repro: run finished at 1.5 s against a 2 s budget, still failed with no manifest."""

    def test_run_whose_events_span_two_pages_lands_succeeded_with_its_result(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d); be = _PagedBackend(cfg)
            L = launcher.Launcher(cfg, be); L.kick = lambda: None
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": "fixture-value-not-a-real-key"}):
                # 200 non-terminal events fill page 1; the terminal event sits on page 2. The worker
                # needs 1.5 s (inside the 2 s budget), so the deadline check fires before any poll
                # consumed a page and the whole stream is still pending at the stop.
                a, _ = L.submit("att_bud00000021", {"objective": "x", "budget": {"timeout_s": BUDGET},
                                                    "fake": {"delay": 1.5,
                                                             "events": [{"kind": "message", "text": "step"}] * _PagedBackend.PAGE
                                                                       + [{"kind": "turn_ended", "stop_reason": "end_turn"}]}})
                man = Path(d, "attempts", "att_bud00000021.result.json")
                _, elapsed = asyncio.run(_run_control_until_terminal(L, a, WINDOW))
                try:
                    self.assertEqual(a["state"], "succeeded",
                                     f"a finished run past the first event page was force-failed: {a.get('detail')!r}")
                    self.assertEqual(a.get("detail"), "turn ended: end_turn")
                    self.assertNotIn("outlived", (a.get("detail") or "").lower())
                    self.assertNotIn("budget timeout", (a.get("detail") or "").lower())
                    self.assertTrue(man.exists(), "the finished run's result was never collected")
                    self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                    self.assertLessEqual(elapsed, BUDGET + 5, f"budget not authoritative: terminal after {elapsed:.1f}s")
                    self.assertEqual(a.get("cursor"), _PagedBackend.PAGE + 1,
                                     "the drain stopped at the first SDK page instead of exhausting the stream")
                    self.assertEqual(L._active(), [], "finished attempt still holds the worker slot")
                finally:
                    r = be.runs.get(a.get("run_id") or "")
                    if r: r["delay"] = 0
                    L.loop.close()


class SuccessPathCleanupCount(unittest.TestCase):
    """F5 (double teardown): the budget-stop success path ran _finish (which cleans up) and then
    the teardown's own finally cleaned up again - two teardowns where the stop owns exactly one."""

    def test_budget_stop_success_path_cleans_up_exactly_once(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d); be = _CountingBackend(cfg)
            L = launcher.Launcher(cfg, be); L.kick = lambda: None
            with mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": "fixture-value-not-a-real-key"}):
                a, _ = L.submit("att_bud00000022", {"objective": "x", "budget": {"timeout_s": BUDGET},
                                                    "fake": {"delay": 1.5}})
                _, elapsed = asyncio.run(_run_control_until_terminal(L, a, WINDOW))
                try:
                    self.assertEqual(a["state"], "succeeded", f"success path disturbed: {a.get('detail')!r}")
                    self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window")
                    self.assertEqual(len(be.cleaned), 1,
                                     f"the success path tore the worker down {len(be.cleaned)} times, not exactly once")
                finally:
                    r = be.runs.get(a.get("run_id") or "")
                    if r: r["delay"] = 0
                    L.loop.close()


class BudgetStopSingleFlight(unittest.TestCase):
    """Discriminator for the in-memory single-flight guard: two teardowns racing for the same
    budget-stop attempt (two restart recoveries spawned back to back, or a watchdog racing its
    controller) must tear down exactly once. Without the guard both run the full teardown."""

    def test_concurrent_budget_finishes_tear_down_exactly_once(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d); be = _ConcurrentTeardownBackend(cfg)
            L = launcher.Launcher(cfg, be); L.kick = lambda: None
            now = time.time()
            a = {"attempt_id": "att_bud00000023", "kind": "opencode", "state": "running",
                 "detail": "opencode run started", "run_id": "run-singlefligh1", "cursor": 0, "lseq": 0,
                 "created_at": now, "updated_at": now, "rate_limited": 0, "model": "deepseek-flash",
                 "brief": {"objective": "x", "budget": {"timeout_s": BUDGET}},
                 "budget_started_at": now - (BUDGET + 1), "budget_stop": True}
            L.attempts[a["attempt_id"]] = a

            async def go():
                await asyncio.gather(L._budget_finish(a, BUDGET, a["budget_started_at"], recover=True),
                                     L._budget_finish(a, BUDGET, a["budget_started_at"], recover=True))

            asyncio.run(go())
            self.assertEqual(a["state"], "failed", f"the attempt never landed: {a.get('detail')!r}")
            self.assertIn(f"timeout_s={BUDGET}", a.get("detail") or "")
            self.assertEqual(len(be.cleaned), 1,
                             f"two racing teardowns cleaned up {len(be.cleaned)} times; the guard must keep it at one")
            L.loop.close()


class WatchdogStopsAControllerTheLoopCannotSave(unittest.TestCase):
    """T1: the watchdog path must be genuinely exercised. The controller here is wedged inside a
    backend call when the deadline passes, so the loop's own fast-path check can never fire:
    without _budget_watchdog nothing stops this attempt, and it stays non-terminal forever."""

    def test_watchdog_stops_attempt_when_controller_is_wedged_in_a_backend_call(self):
        with tempfile.TemporaryDirectory() as d:
            be = _HungBackend()
            L = launcher.Launcher(_cfg(d), be); L.kick = lambda: None
            grace = getattr(launcher, "BUDGET_STOP_GRACE_S", 15)
            now = time.time()
            a = {"attempt_id": "att_bud00000011", "kind": "opencode", "state": "running",
                 "detail": "opencode run started", "run_id": "run-hang0000001", "cursor": 0, "lseq": 0,
                 "created_at": now, "updated_at": now, "rate_limited": 0, "model": "deepseek-flash",
                 "brief": {"objective": "x", "budget": {"timeout_s": BUDGET}}, "budget_started_at": now}
            L.attempts[a["attempt_id"]] = a
            _, elapsed = asyncio.run(_run_control_until_terminal(L, a, WINDOW))
            self.assertEqual(a["state"], "failed", f"wedged controller was never stopped: {a.get('detail')!r}")
            self.assertIsNotNone(elapsed, "attempt never reached a terminal state inside the window (no watchdog enforcement)")
            self.assertLessEqual(elapsed, BUDGET + grace + 5, f"budget not authoritative: terminal after {elapsed:.1f}s")
            self.assertIn("budget timeout", (a.get("detail") or "").lower())
            self.assertIn(f"timeout_s={BUDGET}", a.get("detail") or "")
            self.assertEqual(L._active(), [], "wedged attempt still holds the worker slot")
            self.assertIn("run-hang0000001", be.cleaned, "budget stop did not clean the wedged attempt up")
            L.loop.close()


class BudgetResolution(unittest.TestCase):
    """attempt_budget_s must reuse the existing per-kind clamps, never invent new ones."""

    def test_agy_clamp_preserved(self):
        base = {"kind": "agy-cua", "mode": "ui", "brief": {"objective": "x"}}
        self.assertEqual(launcher.attempt_budget_s(base), launcher.AGY_TIMEOUT_S[1])
        self.assertEqual(launcher.attempt_budget_s({**base, "brief": {"objective": "x", "budget": {"timeout_s": 5}}}), launcher.AGY_TIMEOUT_S[0])
        self.assertEqual(launcher.attempt_budget_s({**base, "brief": {"objective": "x", "budget": {"timeout_s": 99999}}}), launcher.AGY_TIMEOUT_S[2])
        self.assertEqual(launcher.attempt_budget_s({**base, "mode": "code", "brief": {"objective": "x"}}), 1800)

    def test_opencode_without_a_valid_budget_gets_the_host_default(self):
        # Wave A slice HY-1 (review Fix now #2): no budget no longer means no wall clock.
        oc = {"kind": "opencode", "brief": {"objective": "x"}}
        self.assertEqual(launcher.attempt_budget_s(oc), launcher.DEFAULT_ATTEMPT_TIMEOUT_S)
        self.assertEqual(launcher.attempt_budget_s({**oc, "brief": {"objective": "x", "budget": {"timeout_s": 1200}}}), 1200)
        self.assertEqual(launcher.attempt_budget_s({**oc, "brief": {"objective": "x", "budget": {"timeout_s": 0}}}), launcher.DEFAULT_ATTEMPT_TIMEOUT_S)
        self.assertEqual(launcher.attempt_budget_s({**oc, "brief": {"objective": "x", "budget": {"timeout_s": "soon"}}}), launcher.DEFAULT_ATTEMPT_TIMEOUT_S)

    def test_check_keeps_per_command_clamp(self):
        self.assertEqual(launcher.check_timeout_s({"budget": {"timeout_s": 5}}), launcher.CHECK_TIMEOUT_S[0])
        self.assertEqual(launcher.check_timeout_s({}), launcher.CHECK_TIMEOUT_S[1])
        self.assertEqual(launcher.check_timeout_s({"budget": {"timeout_s": 99999}}), launcher.CHECK_TIMEOUT_S[2])
        attempt = {"kind": "check", "brief": {"objective": "x", "budget": {"timeout_s": 100}, "commands": ["a", "b", "c"]}}
        self.assertGreaterEqual(launcher.attempt_budget_s(attempt), 100 * 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
