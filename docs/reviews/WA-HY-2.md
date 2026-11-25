VERDICT: APPROVE

# Independent review: wave-a-HY-2 (PR #47)

Reviewed at head `c584e0689cd2cfabecf3e3cb3e084fd729b44ae5` (local snapshot
`a91fd56`). The base commit `ee733376f` is not in this sandbox, so, per the
supplied facts, the change map was judged against `launcher.py` and
`tests/test_retry_resume_and_teardown.py` as they stand at head, not against a
base-relative diff. The approved RED test file is unchanged: its sha256 is
`712962125c85499b3af8385a5614d41dcb5d79eab87431c727a09dcc72110bbe`, matching
source_rev, and it contains no skip/xfail. `git status --short` was clean
before this review.

## AC mapping

- **Same-model retry resumes instead of restarting.** `_backoff` computes
  `resumable` from a recorded `run_id`, a recorded `sandbox` and a callable
  `backend.resume` (`launcher.py:2829-2842`); on that path it keeps both fields
  plus `cursor` and sets `resume_pending`, and does not clean. The controller
  pops the flag and calls `backend.resume(a, RESUME_PROMPT)` before the
  fresh-start block, applies returned ids and lands `running`
  (`launcher.py:2481-2494`). The prompt is 146 characters, says "Continue",
  and is never the rendered brief (`launcher.py:45-46`). Covered by
  `ResumeAfterRateLimit::test_same_model_rate_limit_resumes_the_same_sandbox`
  (`starts == 1`, one resume with `sandbox == sb-1`, prompt < 500 chars,
  `cleaned == ["sb-1"]`).
- **A backend without resume still restarts from a fresh sandbox.**
  `launcher.py:2843-2847` cleans once, drops `run_id`/`sandbox` and resets
  `cursor`; the fresh-start block discards the deterministic name before
  `backend.start` so the new sandbox is cleanable (`launcher.py:2514-2520`).
  Covered by `test_backend_without_resume_still_restarts`.
- **Cleanup is keyed by sandbox, exactly once.** The ledger is
  `_cleaned_sandboxes` (`launcher.py:1981`); `_cleanup_once` checks and records
  the sandbox name first and keeps the attempt-id ledger only as the fallback
  when no sandbox is recorded (`launcher.py:2223-2246`). Covered by
  `test_cleanup_once_is_keyed_by_sandbox` (two calls per name, one per name)
  and `test_both_sandboxes_are_torn_down_once_after_a_restart_retry`
  (`sorted(cleaned) == ["sb-1", "sb-2"]`).
- **Cancel while parked does not leak the kept sandbox.** `cancel` records a
  kept sandbox for a queued/backoff attempt and schedules one `_cleanup_once`
  after the terminal write (`launcher.py:2141-2156`; durability caveat in
  finding 4).
- **Fail-closed on a failed resume.** A `backend.resume` exception cleans the
  old sandbox once, drops `run_id`, `sandbox`, `sandbox_id`, `workdir`,
  `local_base_rev`, `seed_commit`, resets `cursor`, and falls through to the
  normal key check / fresh start (`launcher.py:2488-2513`). No gate or key
  check is skipped.
- Approved tests pass and the affected existing tests stay green: see
  commands/results below.

## Findings

1. **[minor] The resume path is dormant for every shipped backend.**
   `launcher.py:2829-2830` requires a callable `backend.resume`, but
   `CuaBackend` (`launcher.py:1308`) and `AgyCuaRunner` (`launcher.py:1703`)
   define none, and no other production class does; the only implementation is
   the approved fake (`tests/test_retry_resume_and_teardown.py:44`). In
   production `resumable` is therefore always False and a same-model 429 still
   takes the clean-and-restart branch (`launcher.py:2843-2847`) with the full
   brief. This matches the brief's "when the backend supports it" and passes
   the approved AC, so it is not a blocker, but fix #3 is not user-visible yet.
   Concrete fix/follow-up: add `resume` to `CuaBackend` (rerun in the same
   sandbox with the continue prompt and return `{"sandbox", "run_id"}`, or
   declare the run/cursor contract in a comment) or carry the gap explicitly
   in the wave plan.

