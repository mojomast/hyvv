# Independent review — hyvv PR #18: launcher budget enforcement

Reviewer: independent (Hermes review child)
Worktree: ~/rizzstack/hyvv-review-b18
Head: 04cfbea96bdf40f00301275459c0463b333471e0 ("bundlebees: apply verified attempt att_7813289de8fb4151")
Base: e48328fed1582fc39720dfd7bf8cbfe07bcaf8ae (main)
Method: full diff read, line-by-line trace of the dispatch/control path, RED/GREEN on the actual
tree, the unchanged agy suite, the full unit suite, and six read-only probes run from
~/.hermes/cache/scratch against the worktree's `launcher` module. No tracked file was modified;
`git diff --quiet HEAD` is clean and `launcher.py` hashes to
f9a02a8baf4c68514333464250a4f7ad28c0c23cff1787d989e9c56822ad0c9f after the RED runs.

---

## 1. Diff discipline — PASS

`git diff e48328fe..HEAD --stat`:

    launcher.py                      |  92 +++++++++++++++++++-
    tests/test_budget_enforcement.py | 180 +++++++++++++++++++++++++++++++++++++++
    2 files changed, 270 insertions(+), 2 deletions(-)

`--name-status` is exactly `M launcher.py`, `A tests/test_budget_enforcement.py`; `--numstat` is
`90 2 launcher.py`, `180 0 tests/test_budget_enforcement.py`. No schema, config.example, docs,
workflow, or version changes. `brief.schema.json` is untouched (`budget.timeout_s` remains
`"type": "integer"`, brief.schema.json:57-59). The added constants are module-level in
launcher.py (BUDGET_STOP_GRACE_S, launcher.py:133; CHECK_ATTEMPT_OVERHEAD_S, launcher.py:134).
One commit on top of main (`git log e48328fe..HEAD` → 04cfbea only).

---

## 2. RED reproduction (acceptance gate) — PASS, and it discriminates

Reverted only launcher.py, ran the new file, restored:

    $ git checkout e48328fe -- launcher.py
    $ /var/tmp/bundlebees-py/bin/python -m pytest tests/test_budget_enforcement.py -q
    FAILED ...::AgyChildIgnoresDeadline::test_child_reaped_and_attempt_terminal_at_budget
    FAILED ...::OpencodeWorkerOutlivesBudget::test_resumed_attempt_keeps_its_original_budget_clock
    FAILED ...::OpencodeWorkerOutlivesBudget::test_worker_stopped_slot_freed_and_queued_attempt_starts
    FAILED ...::BudgetResolution::test_agy_clamp_preserved
    FAILED ...::BudgetResolution::test_check_keeps_per_command_clamp
    FAILED ...::BudgetResolution::test_opencode_enforces_only_a_configured_budget
    6 failed, 1 passed in 40.25s          (pytest exit 1)

Failure text: `AssertionError: 'running' != 'failed'` at test:75 (agy case), test:116 (opencode
runaway) and test:138 (resumed attempt), plus three `AttributeError: module 'launcher' has no
attribute 'attempt_budget_s' / 'check_timeout_s'`. The unittest form reproduces the PR body's
evidence exactly: `Ran 7 tests in 40.122s` / `FAILED (failures=3, errors=3)`.

Restored with `git checkout HEAD -- launcher.py`; `git status --porcelain` shows only the
untracked review brief and `git diff --stat HEAD` is empty. On the restored head:

    $ python -m pytest tests/test_budget_enforcement.py -q
    .......  [100%]     7 passed in 4.18s

So the new tests fail on the pre-fix launcher and pass on the fix. No blocking finding here.

---

## 3. Enforcement semantics — the mechanism is present and works on its main paths

### (a) Per-kind resolution preserves the existing clamps

`attempt_budget_s` (launcher.py:1119-1140):

- agy-cua → `agy_timeout(b, mode)` (launcher.py:1131-1132). `agy_timeout` keeps the old
  `budget.timeout_s or (1800 if code else AGY_TIMEOUT_S[1])` default and the
  `max(60, min(3600, t))` clamp (launcher.py:1115-1117; clamp tuple at launcher.py:128). Code-mode
  default 1800 survives the clamp; the test pins all four cases (test:157-162).
