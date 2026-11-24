VERDICT: APPROVE

# Independent review: wave A slice HY-3 (PR #57)

Reviewed at head `cc5481ecc1a99542db8c123eb5139be5aa3f6f00` (local snapshot
`3487dee`). The base commit `d61ca3212f296eb3cec3b87b9f0ee2e87922492b` is not
present in this sandbox, so, per the supplied facts, the change was judged on
the head tree plus the approved RED file, not on a base-relative diff. The
approved test file `tests/test_prompt_findings_and_landed_id.py` is untouched:
its working-tree sha256 `24607a30…379d` equals the blob at `HEAD`, it contains
no skip/xfail, and `git status --porcelain` was empty before this review. The
bee changed only the leased files `launcher.py` and `docs/bee-landed.md`; the
refactor commit `cc5481e` is included in this head and was judged separately
(finding 3).

## AC mapping

- **Findings are rendered.** `render_prompt` appends `_render_findings(b["findings"])`
  (`launcher.py:1941-1942`); `_render_findings` (`launcher.py:1909-1925`) emits
  `# Research findings` plus `failure_id`, `verdict`, `summary`, a `sources:`
  block and a `rejected alternatives:` block. Covered by
  `FindingsInPrompt::test_findings_are_rendered` (`tests/…:30-37`); the probe run
  below printed every asserted value.
- **Bounded.** Per field `_clip_text` at 200 (`failure_id`, `verdict`), 2000
  (`summary`), 300 (`sources`/rejected) and a final 4000-character clip on the
  assembled section (`launcher.py:1869-1871,1876-1885,1887-1902,1925`).
  Covered by `test_rendering_is_bounded` (`:43-45`); a 50 000-character summary
  produced a 2 325-character prompt.
- **Before `# Rules`.** Parts are appended findings → tests → `# Rules`
  (`launcher.py:1941-1945`). Covered by `test_findings_come_before_the_rules`
  (`:39-41`); the probe also confirmed tests sit between findings and rules.
- **Non-mapping findings are ignored, not crashing.** `isinstance(findings, dict)`
  returns `None` (`launcher.py:1910-1911`); `sources`/`rejected_alternatives`
  that are not lists are skipped (`:1913-1922`). Covered by
  `test_non_mapping_findings_are_ignored_not_crashing` (`:47-49`); additionally
  `{}`, a list of pairs and a plain string all render no section and raise
  nothing (probe).
- **Approved tests are rendered.** `_render_tests` (`launcher.py:1927-1934`)
  keeps non-empty strings, clips each to 500, caps at 50, and emits
  `# Tests that must pass`; appended at `launcher.py:1943-1944`. Covered by
  `test_approved_tests_are_rendered` (`:51-54`).
- **Acceptance-dict shape.** `normalize_brief` flattens `acceptance["tests"]`
  into `"Test passes: <id>"` acceptance lines (`launcher.py:1957-1958`), so
  `tests/test_a.py` appears; covered by `test_acceptance_dict_tests_are_rendered`
  (`:56-58`). Note this shape does **not** get the new header (finding 1).
- **Workflow schema `$id`.** `LANDED_EVENT_SCHEMA`
  (`launcher.py:250`) equals the approved canonical
  `https://github.com/mojomast/workflow/schemas/bee-landed.schema.json` and is
  emitted as `dataschema` (`launcher.py:687`); `docs/bee-landed.md:86` carries
  the identical value, and `docs/CI.md:229` names `mojomast/workflow` as the
  owning repo, so the docs match the code. Covered by
  `LandedEventIds::test_dataschema_is_the_workflow_schema_id` (`:64-65`).
- **No collateral schema change.** `LANDED_EVENT_TYPE` and `LANDED_DATA_SCHEMA`
  (`launcher.py:249,251`) are unchanged; covered by
  `test_type_and_data_schema_are_unchanged` (`:67-69`), which already passed at
  RED and still passes.
- **No `rizstack.dev` left.** `test_no_misspelled_host_left` (`:71-73`) passes;
  a tree-wide grep outside `.git` also finds the string only in that test's own
  assertion.
- **Fail-closed / no gate weakened.** The change only renders prompt text; it
  touches no scope check, sandbox call, validation branch or test. Malformed
  findings, sources, alternatives and tests are dropped, never coerced into an
  error path or an open one. `validate_brief`, `in_assigned_scope` and the
  budget/scope gates are untouched.
