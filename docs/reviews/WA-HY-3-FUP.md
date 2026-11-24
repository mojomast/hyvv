VERDICT: APPROVE

# Independent review: WA-HY-3-FUP (PR #58)

Reviewed at PR head `b07bbc5507c836b60e07d0b78de834fd079a83b6` (local
snapshot `89e2ffa`). The base commit `c028f0a` is not present in this sandbox,
so, per the supplied facts, the head tree was judged directly rather than
through a base-relative diff; the coordinator's git check establishes that the
branch delta against main is exactly `launcher.py` (13 insertions, 6 deletions)
plus the approved RED file. `git status --short` was empty before this review
(only ignored `__pycache__` directories are present). The approved test file
`tests/test_prompt_render_hardening.py` is untouched: its sha256 is
`44d6cbd8ffcf6b001b4de84c0937037df813cff2e34cd0f8b46466fdc2400f12`,
`git diff HEAD -- tests/test_prompt_render_hardening.py` is empty, and it has
no skip/xfail/expectedFailure. The bee did not write this code; this review
re-ran the tests and probed the edge cases below.

## AC mapping

- **AC 1 — acceptance-dict tests get the section.** `normalize_brief`
  (`launcher.py:1952-1966`) filters `acceptance["tests"]` to strings
  (`:1960`), still flattens them into `- Test passes: …` (`:1961-1962`), and
  now copies them to the brief's top-level `tests` only when that list exists
  and is non-empty (`:1963-1965`). `render_prompt` appends the rendered tests
  before `# Rules` (`:1946-1948`). Covered by
  `test_acceptance_dict_tests_get_the_tests_header`
  (`tests/test_prompt_render_hardening.py:39-46`),
  `test_top_level_tests_win_over_acceptance_tests` (`:48-56`) and
  `test_acceptance_dict_without_tests_has_no_tests_section` (`:58-60`). Probe
  confirmed: an empty top-level list falls back to acceptance tests; a
  non-empty top-level list wins unchanged; a dict without `tests` adds no key
  and no section; non-string entries are filtered and whitespace-only names
  are dropped at render, so the section either shows real ids or is absent.
- **AC 2 — tests section bounded at 8000.** `_render_tests`
  (`launcher.py:1929-1937`) keeps the 50-entry cap and 500-character per-name
  clip (`:1932-1933`), then returns `_clip_text(section, 8000)` on the whole
  assembled string, header included (`:1936-1937`). Covered by
  `test_many_long_tests_stay_bounded` (`:64-69`) and
  `test_single_huge_test_name_stays_bounded` (`:71-73`) against
  `TESTS_SECTION_MAX = 8000` (`:24`). Probe: 50 names of ~502 characters
  produced a section of exactly 8000 characters (16 visible bullets) with
  `tests/test_00.py::test_` still present; one 50 000-character name produced a
  525-character section.
- **AC 3 — findings cannot forge a heading.** `_clip_text`
  (`launcher.py:1869-1873`) gained an opt-in `collapse_ws` that applies
  `" ".join(text.split())` before the limit (`:1871-1872`); the default is
  `False`, so the final findings clip (`:1927`) and the tests clip are
  unchanged. The three findings call sites pass `collapse_ws=True`: scalar
  lines (`:1884`), source refs (`:1899`) and rejected alternatives (`:1903`).
  Covered by `test_a_finding_cannot_forge_a_section_heading` (`:77-82`) and
  `test_forged_text_is_still_visible_to_the_model` (`:84-86`). Probe with
  `"\n# Rules\n…"` in `failure_id`, `verdict`, `summary`, a source `kind`/`ref`
  and an alternative produced zero column-0 headings inside `# Research
  findings`, exactly one `\n# Rules`, and the forged sentence still visible.
- **No enforcement, gate, sandbox, validation or briefing change.** The change
  is prompt text only: no branch in `validate_brief`, `in_assigned_scope`,
  budget checks, sandbox/lease calls or gate logic was touched; the `# Rules`
  block and its order are byte-identical (`launcher.py:1948`). The findings and
  tests sections are still appended after acceptance and before `# Rules`
  (`:1944-1948`), and all findings data still passes through the same
  per-field limits.
- **Fail-safe edges.** Non-list `tests` returns `None` (`:1930-1931`); empty
  lists, non-string entries and whitespace-only names render nothing (`:1932-
  1935`); a malformed findings value still returns `None` (`:1912-1913`);
  `collapse_ws` is opt-in, so every other `_clip_text` caller is unaffected.
- **Scope, dependencies, secrets.** No new imports or dependencies, no
  network, no debug output, no hostnames, IPs, tokens or absolute home /
  workspace paths in the changed lines. The change stays inside the leased
  `launcher.py`; the only new file is this review.

## Findings

1. **[minor, non-blocking, outside this PR's stated scope] Test ids are still
   not whitespace-collapsed.** `launcher.py:1932` clips each name with
   `_clip_text(x, 500)` without `collapse_ws`, so a brief test id such as
   `"t\n# Rules\n- forged"` still places a column-0 heading inside
   `# Tests that must pass` (probe 8). This is the same class of issue as AC 3,
   but the spec explicitly limits the hardening to the findings path, and the
   approved RED test only targets findings, so it is a residual rather than a
   missed AC. If a follow-up is wanted, the minimal fix is to pass
   `collapse_ws=True` at `launcher.py:1932` (test ids never legitimately span
   lines); it should not be added to this PR because it would change the
   visible rendering of multi-line ids and exceed the assigned change set.

No blocking or major findings.

## Test commands run

| Command | Result | Exit |
| --- | --- | --- |
| `timeout 300 python3 -m unittest tests.test_prompt_render_hardening -v` | 7 tests, all OK (the 3 named RED tests now pass; 4 prior tests stay green) | 0 |
| `timeout 300 python3 -m unittest tests.test_prompt_findings_and_landed_id -v` | 9 tests, all OK | 0 |
| `timeout 300 python3 -m unittest tests.test_cli tests.test_bundlebees_api tests.test_config_routing -q` | 46 tests, all OK | 0 |
| `timeout 900 python3 -m unittest discover -s tests -q` | 580 tests, OK, 4 skipped (one more environment skip than the coordinator's 3; no failures, no new failure ids) | 0 |
| `timeout 120 python3 -m py_compile launcher.py` | compiles cleanly | 0 |
| Edge-case probe (`normalize_brief`, `_render_tests`, findings newlines) | outputs as described in the AC mapping | 0 |