- check → `check_timeout_s(b) * (len(commands)+1) + CHECK_ATTEMPT_OVERHEAD_S` (launcher.py:1133-1134).
  `check_timeout_s` is the exact extraction of the old inline expression, clamp
  `max(10, min(7200, t))` with default 1800 (launcher.py:702-705; tuple at launcher.py:700), and
  `DockerCheckRunner.run_checks` now calls it (launcher.py:740). Behaviour of the per-command
  clock is unchanged.
- opencode → the raw int only when it parses and is > 0, else None (launcher.py:1135-1140).
  `attempt_budget_s` for an opencode brief without a budget returns None, so no clamp and no
  watchdog is invented for unbriefed attempts. Tests pin None/0/"soon"/1200 (test:164-169).
  Base had no opencode use of `budget.timeout_s` at all (`git show e48328fe:launcher.py` grep
  timeout_s → only the agy path at 1105 and the check path at 729), so this is new enforcement,
  not a rewrite.

Minor: a malformed string ("soon") or 0/negative value silently means "unbounded" for opencode
(launcher.py:1138-1140). The schema says integer but has no minimum, so 0 is a legal brief value
that disables the wall clock. Worth a schema `minimum: 1` or a submit-time rejection eventually.

### (b) Anchor and persistence

`_control` computes the budget and anchors it before any start work (launcher.py:1659-1672). For a
resumed attempt (`run_id` present) it reuses `a["budget_started_at"]`; otherwise it stamps now and
saves the whole attempt dict (launcher.py:1668-1670; `_save` writes the entire dict,
launcher.py:1464-1466; `_load` restores every key, launcher.py:1470-1475). The deadline is
`started + budget` and drives both the fast path and the watchdog (launcher.py:1671-1672).

Verified end-to-end through the real journal/`_load`/`_schedule` reattach, not just `_control`:
an attempt saved as running with a 30 s budget and `budget_started_at` 40 s old came back after a
fresh `Launcher(...)` + `_schedule()` as
`state='failed'`, `elapsed=0.05s`, detail `budget timeout: worker outlived
brief.budget.timeout_s=30s (launcher stopped the run after 40s)`, with the stored offset still
40.1 s. So the persisted-clock claim holds for the run_id reattach path.

Caveats: the clock is reused only when `run_id` exists (launcher.py:1668), and every retry path
pops `run_id` (launcher.py:1816, 1829, 1839, 1850). A re-dispatch therefore restarts the clock —
see finding F4. And a restart inside the teardown window wedges the attempt — see F2.

### (c) Fast path

The controller loop checks the deadline first and force-finishes via a shielded
`_budget_finish` (launcher.py:1688-1689). A healthy loop therefore stops at the deadline without
relying on the watchdog. The ordering cost is finding F1 (the check runs before the event drain at
launcher.py:1693).

### (d) Watchdog, budget_stop, terminal write, slot

- Watchdog: one-shot `asyncio.sleep(deadline-now)` then `asyncio.shield(_budget_finish(..., controller))`
  (launcher.py:1732-1739). It is created at launcher.py:1672 and cancelled in the controller's
  finally (launcher.py:1729). The double shield (watchdog → shield, and the controller's fast path
  → shield, launcher.py:1689) is what lets the teardown survive the controller's own
  `watchdog.cancel()`.
- Idempotence and no resurrection: `_budget_finish` sets `budget_stop` under the lock before doing
  anything else and returns if the attempt is already terminal, already stopping, or queued
  (launcher.py:1750-1753). `_active()` excludes `budget_stop` attempts (launcher.py:1636-1637), so
  `_schedule` neither resumes a controller nor counts the slot (launcher.py:1644-1649).
- Stop: cancels the controller and waits BUDGET_STOP_GRACE_S for it (launcher.py:1754-1757), then
  `backend.interrupt` with a 15 s timeout (launcher.py:1758-1759).
- Terminal write before cleanup: `_set(a, "failed", "budget timeout: worker outlived
  brief.budget.timeout_s=<n>s (launcher stopped the run after <t>s)")` at launcher.py:1761-1764,
  then cleanup at launcher.py:1765-1766, then `kick()` at launcher.py:1767. `_set` appends the
  event and saves the journal before the landed-event/notifier work (launcher.py:1622-1634), so
  the journal shows terminal even if the local notifier blocks.
- Slot: the in-process slot frees at `budget_stop` (launcher.py:1636-1637), i.e. before the
  worker is confirmed dead; the admin journal gate frees at the terminal write. See F6.

