# Review — hyvv PR #19: Fix queue-position TOCTOU in Launcher.public

Reviewer: independent (Hermes, detached worktree)
PR: #19 `fix/queue-public-toctou`
Head under review: `0a50f7844dfc0a8d57ad7d5bf6a4437bca475d08`
Base: `14650e3469f7ba2c1097a2dc2cf9b05951ec7d00` (main)
Worktree: `~/rizzstack/hyvv-review-toctou` (detached at head)
Interpreter for the mandated runs: `/var/tmp/bundlebees-py/bin/python` (3.12.3); plus a local 3.13.17 cross-check.

Everything below is from commands I ran in this worktree, not from the PR body.

---

## 1. Diff scope — PASS

```
$ git diff --name-status 14650e34..0a50f78
M	launcher.py
A	tests/test_queue_public_race.py

$ git diff --stat 14650e34..0a50f78
 launcher.py                     |  2 +-
 tests/test_queue_public_race.py | 65 ++++++++++++++++++++++++++++++++++++++++
 2 files changed, 66 insertions(+), 1 deletion(-)

$ git log --oneline 14650e34..0a50f78
0a50f78 bundlebees: apply verified attempt att_8638698eb8794820
```

Single commit, exactly the two leased files, +66/−1. `git rev-parse HEAD^` is `14650e34…`, so the
commit sits directly on the stated base with no merge or rebase noise. `git ls-remote origin
fix/queue-public-toctou` returns `0a50f78…`, identical to local HEAD.

The change itself (launcher.py:1510):

```python
-        if d["state"] == "queued": d["queue_position"] = self._queue().index(a["attempt_id"]) + 1 if a["attempt_id"] in self._queue() else None
+        if d["state"] == "queued": q = self._queue(); d["queue_position"] = q.index(a["attempt_id"]) + 1 if a["attempt_id"] in q else None  # one snapshot: a second _queue() call could race the .index()
```

Both `in` and `.index()` now read the same list object. `list.__contains__` and `list.index` are
C-level, run with no `await`/callback between them, and `q` is a private copy (the comprehension in
`_queue()` builds a new list), so the id cannot disappear between the two operations.

## 2. New test file on the PR head — PASS (3/3)

```
$ /var/tmp/bundlebees-py/bin/python -m unittest tests.test_queue_public_race -v
test_non_queued_attempt_never_reads_queue ... ok
test_queued_id_present_yields_position_from_one_snapshot ... ok
test_race_id_left_queue_between_membership_check_and_index ... ok
Ran 3 tests in 0.001s
OK
```

Cross-check on the leg that actually failed in CI (arm/3.13; I only have x86_64 3.13 locally):

```
$ .../cpython-3.13-linux-x86_64-gnu/bin/python3.13 -m unittest tests.test_queue_public_race -v
Ran 3 tests in 0.010s
OK
```

The test file is auto-discovered by the CI command (`uv run python -m unittest discover -s tests`,
.github/workflows/ci.yml:70, matrix of ubuntu / ubuntu-arm / macos × 3.12 / 3.13), so it is not
sitting outside the gate.

## 3. RED reproduction — PASS (matches the claimed shape exactly)

```
$ git checkout 14650e34 -- launcher.py && ... -m unittest tests.test_queue_public_race -v

ERROR: test_race_id_left_queue_between_membership_check_and_index
  File ".../launcher.py", line 1510, in public
    if d["state"] == "queued": d["queue_position"] = self._queue().index(a["attempt_id"]) + 1 if a["attempt_id"] in self._queue() else None
                                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ValueError: 'att_cafe00002' is not in list

FAIL: test_queued_id_present_yields_position_from_one_snapshot
  File ".../tests/test_queue_public_race.py", line 53
    self.assertEqual(len(calls), 1)
AssertionError: 2 != 1

Ran 3 tests in 0.004s
FAILED (failures=1, errors=1)
```

Restore:

```
$ git checkout HEAD -- launcher.py
$ git status --porcelain
?? review-hyvv-pr19-brief.txt      # pre-existing untracked note, not mine
$ git diff --stat && git diff --cached --stat
(empty)
```

Worktree clean apart from untracked notes; the GREEN run was repeated after restore (3/3 OK).
The RED error is the same exception, same function, same line as the CI failure, and the
`2 != 1` failure independently pins the double read rather than only the symptom.

## 4. API suite on the head — PASS (15/15)

```
$ /var/tmp/bundlebees-py/bin/python -m unittest tests.test_bundlebees_api -v
...
test_v011_brief_shape_accepted ... ok
...
Ran 15 tests in 9.166s
OK
```

The originally-red test now passes. Also ran the full discovery suite:

