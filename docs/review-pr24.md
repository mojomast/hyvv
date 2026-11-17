# Independent review — PR #24 `fix/patch-collect-lease-scope`

- **Reviewed head:** `7f05206ea79488bf6bafb27f78bc974cb2e358b1`
- **Base:** `961a8bce501d0ce38922baeb6e5c10347d1f7efd` (main)
- **Claim:** defect (g) — the patch collector picked up untracked suite side-effect
  artifacts (the `uv.lock` class) as in-scope evidence, so the verify gate refused
  whole payloads on files the bee never intended to deliver.
- **Method:** this workspace is a single-commit snapshot whose tree was proven
  byte-identical to the PR head (all 111 paths, blob-hash equality against
  `7f05206` fetched read-only into `/tmp/opencode/hyvv.git`; the local repo was
  never mutated). Base-revision experiments ran in throwaway git worktrees under
  `/tmp/opencode`, never in the reviewed workspace.
- Note: the SHA prefix `eadf3b0` named in the review brief does not resolve
  upstream (`No commit found for SHA: eadf3b0`); RED (a) was demonstrated at the
  exact PR base `961a8bce`, which is strictly the base revision of this PR.

## Verdict: APPROVE

Non-blocking observations (numbered, none blocking) are listed after the checks.

---

## 1. Delta exactness — PASS

Verified two independent ways:

- GitHub PR API: `changed_files: 2`, `commits: 1`; file list is exactly
  `launcher.py` (modified, +36/−10) and `tests/test_workspace_evidence.py`
  (modified, +61/−2).
- Local: `git diff --name-status 961a8bce 7f05206` in a read-only clone of
  upstream returns exactly those two files, both `M`.

```
 launcher.py                      | 46 ++++++++++++++++++++++-------
 tests/test_workspace_evidence.py | 63 ++++++++++++++++++++++++++++++++++++++--
 2 files changed, 97 insertions(+), 12 deletions(-)
```

No third file, no schema/docs/workflow churn.

## 2. RED-first, both ways — PASS

### (a) Untracked out-of-lease artifact is excluded (RED at base, GREEN at head)

Setup at base revision `961a8bce` (throwaway worktree): the head version of
`tests/test_workspace_evidence.py` was copied in and the specific RED test run.

```
test_untracked_out_of_lease_artifact_is_not_carried_in_the_payload ... FAIL
AssertionError: Lists differ: ['b.txt', 'uv.lock'] != ['b.txt']
```

At base, the in-lease tracked modification (`b.txt`) **and** the untracked
out-of-lease `uv.lock` both land in the collected payload — the defect. Full RED
set at base (head test file against base `launcher.py`):

| test (head) | at base 961a8bce |
|---|---|
| test_untracked_out_of_lease_artifact_is_not_carried_in_the_payload | **FAIL** (uv.lock in payload) |
| test_quoted_untracked_artifact_is_excluded_without_shell_interpolation | **FAIL** (no exclusion) |
| test_failed_unstage_command_is_reported | **FAIL** (no unstage stage exists) |
| test_unscoped_lane_keeps_collecting_untracked_files | **ERROR** (no `scope_note` key yet) |
| test_tracked_out_of_lease_change_still_reaches_the_verifier | ok (invariant already held; not RED, correctly) |

At head `7f05206` (this workspace, unmutated) all five pass (see check 5).

The fix mechanics verified in `launcher.py:1211-1257`: the untracked set is
captured with `git ls-files --others --exclude-standard -z` **before**
`git add -A` (line 1217), so staging cannot hide it; exclusion drops only rows
with `status == "A"` **and** membership in that pre-add untracked set **and**
out-of-lease membership (`launcher.py:1234`), unstages them via
`git rm -q --cached -f --ignore-unmatch -- <shlex.quote(p) ...>` (line 1236,
quoted; worktree file is left intact), and re-cuts `entries()` and the binary
diff from the post-unstage index.

### (b) No-weakening conjunct (mutation testing at head)

Reviewer scratch test (`tests/test_review_red_noweaken.py`, review scaffolding
only, exists solely in throwaway worktrees, not part of the PR): a brand-new
out-of-lease file the bee **commits** (tracked before collection, `A`-status
against the seed, not in the pre-add untracked set) must still reach the
verifier, while an untracked `uv.lock` side-effect artifact in the same tree
stays excluded.

- Unmutated head: **OK** — `bee_new.py` flows (`status A`, `in_assigned_scope:
  false`, present in `diff`), `uv.lock` excluded with `excluded_artifacts`.
- **Mutation B1** — drop the untracked-set membership check
  (`st == "A" and not in_assigned_scope(...)`, sha256
  `d50c4da72f5a0ee7064bfdc0e2673ad673cab194c66d650ddeefe34197b30540`): scratch
  test **FAILS** (`bee_new.py` wrongly dropped). The PR's own committed suite
  (`tests.test_workspace_evidence`, 21 tests) still passes entirely — the
  committed suite does not pin this conjunct (see observation O1).