Empirically verified with a backend whose `events()` never returns (stuck controller):
`state='failed'` at 2.01 s for a 2 s budget with the honest detail, `_active()==[]`; with a
controller that swallows the first cancellation, terminal still lands at 17.01 s (2 s + the 15 s
controller wait) with "stopped the run after 17s". The terminal write is not skipped by the
watchdog and the shield works.

### (e) Child-level backstop preserved

`agy_argv` still passes `--print-timeout` (launcher.py:1113) and `AgyCuaRunner.events` still kills
the child at `timeout + 60` (launcher.py:1280-1281); the check runner still bounds each command
with `t` (launcher.py:746). Nothing in the diff removes or weakens either. (In practice the
launcher now stops at `timeout`, so the +60 kill is a true backstop for a blocked event loop.)

### Where the enforcement can still miss or misfire

**F1 (correctness, non-blocking) — deadline check runs before the event drain.**
launcher.py:1688 compares the clock before launcher.py:1693 polls events. A worker that finished
inside the budget but whose `turn_ended` lands after the last poll is force-failed with the false
detail "worker outlived ...". Reproduced deterministically: a fake run whose `turn_ended` appears
at 1.5 s under a 2 s budget →

    state='failed' elapsed=2.01s detail='budget timeout: worker outlived
    brief.budget.timeout_s=2s (launcher stopped the run after 2s)' result_file=False

The false-failure window is up to one poll interval (1 s, launcher.py:1720) plus backend event
latency, and it is the whole window on a restart with an already-expired deadline (a run that
actually completed while the launcher was down is failed without collecting its result). Fix:
drain once before declaring the deadline, or on deadline take one non-blocking `events()` poll and
finish if a completion event is present.

**F2 (correctness, non-blocking) — a restart inside the teardown window wedges the attempt
forever.** `budget_stop` is persisted at launcher.py:1753; the terminal write is at
launcher.py:1761-1764. If the process dies between them, on restart `_load` restores `running` +
`budget_stop`, `_active()` excludes it (launcher.py:1636-1637), so `_schedule` neither resumes nor
fails it (launcher.py:1644-1648), and the guard at launcher.py:1751 makes any future
`_budget_finish` return early. Probe after a simulated restart:

    state='running' detail='opencode run started' controllers=[] _active()=[]
    active_attempts_for('demo')=1

