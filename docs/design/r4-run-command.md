DESIGN-ONLY: R4 run-command job type design. No launcher.py change exists.

This document is rev-2 of the design previously verified at 1ae4d4eba47a64d0ca62d9b4c9b956f557be13f6. Its decisions and section structure are carried forward; only statements affected by current main at source_rev 2a3bdd4584ac8d44b0d250ef4de587692748894e are updated. No launcher change, deploy, new route, policy enrollment, or launcher.py change exists or is proposed by this deliverable.

## 0 Scanner-conformance note

This deliverable is written to survive the repository secret scanner. It contains no credential-class word paired with a colon or equals sign and a value, no cloud, source-hosting, chat-platform or AI-provider key-shaped literal, no PEM block, and no encoded claim. The only long hexadecimal strings in this document are the two public commit ids named above. Transport authentication is described only by naming `make_handler._auth`; no example header is given. If a future revision needs a per-request access-evidence property, the reserved non-triggering name is `auth_context`.

## 1 Purpose and the two existing shapes it does not duplicate

`run-command` is a no-model job that runs exactly one owner-vetted command against an enrolled project at a recorded source revision, inside the existing throwaway Docker check container, and returns exit status plus redacted logs. It exists so a coordinator can request a narrow, pre-approved operational command without asking a model to choose commands and without scheduling a list of unrelated commands.

It does not duplicate the two agent-run shapes already on main:

- `opencode` and `agy-cua` are model-backed agent runs. `opencode` resolves a model through `model_spec`; `agy-cua` resolves through `AGY_TIERS`/`AGY_MODELS`. Both build a prompt, can carry `assigned_files`, and are collected as a patch plus a result. `run-command` has no model, no prompt harness, no assigned-file lease, and no diff.
- `kind=check` is a check command list. `L.submit` accepts a non-empty `brief.commands` list, and `DockerCheckRunner.run_checks` executes each item sequentially in one container, producing one result row per command. `run-command` carries one `brief.command` string, requires an allowlist match before any container starts, and produces one result row.

## 2 Relationship to the existing kinds

At source_rev, `KINDS` is `("opencode", "agy-cua", "check")`. `L.submit` reads `kind` from the request's top-level field first, then from `brief.kind`, then defaults to `opencode`; an unrecognised value raises `unknown kind`, which the handler maps to 400. `run-command` is therefore **not dispatchable today** and this design does not make it so.

A future implementation would add a fourth kind while preserving the existing three:

| Kind | Model | Command source | Result | Change in this design |
| --- | --- | --- | --- | --- |
| `opencode` | resolved via `model_spec` | agent prompt | patch + result | none |
| `agy-cua` | resolved via `AGY_TIERS`/`AGY_MODELS` | agent prompt | patch or UI observation | none |
| `check` | none | `brief.commands` list | `check_result` manifest with N rows | none; this design reuses its runner |
| `run-command` | none | one allowlisted `brief.command` | same manifest with `source.kind=run-command` and one row | design only; not enrolled |

The prior-art decision not to fold `run-command` into `check` is kept: command lists remain an unfiltered launcher capability with a pre-existing allowlist exposure (N3), while `run-command` is a separately gated shape.

## 3 Request and response sketch with a field-rules table naming every refusal

The strict request shape below is the target for a future implementation. It is not accepted by source_rev: current `make_handler.do_POST` caps the body at 256000 bytes, parses JSON, mints `attempt_id` when omitted, calls `normalize_brief` and `validate_brief`, checks `brief.project` against `L.cfg["projects"]`, and passes the top-level `kind` to `L.submit`. `validate_brief` requires only `objective`, validates a few string-list fields, and ignores unknown fields; the stricter rules below must be added before enrollment.