2. **[minor] A resume that changes `run_id` keeps the old cursor.**
   `launcher.py:2486` merges whatever ids the backend returns, so `run_id` may
   change, while `a["cursor"]` is the value `_backoff` preserved
   (`launcher.py:2839-2842`). If a resume starts a new agent run whose event
   cursor restarts below the preserved value, the poll loop calls
   `events(a, old_cursor)` and skips the new run's earliest events, possibly
   its terminal `turn_ended`, leaving the attempt polling or mis-landing. The
   approved fake appends to the same run, so it cannot catch this. Concrete
   fix: capture the previous `run_id` before the call and set `a["cursor"] = 0`
   when `ids.get("run_id")` differs (or reject a changed run id from `resume`
   as a contract violation and take the existing fresh-start fallback).

3. **[minor] The continue prompt asserts a cause the parked path does not
   always have.** `launcher.py:45-46` says the prior turn was interrupted by a
   provider rate limit, but `_backoff` is also used for the Zoo admission 402
   (`launcher.py:2555`), which can park resumably with the same prompt. The
   turn still works; only the diagnosis is wrong. Concrete fix: make the
   second sentence reason-neutral ("The prior turn was interrupted before it
   could finish.") or pass short reason-specific prompts from `_backoff`.

4. **[minor] Cancel-while-parked teardown is scheduled after the terminal
   write, so a crash or a closed loop still leaks the sandbox.**
   `launcher.py:2147-2155` persists `cancelled` first and only then schedules
   `_cleanup_once` on the loop without awaiting it; on a closed loop the
   coordinator's narrowed `except RuntimeError: pass` (behaviour-preserving:
   `run_coroutine_threadsafe` raises RuntimeError only for a closed loop, and
   a non-coroutine TypeError cannot occur here) makes the cleanup a silent
   no-op, exactly as the comment says. If the process restarts in that window,
   memory and journal are both terminal, and `_reap_stale_and_expired` only
   cleans the terminal/disk-active split-brain case (`launcher.py:2340-2346`),
   while `_cleaned_sandboxes` is in-memory only. This is the same
   terminal-write-then-clean ordering as `_finish`, so it is not a
   regression, but it is the one remaining leak path for cancel/restart.
   Concrete fix: record a durable cleanup-pending marker on the terminal
   attempt (keep `sandbox` plus `cleanup_pending: true`) and have `reconcile`
   `_sync_cleanup` it, or await the cleanup when the loop is running.

5. **[minor] A failed resume followed by a failed start can call cleanup twice
   for the same name.** The resume failure cleans the old sandbox and pops it
   (`launcher.py:2491-2493`); the fresh-start block then discards that
   deterministic name (`launcher.py:2516-2518`); if `backend.start` then
   raises before creating anything, the controller-exception handler
   re-synthesizes `a["sandbox"] = sandbox_name(a)` (`launcher.py:2572, 2574`)
   and the `finally` issues a second `backend.cleanup` for the same name. The
   shipped `CuaBackend.cleanup` deletes by name and swallows the error, so it
   deletes nothing, but a backend that counts or has side effects would see a
   duplicate. Concrete fix: stop re-synthesizing `a["sandbox"]` on a start
   failure when the name is already in `_cleaned_sandboxes`, or key cleanup on
   a created-instance generation rather than on the name alone.

No blocker or major findings.

## Review focus

