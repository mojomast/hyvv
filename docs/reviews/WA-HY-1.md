VERDICT: APPROVE

# Independent review: wave-a-HY-1 (PR #45)

Reviewed at head `29ae227a4f21b4e269a19d5f664dab559465816c` (local snapshot
`4d4dc9d`; the base commit `fdd2354` is not present in this sandbox). Per the
supplied facts the bee patch is `launcher.py` only (+35/-12) and the approved
RED tests were added before the bee ran; their contents match the described RED
contracts and contain no skip/xfail. `git status` was clean before this review.

## AC mapping

- `DEFAULT_ATTEMPT_TIMEOUT_S`: `launcher.py:144` = 3600, inside the required
  60..86400 range. Covered by `test_constant_is_a_sane_ceiling`.
- Config `default_timeout_s`, defaulted and clamped 60..86400:
  `launcher.py:1017-1021` int-coerces with `TypeError/ValueError/OverflowError`
  fallback, then `max(60, min(86400, value))`, and stores the clamped value.
  Covered by `test_load_config_sets_the_default` and
  `test_load_config_clamps_the_default` (1 -> 60, 10**9 -> 86400, "soon" ->
  constant).
- `attempt_budget_s` falls back so no attempt runs unbounded:
  `launcher.py:1556-1584`. The opencode branch resolves `default_timeout_s`
  from `cfg`, falls back to the constant on bad/missing/non-positive default,
  reads `budget.timeout_s` only for a dict budget, and returns the default for
  any value that is not a positive int. The agy/check branches are unchanged and
  keep `AGY_TIMEOUT_S` / `CHECK_TIMEOUT_S`. All three production call sites pass
  `self.cfg` (`launcher.py:2291`, `2355`, `2499`); no production caller relies
  on the implicit default.
- `validate_brief` refuses a missing budget only when `require_budget` is set:
  `launcher.py:1855-1863`, with `require_budget` defaulting false at
  `launcher.py:1022`. The HTTP submit path validates before `L.submit`
  (`launcher.py:3031-3034`), so a refusal is a 400 and no attempt is created.
- Approved tests pass and affected tests stay green: see commands/results below.

## Findings

1. **[minor] `launcher.py:1580-1584` and `launcher.py:1858-1862` accept a JSON
   boolean as a budget.** `int(True)` is 1, so `{"budget": {"timeout_s": true}}`
   yields a 1-second attempt wall clock instead of the host default, and under
   `require_budget` the same brief is accepted as "positive". This does not
   escape the wall clock (1 s is bounded, so it fails closed) and no approved AC
   covers it, but it contradicts the field's integer-seconds contract.
   Concrete fix: reject `isinstance(raw, bool)` before `int()` in both places
   (return the default in `attempt_budget_s`; return the existing
   `require_budget` error in `validate_brief`).

2. **[minor] `launcher.py:1584` does not cap an explicit opencode budget.**
   `timeout_s: 10**18` is honored, while agy is capped by `AGY_TIMEOUT_S[2]`
   (3600), check is capped per command by `CHECK_TIMEOUT_S[2]` (7200), and the
   host default is capped at 86400. This is pre-existing explicit-budget
   behaviour, is not one of the frozen ACs, and is therefore not a blocker; it
   is, however, the remaining way for a caller to obtain an effectively
   permanent worker-slot lease. If a ceiling is wanted, clamp the positive value
   (e.g. `return min(t, 86400)`) after `t > 0`; `test_explicit_budget_still_wins`
   (1200) and the agy/check tests still pass.

3. **[minor] `launcher.py:1572-1577`: the 60..86400 clamp exists only in
   `load_config`.** A `cfg` dict constructed directly (not through
   `load_config`) is trusted whenever the default is positive; only `<= 0`
   falls back to the constant. That is deliberate and is required by
   `tests/test_default_wall_clock.py:80`, which sets `default_timeout_s = 2`
   below the clamp to drive the runaway clock, so the config loader is the
   single clamp boundary. No code change is needed; adding that invariant to
   the `attempt_budget_s` docstring would stop a future caller from assuming
   the range clamp is re-applied.

No blocker or major findings.

## Review focus

- **Clamp on every read path:** enforced in `load_config`; `attempt_budget_s`
  re-checks only positivity because the approved runaway test intentionally
  drives a sub-clamp in-memory default. HTTP-submitted briefs do not choose
  `default_timeout_s`, so they cannot bypass the config clamp.
- **Crafted briefs:** `budget: null`, a non-dict budget, `timeout_s: 0`, a
  negative value, `null`, a non-numeric string, NaN/Infinity, and over-range
  values all resolve to the host default for opencode; none returns `None` or a
  non-positive wall clock. A huge explicit integer is bounded but effectively
  long (finding 2). A boolean is coerced to 1 s (finding 1). The agy/check
  branches keep their existing clamps and tests.
- **`require_budget` is opt-in:** defaults to false; the truthy check is
  fail-closed for malformed/missing budgets; the 400 is emitted before
  `L.submit`, so no attempt journal or worker slot is created on refusal.
- **No weakened gate/sandbox/check/test:** the change is limited to budget
  resolution and validation; no auth, scope, sandbox, or security-profile code
  is touched, and the approved tests remain read-only and unmodified with no
  skips.
- **Hygiene:** changed lines add no secrets, hostnames, IPs, or absolute paths.
  The new optional config keys are not added to `config.example.json`; the
  repository does not enumerate config keys exhaustively, so no documentation
  contradicts the code, and the bee's assigned-file scope was `launcher.py`.

## Test commands and exit codes

`pytest` is not installed in this sandbox (`python3 -m pytest --version` exits
1: "No module named pytest"), so the stdlib equivalents specified by the
coordinator were run:

| Command | Result | Exit |
| --- | --- | --- |
| `timeout 900 python3 tests/test_budget_enforcement.py` | Ran 16 tests, OK | 0 |
| `timeout 900 python3 tests/test_default_wall_clock.py` | Ran 11 tests, OK | 0 |
| `timeout 1800 python3 -m unittest discover -s tests -p 'test*.py'` | Ran 524 tests, OK (skipped=4) | 0 |

The 27 approved RED tests all pass (16 + 11), and the full suite reports 524
tests with zero failures/errors and 4 skips, matching the coordinator's
expected counts.
