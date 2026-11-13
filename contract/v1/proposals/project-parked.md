# Proposal: refuse dispatch to parked or archived projects

Status: **proposed, aligns with workflow docs/18-parking-projects.md (MR.INDIA, in progress)**.
Not implemented in launcher v0.1.15. **Update:** the gate is now implemented in the `/v1` reference (`contract/v1/project_state.py`, tests in `contract/v1/tests/test_project_state_gate.py`); see `contract/v1/README.md`. This file is the original proposal.

## Rule

When creating or dispatching an attempt, hyvv looks at the authorized project's repository for `.workflow/project-state.json`.

That file has:

| field | meaning |
| --- | --- |
| `state` | `active`, `parked`, or `archived` |
| `reason` | why the project entered that state |
| `at` | when it was set |
| `by` | who set it |

- `state: active`, or a missing state file: dispatch proceeds as today.
- `state: parked` or `state: archived`: `POST /v1/attempts` returns **409** with `code` / `error` **`project_parked`**. The attempt is not created.
- The only way past that refusal is an **explicit override plus a reason**. Both are stored on the attempt (override flag, reason, the project state's `state`/`at`/`by`, and when and by whom the override was applied). An override without a reason is **400**.
- The 409 body may include the project's `state`, `reason`, `at`, and `by` (no repo paths).

This is additive: existing coordinators that never send an override keep the safe default (parked and archived projects are refused).