That is a permanent non-terminal attempt that blocks the journal-based apply-update gate (the
host admin tool refuses while the journal shows active/queued attempts; see the launcher-admin
skill's apply-update contract — that gate is outside this repo) — the
exact symptom this PR removes, in a narrower window. Fix: a startup sweep that re-runs the
teardown (with a `force` that bypasses the `budget_stop` guard) for any non-terminal `budget_stop`
attempt with no controller.

**F3 (claim vs code, non-blocking) — interrupt/cleanup failures are swallowed with no record.**
launcher.py:1758-1759 and 1765-1766 catch and discard every failure. For agy `interrupt` is a local
`p.kill()` (launcher.py:1297-1299), reliable; for opencode it is an SDK `interrupt()`+`stop()`
(launcher.py:1067-1071) and a sandbox delete (launcher.py:1088-1094), both fallible. If both fail,
the attempt is journalled failed and the slot is free while the opencode run/sandbox can keep
running, with no evidence in the attempt. Nothing retries. At minimum record
`budget_cleanup_error` on the attempt and schedule a retry.

**F4 (scope, non-blocking) — the wall clock is per-dispatch, not per-attempt.**
Re-dispatch after rate-limit backoff / pool switch / DeepSeek fallback drops `run_id`
(launcher.py:1816, 1829, 1839, 1850), and the anchor is only reused when `run_id` exists
(launcher.py:1668). Probe: an attempt with `budget_started_at` 100 s old and a 5 s budget was
re-dispatched with a fresh clock and succeeded. So an opencode attempt can live
N × (run budget + backoff delays) — the incident kind. If "attempt wall clock" is meant literally,
anchor on `created_at` or never clear `budget_started_at`; if per-dispatch is intended, say so in
the PR body and the docstring (launcher.py:1120-1127).

**F5 (correctness, non-blocking) — check-kind derived budget can be shorter than the legitimate
start phase.** `CHECK_ATTEMPT_OVERHEAD_S = 300` (launcher.py:134) is added to
`t*(n+1)` (launcher.py:1134), but `DockerCheckRunner.start` allows 300 s for `docker run`
(launcher.py:722), 300 s for the tar seed (launcher.py:729) and 300 s for the git init/commit
(launcher.py:732) — up to ~900 s of legitimate pre-command work. A check brief with a small
`timeout_s` (e.g. 10 s, one command → 320 s attempt clock) can be failed by the watchdog during
seeding. Also `_budget_finish` calls the generic `backend.interrupt/cleanup` for check attempts,
which routes to the Cua SDK (`CuaBackend.interrupt`, launcher.py:1067-1071; `cleanup`,
launcher.py:1088-1094) rather than the docker path, wasting up to 45 s after the terminal write;
the container is actually removed by `_control_check`'s finally (launcher.py:1796-1797). Use the
check-specific cleanup and size the overhead to the start-stage timeouts.

**F6 (ordering, non-blocking) — the in-process slot frees before the worker is dead.**
`budget_stop` removes the attempt from `_active()` (launcher.py:1636-1637), which feeds both the
free-slot count (launcher.py:1649) and `/v1/health.active` (launcher.py:2135). A new attempt can
therefore start (and health can report 0 active) while the old worker is still being stopped, for
up to the 15 s controller wait + interrupt. The journal gate is unaffected until the terminal
write, but two workers on one host briefly overlap. Consider keeping the attempt in the slot count
until `interrupt` returns.

**F7 (honesty of the detail) — the detail always names `brief.budget.timeout_s`, even when the
brief carried none.** For agy it can be the default 600/1800 (launcher.py:1129-1132); for check
it prints the derived clock (e.g. 7500) that no brief contains (launcher.py:1134). The number is
real but the label is not. `attempt_budget_s` should return (value, source) or the detail should
say "launcher budget (agy default)" / "derived check clock".

**F8 (robustness, minor) — wall clock for in-process comparisons.** The anchor must be wall clock
to persist, but the comparisons at launcher.py:1688, 1738 and 1760 use `time.time()`; a clock step
changes the effective budget. A monotonic deadline for the running process, re-derived on load,
would be immune.

---

## 4. No regression — PASS

- `tests/test_agy_cua.py` is byte-identical to base (`git diff e48328fe..HEAD --quiet` → clean)
  and passes: `23 passed in 0.31s`.
- Full suite: `Ran 343 tests in 60.294s` / `OK (skipped=3)` — matches the PR body's "343 tests
  green" (the 3 skips are environment-conditional; `cua` is not installed in this review venv,
  tests/test_sdk_contract.py:42).

---

## 5. Test quality

The six failing tests do discriminate the fix (section 2), and the three BudgetResolution tests
pin return values rather than internals — good. The gaps:

**T1 (important) — the watchdog path is not tested at all.** Every runtime test keeps a healthy
controller loop, so the fast path at launcher.py:1688 handles the deadline; the watchdog
(launcher.py:1672, 1732-1739) never has to take over. Mutation check in a scratch copy: replacing
the watchdog creation with `watchdog = None` leaves all 7 tests green (`7 passed in 4.17s`). The
incident's stated failure mode is exactly "controller/worker never returns", so the one mechanism
that fixes it is unproven by the suite. Add a backend whose `events()` never returns (or raises
`CancelledError` once) and assert terminal `failed` at budget + grace with the slot free. (The
mechanism does work — verified by my probes — but nothing in the suite protects it.)

**T2 — `test_healthy_run_within_budget_is_not_stopped` (test:142-152) is the one test that passes
on the unmodified tree** (verified: `1 passed in 0.09s` on base). That is legitimate as an
anti-false-positive guard, not a vacuous enforcement test. It is weak, though: the fake run ends
in ~0.05 s against a 60 s budget, so it cannot catch an early kill or the boundary race in F1. A
run that completes at, say, budget − 1 s would be a real guard.

**T3 — the resume test does not exercise persistence.** `test_resumed_attempt_keeps_its_original_budget_clock`
(test:127-140) hand-builds the attempt dict with `budget_started_at` and calls `_control` directly;
it never goes through `_save`/`_load`/`_schedule`. My probe covers the real reattach path; the
suite does not pin it, and no test covers the retry-clock reset (F4).