- **Scope / deps / secrets.** No new imports or dependencies, no debug prints,
  no `/home`, `/workspace`, `C:\`, private host or IP in the changed lines. The
  one URL added is the public schema `$id` the AC requires. No scope creep: all
  six helpers are used, and nothing outside the two leased files changed.

## Findings

1. **[minor] The acceptance-dict brief shape never gets the new
   `# Tests that must pass` section.** `normalize_brief` only rewrites
   `acceptance["tests"]` into acceptance strings (`launcher.py:1957-1958`); it
   never sets `b["tests"]`, so `_render_tests(b.get("tests"))`
   (`launcher.py:1943`) returns `None` for that shape. The named test passes
   incidentally because the path string lands under `# Acceptance criteria` as
   `- Test passes: tests/test_a.py` (`tests/…:56-58`; the probe confirmed the
   header is absent). The AC is met by its named test as written, so this is
   not a blocker; it is a gap between the test's intent and what it exercises.
   Concrete fix (follow-up slice, since the approved test is read-only): in
   `normalize_brief`, also set `b["tests"] = [t for t in acc.get("tests") or []
   if isinstance(t, str)]` when the brief has no top-level `tests`, and pin the
   header with a new test asserting `"# Tests that must pass" in prompt` for
   the acceptance-dict shape.
2. **[minor] `_render_tests` has no aggregate bound.** Per-item (500) and count
   (50) caps (`launcher.py:1930-1931`) allow a ~25 KB section; the prompt is
   then re-measured against `max_prompt_chars`, whose smallest value is 60 000
   (`launcher.py:67,1517-1519,2835-2836`). A brief that fit before this slice
   can therefore newly raise `brief too large` or drop a free model from
   `_pick_free`. Concrete fix: clip the assembled section, e.g.
   `_clip_text("# Tests that must pass\n" + …, 8000)`, matching the 4000-char
   bound already applied to findings.
3. **[minor] The refactor is behaviour-preserving as far as it is verifiable,
   but the pre-refactor function is not in the checkout.** The single squashed
   commit has no parent, so the worker's original `_render_findings` cannot be
   diffed and the PR's "6 006 briefs, 0 mismatches" claim cannot be reproduced
   here. Judged from the head code and probes, the split preserves the
   semantics that matter: fixed field order `failure_id`, `verdict`, `summary`;
   `sources:` before `rejected alternatives:`; per-field clipping boundaries
   (200/2000/300) and the 4000-char final clip; `str.strip()` on every rendered
   value; and bool/`None`/container exclusion via `_is_scalar`
   (`launcher.py:1873-1874`) — a probe showed `True` dropped while `1` is kept.
   No corrective change is required; keep the harness result in the PR record
   as the evidence.
4. **[minor, defence-in-depth] Finding text is interpolated verbatim, so a
   newline plus a `# ` heading can forge a section ahead of the real
   `# Rules`.** `_render_findings` does not neutralise line breaks in
   `summary`, source refs or rejected alternatives (`launcher.py:1876-1925`).
   Nothing in the repo consumes the prompt programmatically and every
   enforcement point (scope, sandbox, budget, checks) stays in code, so this
   weakens no gate; it is only a model-instruction surface, the same class as
   the already-verbatim `objective`. If wanted, collapse newlines or prefix
   continuation lines in `_clip_text` in a later slice; not required for this
   AC.

## Verification

- Approved test file read in full; its nine tests map to the RED/GREEN ids
  listed in the supplied facts. No approved test, gate or check was edited,
  skipped or weakened.
- Edge probes: non-dict/empty findings, non-scalar fields, bool exclusion,
  non-list `sources`, non-dict source entries, missing/blank `kind`, mixed and
  non-scalar `rejected_alternatives`, >20 sources, 50 000-char summary, exact
  and tiny clip limits, non-string/blank/>50 test entries, and a top-level
  `tests` list — all bounded, ordered and silent on bad input.
- Full suite at head: 573 tests, 0 failures, 4 skips, all pre-existing and
  environmental (`test_r4_run_command_design` needs full Git history;
  `test_sdk_contract` needs the `cua` SDK). None is in the approved file.
- `git status --porcelain` was empty before writing this file and shows only
  this file afterwards.

## Commands run (exit codes)

1. `timeout 300 python3 -m pytest -q -p no:cacheprovider tests/test_prompt_findings_and_landed_id.py`
   — **exit 1**: `/usr/bin/python3: No module named pytest` (this image has no
   pytest, as the supplied facts warned). No test executed.
2. `timeout 300 python3 -m unittest tests.test_prompt_findings_and_landed_id -v`
   — **exit 0**: Ran 9 tests, `OK`.
3. `timeout 600 python3 -m unittest tests.test_landing_events -q`
   — **exit 0**: Ran 18 tests, `OK`.
4. `timeout 1800 python3 -m unittest discover -s tests -q`
   — **exit 0**: Ran 573 tests in 110.992 s, `OK (skipped=4)`.
5. `timeout 1800 python3 -m unittest discover -s tests -v` (skip audit)
   — **exit 0**: the four skips are the two environmental groups above; the
   approved file contributes none.
6. Inline `python3` probes of `_render_findings`, `_render_tests`,
   `normalize_brief` and `render_prompt` (no files written)
   — **exit 0**: outputs quoted in the findings above.