- **Mutation B2** — exclude ALL A-status rows (`st == "A"`, sha256
  `7405d0e50c48a3cd3d78fd5e87d0b073ad491b4afbae2d98206b066e04dbd126`): scratch
  test **FAILS**, and the committed suite **FAILS** (3 failures, 1 error — the
  in-lease bee-committed `b.txt` case is caught there).
- Both mutations were reverted; `launcher.py` restored byte-identical to head,
  sha256 `bb6c7bf0319a7ad729821df96a8332fd2efa1305ab905edaca21f532324d7dfe`
  (matches pre-mutation value; `git diff HEAD -- launcher.py` empty).

**Conclusion:** exclusion requires `A`-status AND pre-add untracked membership;
tracked out-of-lease changes (M/D/T, and A for files the bee committed) always
reach the verifier. No refusal is weakened.

## 3. `excluded_artifacts` carries path + reason — PASS

`launcher.py:1254` emits
`evidence["excluded_artifacts"] = [{"path": p, "reason": "untracked_outside_assigned_files"} for p in dropped]`,
 asserted verbatim in `test_untracked_out_of_lease_artifact_is_not_carried_in_the_payload`
 and `test_quoted_untracked_artifact_is_excluded_without_shell_interpolation`.
The full evidence object (diff/files/base + `excluded_artifacts`/`scope_note`)
flows verbatim into the result manifest via `"patch": patch`
(`launcher.py:2262`), so the verifier sees what was withheld and why.

## 4. Unscoped lanes unchanged — PASS

`scoped = isinstance(lease, (list, tuple)) and bool(lease)`; missing, empty, or
non-list leases take the unscoped lane: no `ls-files` probe, no filtering,
`dropped = []`, and the evidence gains only the additive
`scope_note: "no assigned_files lease: untracked files collected unfiltered"`
(`launcher.py:1255-1257`). Coverage: real-Git missing-lease test
(`test_unscoped_lane_keeps_collecting_untracked_files`, RED at base for the
missing key) plus `[]`/`None` lease lanes in `tests.test_assigned_scope`
(`test_empty_scope_marks_patch_files_out_of_scope`). The diff/files payload in
the unscoped lane is exactly the pre-fix shape plus the note; the full suite
(398 tests) confirms no consumer regression.

## 5. Test gates at the reviewed head — PASS

In this workspace, tree byte-identical to `7f05206`:

```
python3 -m unittest tests.test_workspace_evidence tests.test_assigned_scope tests.test_acceptance_class
Ran 56 tests in 1.128s — OK            (21 + 11 + 24)
python3 -m unittest discover -s tests -t .
Ran 398 tests in 71.316s — OK (skipped=3)
```

Matches the worker claim (398 OK / 3 pre-existing skips) exactly.

## 6. CI at the exact head — PASS (all completed, none pending)