**T4 — no tests for:** `budget_stop` preventing `_schedule` resurrection, terminal-before-cleanup
ordering, `_budget_finish` idempotence under the fast-path/watchdog race, interrupt/cleanup
failure, the start-phase budget (a slow `start` that consumes the budget), or check-kind teardown.
These are cheap unit tests against the FakeBackend.

No test is vacuous with respect to the fix as a whole (all six RED tests fail without it).

---

## 6. Security and hygiene — PASS

- Added lines contain no credentials, tokens, hostnames, or secret-shaped literals. The only
  IP is the test's loopback bind `"127.0.0.1"` (test:26). The only key-shaped string is
  `ORBIT_WORKER_GO_KEY: "fixture-value-not-a-real-key"` (test:97, 130, 145) — clearly a fixture
  placeholder, not a secret. The model name `gemini-4-argon` (test:65) is a public tier name.
- The diff adds no imports and no network calls (`requests`, `urllib`, sockets are untouched);
  the only I/O added is journal/test file writes.
- No secrets are logged: the detail strings contain only counts/durations.

---

## 7. Honesty — the evidence claims check out; the incident diagnosis does not

Verified accurate:

- RED record `Ran 7 tests ... FAILED (failures=3, errors=3)` — reproduced exactly (40.122 s vs
  the claimed 40.20 s).
- `6 failed, 1 passed in 40.20s` — reproduced as 40.25 s.
- `junit 30 tests / 0 failures / 0 errors / 0 skipped` for the two files — 7 + 23 = 30, all pass.
- `343 tests` full suite — reproduced (`OK (skipped=3)`).
- `+270/−2 in exactly the two leased files` — reproduced.
- The mechanism description (anchor, fast path, shielded watchdog, budget_stop, terminal before
  cleanup, child backstop preserved) matches the code line for line.

Not accurate / overclaimed:

**H1 (material) — the incident is misdescribed.** The campaign record for
att_641b8119a39a426d says it was an **opencode** attempt (`state running ("opencode run started")`,
budget 40/1200) that stayed alive past the 1200 s wall from ~20:21 and **landed `succeeded` at
21:06 EST** (see `~/.hermes/plugin-data/bundlebees/campaigns/focused-improvement-2026-11-15.md`
§21-§24, lines 615, 623, 631, 640). On base, `budget.timeout_s` reached *nothing* for opencode —
the only consumers were agy (`--print-timeout`) and check (per-command timeout). The PR body's
"reached only the child process flag (e.g. the agy runner's `--print-timeout`)" and the test class
docstring "The incident shape: agy-cua, child hangs past its own --print-timeout" (test:60) model
a different kind and a different mechanism. The core diagnosis ("nothing watched the attempt as a
whole") is right; the incident attribution is not.

This also means the fix converts the incident's actual outcome (a slow but healthy opencode run
that finished at ~45 min) into `failed` at 20 min. That is the requested policy, but it should be
stated: the change is not only "stuck workers are reaped", it also fails slow-but-progressing
attempts at the budget.

**H2 — "the worker is interrupted within BUDGET_STOP_GRACE_S=15"** is an interrupt *attempt* with
a 15 s timeout whose failure is swallowed (launcher.py:1758-1759), not a verified stop; and the
terminal write can be up to ~2× the grace late (controller wait then interrupt wait — probe
measured 17.01 s for a controller that ignored one cancellation). Say "attempted within 15 s, then
abandoned".

**H3 — "persists budget_started_at across restarts"** holds for the run_id reattach path
(verified end-to-end) but not for a restart inside the teardown window (F2), where the attempt is
never recovered. The restart-safety claim is incomplete.

**H4 — "honest detail"** is false when the run actually finished inside the budget (F1: it says
"worker outlived") and misleading when the value is an agy default or a derived check clock (F7).

**H5 (cosmetic)** — the fix paragraph says `+268/−2`; the verified diff (and the PR's own
evidence paragraph) says `+270/−2`.

---

## Findings summary