```json
{
  "request": {
    "kind": "run-command",
    "attempt_id": "att_run_command_001",
    "brief": {
      "objective": "Re-run the design pin test on the recorded source revision",
      "project": "hyvv",
      "source_rev": "2a3bdd4584ac8d44b0d250ef4de587692748894e",
      "purpose": "verification",
      "command": "python -m pytest tests/test_r4_run_command_design.py -q",
      "budget": {"timeout_s": 300}
    }
  },
  "response": {
    "attempt_id": "att_run_command_001",
    "kind": "run-command",
    "state": "queued",
    "detail": "waiting for a worker slot",
    "security_profile": "default"
  }
}
```

Every field refusal is named below. Authorization and lifecycle refusals that are not field-shape constraints are named in sections 5 and 10.

| Property | Rule | Refusal |
| --- | --- | --- |
| `kind` | Required, exact string `run-command`; top-level wins over `brief.kind`. | `request_kind_invalid` |
| `brief` | Required object. | `brief_not_object` |
| `attempt_id` | Optional; when present, `^[A-Za-z0-9_-]{8,64}$`. | `attempt_id_invalid` |
| top-level extra field | Only `kind`, `brief`, `attempt_id`; notably no `model`, `tier`, `mode` or top-level `project`. | `request_field_not_allowed` |
| `brief.objective` | Required non-empty string. | `objective_required` |
| `brief.project` | Required slug string `^[a-z][a-z0-9_-]{0,63}$`; enrollment is a separate gate. | `project_invalid` / `project_not_enrolled` |
| `brief.source_rev` | Required 40- or 64-hex revision string. | `source_rev_invalid` |
| `brief.purpose` | Required slug string `^[a-z][a-z0-9_-]{0,63}$`; owner enum is N4. | `purpose_invalid` / `purpose_not_allowed` |
| `brief.command` | Required non-empty string, provisional cap 2000 chars (N4). | `command_invalid` |
| `brief.command` | Longer than the provisional cap. | `command_too_long` |
| `brief.budget` | Required object with only `timeout_s`. | `budget_not_object` |
| `brief.budget.timeout_s` | Required integer 10..7200. | `budget_timeout_s_invalid` |
| `brief.commands` | Must not appear; that is the `check` list shape. | `command_list_not_allowed` |
| `brief.security_profile` | Must not appear; profile comes from the matched policy entry. | `security_profile_not_allowed` |
| `brief.model`, `brief.tier`, `brief.mode` | Must not appear; there is no model route. | `model_field_not_allowed` |
| `brief.verify_of` | Must not appear; opencode-only. | `verify_of_not_allowed` |
| any other `brief` field | Strict envelope: no unknown brief fields. | `brief_field_not_allowed` |

The response sketch follows `L.public`: attempt identity, kind, state, detail, and the resolved profile. It carries no command text, no log text, and no access evidence.

## 4 Draft schema and envelope strictness

`schemas/drafts/run-command-job.draft.schema.json` is JSON Schema draft 2020-12, marked draft / rev-2 / not-frozen. It pins the request envelope in section 3 with `additionalProperties: false` at the top level, at `brief`, and at `brief.budget`. Forbidden known fields are additionally represented by `not: {}` so the intended refusal is explicit in the schema rather than only implied by strictness.

Current main is looser: `normalize_brief` leaves unknown fields untouched and `validate_brief` ignores them, while the handler accepts top-level fields such as `model`, `tier`, `mode` and `project` for other kinds. The draft is therefore a target contract, not a description of source_rev behaviour. An implementation must reject those fields for `run-command` before the kind is enrolled.

The body-size ceiling and attempt-id minting already in `make_handler.do_POST` remain useful as-is. A top-level `project` is a route-preview convenience only (section 11) and must not be smuggled into dispatch through this envelope.

## 5 Authorization model: five gates, each fail-closed

The five gates run in order. Every gate refuses on missing, malformed, or ambiguous input; none falls through to a broader default.