GitHub check-runs for `7f05206ea79488bf6bafb27f78bc974cb2e358b1`
(run 44071180116): **14 of 14 completed, conclusion success** — `CI`,
`scan`, `bee-ci/unit`, `bee-ci/contract`, `bee-ci/scan`,
`bee-ci/release-assets`, `bee-ci/installer-dry-run`, `test` matrix
(ubuntu 3.12/3.13, ubuntu-24.04-arm 3.12/3.13, macos 3.12/3.13),
`provisioning (windows)`. Nothing pending. PR `mergeable_state` is `clean`
(`mergeable: true`); the legacy combined *status* endpoint reports `pending`
only because zero legacy statuses are posted (GitHub's no-statuses default) —
the check-runs API is the authoritative signal here.

## 7. Scanner hygiene — PASS

- `bash scripts/scan-secrets.sh` → exit 0 (clean; same scanner CI runs as
  `scan`/`bee-ci/scan`).
- The PR diff hunks contain no credentials or secret-shaped content (the only
  token-adjacent diff line is the pre-existing `_inject_bbs_token` context
  line, unchanged by this PR).
- Exactly one synthetic negative-control string in the touched test file,
  pre-existing and clearly marked as a scrub fixture:
  `"Bearer " + "fixture-token"` in `test_structured_error_scrubs_stderr`
  (tests/test_workspace_evidence.py:300), which asserts the marker is scrubbed
  from error output. No new secret-shaped strings introduced.

---

## Non-blocking observations

- **O1 — missing committed regression test for the no-weakening conjunct.**
  Mutation B1 (drop the untracked-set membership check) is not caught by the
  committed suite; only the reviewer's scratch test detects it (B2 is caught by
  `test_worker_committed_change_survives_successful_controller_finish`). The
  invariant "A for files the bee committed always reaches the verifier" is
  correctly implemented but unpinned by CI. Recommendation: add a real-Git test
  committing a new out-of-lease file alongside an untracked side-effect
  artifact (the scratch test's shape) in a follow-up.
- **O2 — plain-`mv` rename asymmetry (matches the spec's letter).** For an
  out-of-lease tracked file moved via plain `mv` (unstaged at collection), the
  destination is A-status and pre-add untracked, so it is excluded while the
  source's `D` remains: the payload shows a pure deletion. A `git mv` (already
  staged) is not in the pre-add untracked set and delivers intact. This follows
  the exclusion rule exactly as specified; noting only the behavioral nuance.
- **O3 — `eadf3b0` in the review brief does not resolve upstream**; RED (a) was
  demonstrated at the exact PR base `961a8bce` instead (strictly correct).

## Integrity ledger (sha256)

| artifact | sha256 |
|---|---|
| workspace `launcher.py` before/after all experiments | `bb6c7bf0319a7ad729821df96a8332fd2efa1305ab905edaca21f532324d7dfe` |
| workspace `tests/test_workspace_evidence.py` before/after | `dadd9c3c9169cab584d2c0c30ab26f5d258a162b81a1620fa601ab5d69af871f` |
| mutated launcher (B1) | `d50c4da72f5a0ee7064bfdc0e2673ad673cab194c66d650ddeefe34197b30540` |
| mutated launcher (B2) | `7405d0e50c48a3cd3d78fd5e87d0b073ad491b4afbae2d98206b066e04dbd126` |
| base test file at 961a8bce (overwritten in throwaway worktree) | `ff40070e0c29bdc34573e3fe0af85c21834b58b2c299d8526d575f36c246b2bb` |

`git status` in the reviewed workspace was clean before and after every
experiment; all mutations happened in `/tmp/opencode` worktrees.

---

## Blocking items

None.

**Verdict: APPROVE** (with non-blocking observations O1–O3 above).

```json
{
  "summary": "PR #24 verified at head 7f05206 against base 961a8bce: delta exactly launcher.py + tests/test_workspace_evidence.py; RED-first both ways proven (untracked uv.lock-class artifact excluded post-fix and present at base; no-weakening conjunct mutation-tested with a reviewer scratch test); excluded_artifacts carry path+reason; unscoped lanes unchanged with scope_note; 56-test floor and 398-test full suite green at head; CI 14/14 success at the exact head; scanner clean.",
  "changed_files": ["launcher.py", "tests/test_workspace_evidence.py"],
  "checks": [
    {"name": "delta_exactness", "command": "git diff --name-status 961a8bce501d0ce38922baeb6e5c10347d1f7efd 7f05206ea79488bf6bafb27f78bc974cb2e358b1 (read-only clone) + PR API changed_files", "passed": true},
    {"name": "red_first_untracked_artifact", "command": "head tests/test_workspace_evidence.py at base worktree 961a8bce: test_untracked_out_of_lease_artifact_is_not_carried_in_the_payload -> FAIL (uv.lock in payload); at head -> OK", "passed": true},
    {"name": "red_first_no_weakening", "command": "mutation tests in head worktree: B1 drop untracked-membership conjunct -> scratch test FAILS (committed suite still green, gap O1); B2 exclude all A-rows -> scratch test + committed suite FAIL; restores byte-identical (sha256 bb6c7bf...)", "passed": true},
    {"name": "excluded_artifacts_shape", "command": "launcher.py:1254 + tests assert [{'path','reason':'untracked_outside_assigned_files'}]; flows into result manifest via 'patch' (launcher.py:2262)", "passed": true},
    {"name": "unscoped_lane_unchanged", "command": "test_unscoped_lane_keeps_collecting_untracked_files + test_empty_scope_marks_patch_files_out_of_scope ([], None); scope_note asserted", "passed": true},
    {"name": "test_floor_56", "command": "python3 -m unittest tests.test_workspace_evidence tests.test_assigned_scope tests.test_acceptance_class -> Ran 56, OK", "passed": true},
    {"name": "full_suite", "command": "python3 -m unittest discover -s tests -t . -> Ran 398, OK (skipped=3)", "passed": true},
    {"name": "ci_at_head", "command": "GET /repos/mojomast/hyvv/commits/7f05206.../check-runs -> 14/14 completed, success; nothing pending", "passed": true},
    {"name": "scanner_hygiene", "command": "bash scripts/scan-secrets.sh -> exit 0; diff hunks free of secret-shaped content; one pre-existing marked synthetic negative-control (fixture-token)", "passed": true},
    {"name": "workspace_integrity", "command": "sha256 of launcher.py + tests/test_workspace_evidence.py identical before/after all experiments; git status clean", "passed": true}
  ],
  "open_issues": [
    "O1 (non-blocking): committed suite does not pin the no-weakening conjunct for bee-committed A-status out-of-lease files (mutation B1 undetected by CI); add a real-Git regression test",
    "O2 (non-blocking): plain-mv rename of an out-of-lease tracked file delivers the D without the destination A (spec-conformant; behavioral nuance)",
    "O3 (non-blocking): eadf3b0 from the brief does not resolve upstream; RED (a) used the exact PR base 961a8bce"
  ]
}
```