```
$ /var/tmp/bundlebees-py/bin/python -m unittest discover -s tests
Ran 346 tests in 60.796s
OK (skipped=3)
```

## 5. Discrimination check — YES, a two-read "fix" is caught (verified, not just reasoned)

I applied three alternative implementations to the real `launcher.py` in this worktree and ran the
new test file against each, restoring from HEAD afterwards (`git status` clean).

| Variant | Shape | Result |
|---|---|---|
| A — wrong, looks fixed | `inq = a["attempt_id"] in self._queue()` then `self._queue().index(...)` — membership hoisted into a variable but still two reads | **FAILED (failures=1, errors=1)**: test 1 `ValueError: 'att_cafe00002' is not in list` at launcher.py:1512, test 2 `AssertionError: 2 != 1` |
| B — correct, different shape | one `self._queue()` call wrapped in `try/except ValueError` | **OK (3/3)** — the tests are not overfitted to the `q` variable |
| C — wrong, crash masked | original double read wrapped in `try/except ValueError` (the tempting "make the 500 go away" non-fix) | **FAILED (failures=2)**: `AssertionError: None != 1` on the race test plus `2 != 1` |

Reasoned answer: yes, and for two independent reasons. (a) The `assertEqual(len(calls), 1)` in
`test_queued_id_present_yields_position_from_one_snapshot` fails **any** implementation that reads
`_queue()` more than once, regardless of interleaving — a single-element snapshot list makes the
value assertion pass, so the call-count assertion is the discriminator. (b) In the race test the
second read returns `[]`, so every ordering fails: membership-then-index raises `ValueError`,
index-then-membership yields `None` and trips `None != 1`. Variant C shows the suite also rejects
the crash-masking variant that would look green in the API suite. This is a genuinely
discriminating regression test, not a tautology.

## 6. Honesty check — claims match my measurements

| PR body claim | My measurement | Verdict |
|---|---|---|
| junit 18 tests, 0 failures, 0 errors, 0 skipped at the verify step (tests = `test_queue_public_race.py` + `test_bundlebees_api.py`) | 3 + 15 = 18, all pass | matches |
| worker sandbox: race file 3/3, API file 15/15 incl. `test_v011_brief_shape_accepted` | reproduced on head | matches |
| full CI discover 346 tests OK, skipped=3 pre-existing | `Ran 346 tests … OK (skipped=3)` | exact match |
| sha `0a50f78…`, diff = exactly the two files | confirmed (section 1) | matches |
| main CI at 14650e34: only real failure `test (ubuntu-24.04-arm, 3.13)` | `gh run view 43861000834`: that leg `failure`; all six other test legs, provisioning and scan `success`; every `bee-ci/*` and `CI` `failure` | matches |
| traceback: `do_POST` launcher.py:2179 → `public` 1510 → `ValueError: 'att_cafe00002' is not in list` | `gh run view 43861000834 --log-failed` shows exactly 2179 → 1510 → that ValueError, `FAILED (errors=1, skipped=3)` | matches |
| #18 did not touch this line; its hunks sit at 126/693/726/1105/1599-1726 | `git show 04cfbea -- launcher.py` hunk pre-image headers are `-126 -693 -726 -1105 -1599 -1621 -1637 -1676`; no `queue_position` / `_queue()` anywhere in that diff | matches (pre-image numbering) |
| PR #18 head 04cfbea was 14/14 green | `gh pr view 18`: 14 checks, 0 non-success; run 43859096807 `success` | matches |
| PR head CI | all 14 contexts `SUCCESS`, including `test (ubuntu-24.04-arm, 3.13)` | green |

Disclosed detour (worker told not to push; coordinator created the branch at the exact base;
one `--commit-local` failure on a missing base object): unverifiable from repository state — no
artifact of a failed attempt survives — but the resulting history is exactly what that story
predicts (one commit whose parent is precisely `14650e34`, branch name as disclosed, no stray
commits). The disclosure is specific and self-incriminating rather than flattering; nothing I can
measure contradicts it. I am recording it as consistent-but-unproven.

## 7. New-defect analysis

**Behaviour change for non-queued states — none.** The fix only rewrites the assignment inside the
existing `if d["state"] == "queued"` guard; `d` is still seeded from `a.get(k)` and the guard is
unchanged. States other than `queued` still skip the queue read entirely (test 3 asserts
`len(calls) == 0` and a `None` position).

**None handling — improved, contract-compliant.** Previously, a queued attempt that left the queue
between the two reads escaped as `ValueError` from the HTTP handler (`do_POST` 2179) and killed the
request. Now the same race resolves to `queue_position: null`. `contract/v1/schemas/attempt.json`
types `queue_position` as `["integer","null"]`, so null is a legal payload; nothing in this repo
consumes the field (the only other hits are the contract schema and the new tests).