1. **Host transport check.** The existing check in `make_handler._auth` is the first gate. Nothing about `run-command` changes it. Failure is `unauthorized`; the connection is closed rather than reused. No example header appears in this design.
2. **Enable gate.** A host config flag for `run-command` must be explicitly true. Missing config, false, or a non-boolean value is `run_command_disabled`. Absence is never read as enabled.
3. **Enrolled policy file.** The config must name a host-local policy file that parses, validates, and carries owner approval. Missing, unreadable, malformed, or unapproved files refuse as `policy_unavailable` or `policy_not_approved`. The file is never accepted from the request.
4. **Per-request allowlist match.** The exact tuple `(project, purpose, command)` must appear in the approved policy. Matching is exact string equality: no globs, no regex, no shell reinterpretation, and no prefix match. A non-match is `command_allowlist_denied`; an unknown project is `project_not_enrolled` and a purpose outside the owner list is `purpose_not_allowed`. The matched entry supplies the security profile; the request cannot choose one.
5. **Budget.** `budget.timeout_s` must pass the field range and be no greater than the policy entry's approved maximum. Missing or malformed budget is `budget_timeout_s_invalid`; above the policy maximum is `budget_exceeds_policy_max`. There is no implicit clamp (section 8).

## 6 Run-command policy file (enrolled, owner-approved)

The policy file is host-local state enrolled by the owner, not a request field and not a new route. It is loaded only after the enable gate passes.

| Field | Purpose |
| --- | --- |
| `schema` | Fixed policy identifier for rev-2. |
| `owner_approval.approved_by`, `approved_at`, `review_ref` | Evidence that an owner reviewed this exact file. Missing or partial approval refuses. |
| `max_timeout_s` | Ceiling for gate 5; owner value pending N4 and must not exceed 7200. |
| `allowlist[].project` | Enrolled project name; must match `brief.project`. |
| `allowlist[].purpose` | Allowed purpose label; values are the owner enum in N4. |
| `allowlist[].command` | Exact command string; the request must match byte for byte. |
| `allowlist[].security_profile` | Profile passed to `DockerCheckRunner`; must be `default` or a configured profile. |
| `allowlist[].description` | Human-readable reason for the entry; never executed. |

An entry with a malformed profile, duplicate `(project, purpose, command)` tuple, or missing approval is invalid policy and refuses closed. Policy loads do not mutate launcher config and this design does not enroll a file.

## 7 Isolation and execution path reusing DockerCheckRunner, with the differences from check spelled out

The future execution path reuses `DockerCheckRunner` unchanged:

- `start` resolves the security profile and creates one throwaway container via `docker_check_argv`: no privileged mode, extra capabilities, host namespaces or bind mounts; network defaults to `none`; a configured seccomp file is applied only through a validated profile.
- The enrolled project is exported with `export_project` using `brief.source_rev`, streamed to `/work/<project>`, and initialised as a fresh git repo with `refs/hyvv/seed` at the seeded revision.
- The command runs through `docker exec ... bash -lc <command>` in that workdir under the command timeout. stdout and stderr are redacted; tails are kept in the result and the full text is written to a per-attempt log.
- `cleanup` removes only this attempt's own container.

Differences from `check`:

1. `run-command` is not in `KINDS` at source_rev; dispatch refuses it until a future implementation enrolls it.
2. One `brief.command`, not a `brief.commands` list; `run_checks` is invoked with a single-item view rather than accepting a caller-supplied list.
3. All five authorization gates complete before `check_start`; `check` has no per-command allowlist today (N3).
4. The request cannot name a security profile; the matched policy entry supplies it.
5. The receipt sets `source.kind` to `run-command` while retaining the existing manifest schema id.
6. The container naming and label helpers remain the existing check-oriented ones for this rev; changing them is out of scope.

## 8 Budget and timeout semantics (defaults, refuse-vs-clamp, worked arithmetic)

The shared runner constants at source_rev are `CHECK_TIMEOUT_S = (10, 1800, 7200)` and `CHECK_ATTEMPT_OVERHEAD_S = 300`. `check_timeout_s` currently clamps a value into that tuple, and `attempt_budget_s` for `check` computes `clamped_timeout * (len(commands) + 1) + 300`. The launcher watchdog enforces the attempt clock at dispatch with `BUDGET_STOP_GRACE_S = 15`, and the final event drain is bounded by `BUDGET_STOP_DRAIN_S = 2.0`.

For `run-command` the draft decision is:

- The field is required; the runner default of 1800 is **not** silently substituted. If the owner later chooses an implicit default, that is N4.
- Values outside 10..7200 are refused as `budget_timeout_s_invalid`, not clamped. This keeps the reviewed allowlist entry and the executed timeout identical.
- The matched policy's `max_timeout_s` is a second, stricter ceiling; exceeding it is `budget_exceeds_policy_max`.
- With one command, the attempt wall clock is `timeout_s * (1 + 1) + 300`.

Worked arithmetic: `timeout_s = 300` gives `300 * 2 + 300 = 900` s attempt wall clock; the watchdog may force-finish at about 900 s plus the 15 s teardown grace, after a bounded 2 s drain. At the runner default `1800`, the same formula gives `1800 * 2 + 300 = 3900` s. At the policy maximum `7200`, it gives `14700` s; an owner-approved lower `max_timeout_s` is expected (N4).

## 9 Receipts (reuse of the existing check-result manifest with source kind run-command, plus a consumer note)

The result file stays `attempts/<attempt_id>.result.json` with schema id `orbit.local-worker.check-result/v1`. For `run-command`:

- `source.kind` is `run-command`; `source.project`, `source.repo_rev`, `source.image`, `source.security_profile` and `source.launcher` keep their existing meanings.
- `check_run.checks` has exactly one row with `name=check-1`, the allowlisted command, `exit_code`, `duration_s`, redacted tails, and `log_file`.
- `check_run.conclusion` is `success` only for a single zero exit; otherwise `failure`.
- `patch` remains `null` and the note states that there is no model and no diff.
- `observed.run_id`, `sandbox_id`, `workdir`, `local_base_rev` and `seed_commit` are copied from `DockerCheckRunner.start`.

Consumer note (N1): no external consumer of this manifest is documented on main. Before enrollment, the owner must confirm that consumers tolerate a new `source.kind` value and a one-row `check_run.checks` array, or require a consumer-side change first.

## 10 Failure-mode table (at least 12 rows: condition -> typed refusal or state -> operator action)

| Condition | Typed refusal or state | Operator action |
| --- | --- | --- |
| Transport check fails | `unauthorized` | Renew the coordinator's host access and retry; no body is processed. |
| `kind` absent or not `run-command` | `request_kind_invalid` | Fix the request envelope; use `check` for command lists. |
| `brief` absent or not an object | `brief_not_object` | Resend with a brief object. |
| `attempt_id` present but malformed | `attempt_id_invalid` | Supply 8..64 safe characters or omit to let the handler mint one. |
| Unsupported top-level field | `request_field_not_allowed` | Remove model, tier, mode, or top-level project. |
| `brief.objective` empty | `objective_required` | Add a non-empty objective. |
| `brief.project` malformed | `project_invalid` | Use the enrolled project slug. |
| `brief.project` not configured | `project_not_enrolled` | Owner enrolls the project on this host or coordinator corrects the name. |
| `brief.source_rev` malformed | `source_rev_invalid` | Supply a 40- or 64-hex revision. |
| `brief.purpose` malformed or unapproved | `purpose_invalid` / `purpose_not_allowed` | Use an owner-approved purpose label. |
| `brief.command` empty or too long | `command_invalid` / `command_too_long` | Shorten the command or split the work into a separately approved entry. |
| `brief.commands` present | `command_list_not_allowed` | Use `kind=check` for lists or send one `command`. |
| `brief.security_profile` present | `security_profile_not_allowed` | Remove it; the policy entry chooses the profile. |
| Command tuple not allowlisted | `command_allowlist_denied` | Owner reviews and adds an exact entry; no partial match is attempted. |
| Budget missing or outside 10..7200 | `budget_timeout_s_invalid` | Set an integer in range; do not rely on clamping. |
| Budget above policy ceiling | `budget_exceeds_policy_max` | Lower the request or owner approves a new ceiling. |
| Enable flag missing or false | `run_command_disabled` | Owner enables the kind deliberately. |
| Policy file missing, malformed or unreadable | `policy_unavailable` | Restore the enrolled file and re-run startup checks. |
| Policy has no owner approval | `policy_not_approved` | Owner records approval for the exact file. |
| Docker start or seeding fails | `failed` with descriptive detail | Check Docker, the project path, and the revision; retry. |
| Command exits non-zero | `failed`, `check_run.conclusion=failure` | Inspect the receipt tails and per-attempt log. |
| Watchdog reaches the attempt clock | `failed` with a budget-timeout detail | Reduce the budget or the command; inspect the drain result. |
| Route preview receives this kind | `route_preview_kind_unsupported` | Use dispatch-only validation; preview is model-only. |
| Source_rev launcher hash changed | design test failure | Stop and re-review this design against the changed launcher. |