| ID | Severity | Finding | Evidence |
|----|----------|---------|----------|
| F1 | followup (fix soon) | Deadline check precedes event drain; a run that finished inside the budget can be failed with a false "outlived" detail and no result | launcher.py:1688 vs 1693; probe: 1.5 s run / 2 s budget → failed at 2.01 s, result_file=False |
| F2 | followup (fix soon) | Crash/restart between `budget_stop` and the terminal write wedges the attempt forever (no controller, journal non-terminal, blocks apply-update) | launcher.py:1751-1753, 1636-1637, 1644-1648; probe: state running, controllers [], active_attempts_for=1 |
| F3 | followup | Interrupt/cleanup failures swallowed with no record; worker can outlive the budget while journalled failed | launcher.py:1758-1759, 1765-1766 |
| F4 | followup | Wall clock resets on every retry/fallback re-dispatch (run_id popped) | launcher.py:1668, 1816, 1829, 1839, 1850; probe: 100 s-old anchor ignored |
| F5 | followup | Check derived budget (max 300 s overhead) can be below the legitimate ~900 s start phase; generic cleanup used for check | launcher.py:134, 1134, 722/729/732, 1067-1071, 1088-1094 |
| F6 | followup | In-process slot/health frees at `budget_stop`, before the worker is dead | launcher.py:1636-1637, 1649, 2135 |
| F7 | followup | Detail names `brief.budget.timeout_s` even when it is an agy default or derived check clock | launcher.py:1129-1132, 1134, 1763 |
| F8 | minor | `time.time()` for in-process deadline comparisons | launcher.py:1688, 1738, 1760 |
| T1 | followup (important) | Watchdog path untested; deleting the watchdog leaves all 7 tests green | mutation in scratch: 7 passed |
| T2 | minor | Healthy-run test finishes in 0.05 s of a 60 s budget; cannot detect early kills | test:142-152; passes on base |
| T3 | minor | Resume test hand-builds the dict and calls `_control`; no `_save`/`_load`/`_schedule` coverage | test:127-140 |
| T4 | minor | No tests for budget_stop/reschedule, ordering, idempotence, cleanup failure, slow start, check teardown | — |
| H1 | followup (docs) | Incident misattributed to agy; it was opencode and eventually succeeded | campaign record §21-§24; base grep |
| H2-H5 | docs | Interrupt "within 15 s", restart persistence, "honest detail", +268 vs +270 | launcher.py:1758-1759, F2, F1/F7 |

BLOCKING findings: 0. The acceptance gate (RED on base, GREEN on head) is real; the incident kind
(opencode with a budget) is stopped at the deadline and its slot frees. The followups are
crash-consistency, boundary correctness, retry semantics, and test coverage — none of them
invalidates the primary path, but F1/F2 should land before this launcher version is treated as
restart-safe.

---

## Evidence log (raw)

Environment: Python 3.12.3 (`/var/tmp/bundlebees-py/bin/python`, pytest 8.4.2). `cua` is not
installed; both the new test and the probes mock it exactly as the suite does.

    git rev-parse HEAD                          -> 04cfbea96bdf40f00301275459c0463b333471e0
    git diff e48328fe..HEAD --stat              -> 2 files, 270 insertions(+), 2 deletions(-)
    RED  (launcher.py at e48328fe, tests at head) -> 6 failed, 1 passed in 40.25s
    RED  unittest form                           -> Ran 7 tests, FAILED (failures=3, errors=3)
    GREEN (head)                                 -> 7 passed in 4.18s
    test_agy_cua.py                              -> 23 passed in 0.31s (file identical to base)
    unittest discover -s tests -q                -> Ran 343 tests, OK (skipped=3), 60.294s

Probes (scratch, read-only against the worktree):

    watchdog, stuck controller, cancellable       -> failed at 2.01s, detail "budget timeout ...=2s
                                                     (stopped after 2s)", _active()==[]
    watchdog, controller ignores first cancel     -> failed at 17.01s, detail "... (stopped after 17s)"
    restart inside teardown (budget_stop, no term)-> state running, controllers [], _active()==[],
                                                     active_attempts_for('demo')==1
    restart reattach (run_id + 40s-old anchor)    -> failed at 0.05s, "stopped after 40s", anchor kept
    retry re-dispatch (no run_id, 100s anchor)    -> succeeded in 0.05s, clock reset (anchor ignored)
    boundary race (turn_ended at 1.5s, budget 2s) -> failed at 2.01s, "worker outlived ...=2s",
                                                     result_file=False
    watchdog creation disabled (mutation)         -> 7 passed in 4.17s (watchdog untested)

VERDICT: approve_with_followups, BLOCKING: 0