**queue_position semantics for attempts not in the queue.** Unchanged for attempts whose state is
not `queued` (position is not computed at all). For a `queued` attempt absent from the snapshot the
result is now `null` instead of a crash. One inherent property worth naming: the returned position
describes the snapshot instant, so the race test's `queue_position == 1` is a value that the very
next snapshot no longer supports. That is unavoidable for a lock-free read and is the same
"position at read time" semantic the old code intended on its happy path.

### Findings (all non-blocking)

**F1 — Residual, pre-existing race of the same family: `public()` is lock-free and `_queue()`
iterates the live `self.attempts` dict.** The HTTP handlers call `L.public(a)` with no lock
(launcher.py:2098, 2156, 2179, 2217, 2219), while `submit()` inserts into `self.attempts` under
`self.lock` (1557). `_queue()`'s comprehension iterates `self.attempts.values()`, so a concurrent
insert can raise `RuntimeError: dictionary changed size during iteration`. I reproduced the
mechanism with a 3-second stress harness over the real `Launcher.public`/`_queue` (one reader
calling `public()`, one writer inserting): 1 × `RuntimeError: dictionary changed size during
iteration` after 5 reads. Honest caveat: my writer inserted 2.21 M attempts in those 3 seconds,
which is wildly more aggressive than production (one insert per dispatch), so the practical
probability is far lower than that ratio suggests — it is the same order as the bug this PR fixes,
which also needed specific arm/3.13 timing to appear. **Not introduced or worsened by this PR**: the
fix reduces iterations per `public()` call from two to one, so exposure is strictly halved.
Followup worth filing: take the queue snapshot under `self.lock` (it is an `RLock`) or compute the
queue once per request and pass it into `public`/`listing` — that closes the whole class, including
the per-row `_queue()` calls in the 200-row listing (2098) and `len(L._queue())` in the status
endpoint (2135), which are also O(k·n log n) today.

**F2 — Test pins "exactly one" `_queue()` call, which would reject a *better* future fix.**
`assertEqual(len(calls), 1)` in `test_queued_id_present_yields_position_from_one_snapshot` is the
strong discriminator that catches variant A/C, so it is earning its keep now — but a refactor that
hoists the queue per request (zero calls inside `public`) would fail it even though it removes the
race more thoroughly than this patch. Prefer `assertLessEqual(len(calls), 1)` plus an optional
pre-computed queue argument, or leave as-is and accept the tripwire. Worth a line in the code
comment so the next author knows it is deliberate.

**F3 — `backoff` attempts occupy a queue slot but report no position (pre-existing).**
`_queue()` includes states `("queued","backoff")` (1531), while `public()` only computes
`queue_position` for `state == "queued"` (1510). A backing-off attempt therefore shifts everyone
else's position while reporting `queue_position: null` itself. Untouched by this PR and out of its
scope; flagging only because it is the same field.

**F4 — Test hygiene (cosmetic, matches existing suite conventions).** Each `launcher()` in the new
test constructs a `Launcher` whose `asyncio` loop (created at 1455) is never closed, producing 3
`ResourceWarning: unclosed event loop` lines per run; `tempfile.mkdtemp` dirs are also left behind.
Other test modules (`test_bundlebees_api.py`, `test_config_routing.py`) do the same, and warnings
are not errors in CI, so no action required — a `la.loop.close()` in `tearDown` would quiet it.

**Not a finding:** the long line / trailing comment style matches the file's existing dense style,
and CI has no linter gate (only gitleaks and the secrets scan), so there is no style violation to
raise.

## What remains unproven

- The arm64 hardware leg was not executed locally (x86_64 3.13 only); GitHub's `test
  (ubuntu-24.04-arm, 3.13)` context is green on the head, so this is covered by CI, not by me.
- macOS legs were not executed locally.
- The verify-path detour narrative (worker told not to push, missing base object on
  `--commit-local`) cannot be verified from repository state; only its predicted end-state is
  consistent with what I see.
- F1's production probability is unquantified; my stress ratio is not representative.

## Findings summary

| # | Severity | Finding |
|---|---|---|
| F1 | Medium, pre-existing | `public()`/`_queue()` still read the live attempts dict unlocked; concurrent insert → `RuntimeError` (reproduced under stress). PR halves exposure, does not worsen it. |
| F2 | Low | `len(calls) == 1` assertion would reject a future per-request queue hoist; strong discriminator today. |
| F3 | Low, pre-existing | `backoff` attempts consume a queue position but report `null`. |
| F4 | Cosmetic | Unclosed event loops / temp dirs in the new test, consistent with the suite. |

BLOCKING: 0
VERDICT: approve_with_followups