## 11 Route preview behaviour

At source_rev, `/v1/route` resolves models only. `route_preview` reads the kind the way dispatch does — request top-level first, then `brief.kind`, then `opencode` — and raises for any kind other than `opencode`; the handler maps that `ValueError` to 400. A 200 preview means "this is the model dispatch would pick", not "dispatch would accept this brief"; the docstring explicitly notes that security profile, check commands and project authorization are not previewed.

`run-command` keeps that refusal, with typed name `route_preview_kind_unsupported`. Both `{"kind": "run-command", ...}` and `{"brief": {"kind": "run-command", ...}}` refuse, and a non-string kind also refuses rather than being dropped. No new preview route, model answer, or partial validation is added. A top-level `project` remains a preview-only stand-in and is refused by the run-command request envelope.

## 12 Non-goals

- No launcher.py change, deploy, route, or policy enrollment in this deliverable.
- No new model, tier, prompt, harness, or agent behaviour.
- No widening of `kind=check` command lists and no fix to N3 in this rev.
- No glob, regex, or shell-string allowlist matching.
- No implicit timeout clamp or implicit budget default for `run-command`.
- No patch collection, diff, lease, or `assigned_files` handling.
- No interactive approval flow in the request path; approval is owner-side and out of band.
- No change to container naming, labels, or cleanup semantics for this rev.

## 13 Open questions

- **N1 Manifest consumer note:** Confirm whether any consumer outside this repository reads `orbit.local-worker.check-result/v1`, and whether it tolerates `source.kind=run-command` and a single-row `check_run.checks` array.
- **N2 Per-attempt log-file naming:** The runner currently writes `<attempt_id>.check-1.log`. Decide whether `run-command` keeps that name or gets a distinct suffix; receipts and operators both depend on the answer.
- **N3 Pre-existing kind=check allowlist exposure:** `submit` accepts any non-empty `brief.commands` list today; profiles constrain the container but not the command strings. Confirm whether to address this separately; do not fold the fix into `run-command`.
- **N4 Owner values:** Owner must approve the 2000-char command cap, the purpose enum, `max_timeout_s`, and refuse-vs-clamp. The draft picks 2000, a slug-shaped purpose, a ceiling no greater than 7200, and refuse-never-clamp; all four remain unapproved.

## 14 Owner approval needed before any implementation

Implementation must not start until the owner approves: the enable flag; the enrolled policy file path and approval record; each initial allowlist tuple and its profile; the four N4 values; the N1 consumer answer; the N2 log name; and the decision to leave N3 separate. Re-review is also required if source_rev moves or launcher.py differs from the recorded hash. This document and its two sibling files are the only changes in this rev.

## 15 Test map

`tests/test_r4_run_command_design.py` pins this deliverable without importing launcher.py:

- parses the draft schema and the JSON block in this document;
- validates the positive request and one negative request per field-rule row in section 3;
- asserts the DESIGN-ONLY banner, all 16 section headings, the no-launcher-change statement, the `make_handler._auth` reference, and N1..N4;
- checks that `launcher.py` still matches source_rev via git history, with a recorded content-hash fallback;
- re-implements the key-shaped assignment, literal credential shape, and bare long-hex scanner rules and scans all three files line by line.

The exact local command is `python -m pytest tests/test_r4_run_command_design.py -q`.