- **Exactly-once teardown per path.** (a) Resumable retry: the parked sandbox
  is kept and the controller's `finally` skips cleanup while `resume_pending`
  is set and the attempt is non-terminal (`launcher.py:2581-2590`); the single
  cleanup happens at `_finish` (`launcher.py:2852-2859`) and the test proves
  one delete. (b) Non-resumable retry: cleanup happens once before the sandbox
  is popped (`launcher.py:2843-2847`), the abandoned controller's `finally`
  no-ops through the attempt-id fallback, and the reused deterministic name is
  discarded before the next create, so the second sandbox is cleaned once
  (`launcher.py:2514-2520`; test asserts `sb-1` and `sb-2`). (c) Model
  switch/free pool: `_fallback_unavailable` (`launcher.py:2770-2792`),
  `_fallback_to_deepseek` (`launcher.py:2794-2803`), the pool branch
  (`launcher.py:2806-2816`) and the exhausted branch
  (`launcher.py:2819-2828`) all clean once before dropping `run_id`/`sandbox`
  and re-queueing; existing fallback tests stay green. (d) Cancel while
  parked: one scheduled cleanup; finding 4 covers its durability. (e)
  Restart/reconcile: a parked retry is not in `_active()`, so it is not
  reattached; it stays in `_queue` and resumes after `retry_at`
  (`launcher.py:2438-2460`), a live run reattaches without a spurious resume
  because the flag is absent, and the only unhandled window is finding 4.
- **Deterministic-name discard.** At `launcher.py:2514-2520` the discard runs
  only on the fresh-start path, and every path that reaches it has already
  cleaned and popped the previous sandbox, so it cannot make a live entry look
  cleaned. A genuine new sandbox is always eligible: the reused deterministic
  name is discarded before `start` and the returned name again after it. The
  only way to erase another live attempt's entry is two attempts whose
  normalized last-10 attempt-id suffix and model/slug coincide, which already
  collide in `sandbox_name` (`launcher.py:218-223`) and is a pre-existing
  uniqueness assumption of the naming scheme.
- **Resume correctness.** `run_id`, `sandbox` and `cursor` are preserved; the
  prompt is short and does not resend the brief; a resume failure reaches a
  fully reset fresh start with the normal key/gate checks. Findings 1 and 2
  are the contract limits to record.
- **`finally` guard.** `resuming` is true only for a `resume_pending` attempt
  that is not terminal, and `_backoff` sets the flag immediately before
  `_set(a, "backoff", ...)` with no intervening await, so the only state the
  guard can cover is backoff. Every terminal transition either cleans before
  landing or lands and then cleans, and the sandbox ledger dedupes the delete.
  The budget watchdog's ownership path is unchanged (`budget_owned` still
  short-circuits cleanup, `launcher.py:2583-2585`).
- **Fail-closed/gates/scope/hygiene.** No auth, lease, scope, security-profile
  or sandbox-isolation code is touched, and no test is modified, skipped or
  weakened. The change is `launcher.py` plus the approved test file only; no
  dependency, network call or new endpoint is added. Changed lines add no
  secrets, hostnames, IPs or absolute paths. Comments and the `_cleanup_once`
  docstring match the code; the README's 429/Retry-After and fallback
  statements remain true and no doc contradicts keeping a sandbox across a
  backoff. The sandbox ledger is in-memory and grows by one short string per
  torn-down sandbox for the launcher's lifetime, comparable to the existing
  `_cleaned_aids`.

## Test commands and exit codes

`pytest` is not installed in this sandbox (supplied fact), so the stdlib
equivalents specified by the coordinator were run:

| Command | Result | Exit |
| --- | --- | --- |
| `timeout 900 python3 tests/test_retry_resume_and_teardown.py` | Ran 4 tests in 1.452s, OK | 0 |
| `timeout 1800 python3 -m unittest discover -s tests -p 'test*.py'` | Ran 564 tests in 109.561s, OK (skipped=4) | 0 |

The full suite reports zero failures and zero errors, so every affected
existing test (including `test_acceptance_class.py`, `test_agy_cua.py`,
`test_assigned_scope.py`, `test_bee_landed_usage.py`,
`test_budget_enforcement.py`, `test_bundlebees_api.py`,
`test_config_routing.py`, `test_deepseek_models.py`,
`test_deployment_receipt.py`, `test_dropout_fixes.py`,
`test_export_and_scripts.py`, `test_export_project.py`, `test_health_models.py`
and `test_install_artifacts.py`) passes. This sandbox observed 4 skips where
the coordinator observed 3; the extra skip is environment-conditional (no
test failed or errored), not a regression from this change.
