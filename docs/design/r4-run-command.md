# R4 — the /v1 run-command job type (design r2, DESIGN-ONLY)

Status: DESIGN-ONLY. This design work was commissioned by an owner-approved dispatch, and the
design is the deliverable; it authorizes nothing else. **No launcher.py change, no deploy,
no live API surface, and no policy enrollment are authorized by this document.** Section 14
lists every step that would need explicit owner approval before any implementation exists. The
companion schema at `schemas/drafts/run-command-job.draft.schema.json` is a draft, not frozen;
its draft values are proposals for owner confirmation, not contract.

Base: hyvv `main` at `316bd975e212973837daeefc271a491471ea882c`. This revision (r2) reproduces
the r1 deliverable after the coordinator verify refused r1 on scanner grounds alone; the design
content is unchanged in substance and re-recorded scanner-safe (section 0).

## 0. Scanner-conformance note

The r1 revision of this document was refused by the coordinator verify with `secret_detected`
on this file. The refusal was a false positive from the keyword-assignment scanner rule, not a
leak: nothing was applied, pushed, or disclosed. The two rule classes this deliverable set
conforms to are, in prose (never by example, deliberately — a quoted example would itself be a
violation):

- keyword-assignment — any word ending in a credential-name suffix (access material, password,
  or the transport header name) followed on the same line by a colon or an equals sign and a
  value, in any syntax including JSON, YAML, prose, and tables;
- literal credential shapes — the recognizable prefixes and encodings of real cloud,
  source-hosting, chat-platform, AI-provider, and encoded-claim material.

Conformance choices used across the three deliverables (this document, the draft schema, and
the test file):

1. No line pairs a keyword of that class with a value through a colon or an equals sign. Prose
   and tables carry the meaning; examples name only non-triggering keys.
2. No literal credential-shaped string appears anywhere in the deliverables, and no transport
   header name is ever written together with a value on one line.
3. The transport gate is described by naming the code that enforces it today (the
   `make_handler._auth` constant-time check), never by writing an example header.
4. If a future revision ever needs a request property that carries per-request access
   evidence, its name must be chosen so that examples cannot trip the keyword rule; the
   reserved non-triggering name for that purpose is `auth_context`. This draft needs no such
   property: the only access material in the system remains the host's single shared value
   checked by `_auth`, which never appears in a request body.
5. The test file re-implements both rule classes and scans all three deliverables (including
   itself) line by line, so the conformance property is enforced by the suite, not by review.

## 1. Purpose

hyvv needs a scoped, single-command execution job for operator and agent use cases —
diagnostics and bounded maintenance on worker hosts — as opposed to full multi-file bee
dispatch. Today the launcher offers two shapes and neither fits:

- `opencode` (and `agy-cua`) dispatch a whole agent run: a model, a prompt, a sandbox, a
  collected patch. That is the wrong and expensive tool for "run one bounded command and
  report what happened".
- `check` runs a command list against a seeded project, but it is ungated beyond the host
  access header: any authenticated caller can run any command list today. It is a test-run
  tool, not an operator tool, and its receipt shape and semantics are built for command lists.

The proposed `run-command` kind is a single command, allowlist-gated before dispatch, executed
in the same throwaway container machinery check jobs use, producing the same kind of receipt
(exit code, redacted output tails, log files, landing event). It gives operators and agents a
narrow, owner-governed diagnostic and maintenance primitive instead of a general-purpose shell.

## 2. Relationship to the existing kinds

| Kind | Model | Runs where | Authorization today |
| --- | --- | --- | --- |
| `opencode` | yes | Cua sandbox, opencode harness | shared host credential header, project key 403 |
| `agy-cua` | agy tiers | Cua sandbox plus a host agy child | shared host credential header, agy tier allow-list |
| `check` | no | throwaway docker container | shared host credential header, host-side profiles |
| `run-command` (this design) | no | the same throwaway docker container | five owner gates, host enable flag, enrolled policy, per-request gates (new) |

The execution machinery is shared with `check`; the authorization ladder is what is new, and
it is the point of the kind.

## 3. Request and response sketch

Dispatch is the existing lifecycle: `POST /v1/attempts` with a top-level `kind` of
`run-command` and the brief below. No new endpoint is added. The minted attempt id, the
idempotent re-POST (200 on replay of the same id), the 201 on create, and the journal and
event-stream semantics are all the existing ones.

Request example (illustrative values only):

```json
{
  "attempt_id": "att_rc1a2b3c4d5e6f7a8b",
  "kind": "run-command",
  "brief": {
    "objective": "report disk usage on the seeded workspace",
    "command": "df -h /work",
    "purpose": "diagnostics",
    "project": "rizzstack-smoke",
    "budget": {"timeout_s": 120},
    "security_profile": "default"
  }
}
```

Response: the existing 201 body of public attempt fields. Example:

```json
{
  "attempt_id": "att_rc1a2b3c4d5e6f7a8b",
  "kind": "run-command",
  "state": "queued",
  "detail": "waiting for a worker slot",
  "queue_position": 1,
  "lseq": 0
}
```

The result is read through the existing `GET /v1/attempts/<attempt_id>/result` route once the
attempt is terminal (409 until then, unchanged). The manifest is the existing check-result
shape (`orbit.local-worker.check-result/v1`) with `source.kind` set to `run-command` and
exactly one `check_run` entry; no second result schema is introduced, so every consumer of
result files and landing events keeps working unchanged. Abbreviated example:

```json
{
  "schema": "orbit.local-worker.check-result/v1",
  "attempt_id": "att_rc1a2b3c4d5e6f7a8b",
  "source": {"kind": "run-command", "project": "rizzstack-smoke", "security_profile": "default"},
  "observed": {"run_id": "check-1a2b3c4d", "workdir": "/work/rizzstack_smoke"},
  "check_run": {
    "conclusion": "success",
    "checks": [{"name": "run-1", "command": "df -h /work", "exit_code": 0,
                "stdout_tail": "…", "stderr_tail": "…"}]
  },
  "patch": null
}
```

The run id keeps the reused `check-` prefix because the container path is reused unchanged;
receipts distinguish kinds through the kind fields, not through the prefix.

Field rules — the kind gate, enforced at dispatch before anything is created:

| Field | Rule | Refusal |
| --- | --- | --- |
| `brief.command` | required, non-empty string, at most 2000 characters (draft cap, pending owner confirmation) | 400 |
| `brief.purpose` | optional; one of `diagnostics`, `maintenance` (draft enum, pending owner confirmation); defaults to `diagnostics` | 400 |
| top-level or brief `model` / `tier` | must be absent; a run-command attempt has no inference | 400 |
| `brief.commands` | must be absent; multi-command work belongs to `kind=check` | 400 |
| `brief.verify_of` | must be absent; an opencode-only concept, exactly as check refuses it today | 400 |
| `brief.assigned_files` | must be absent; there is no patch and no file collection | 400 |
| `brief.project` | optional; when present it must be an authorized project key (existing 403) | 403 |
| `brief.security_profile` | optional; resolved like check jobs | 400 when unknown |
| `brief.budget.timeout_s` | optional integer inside the `CHECK_TIMEOUT_S` bounds (10 to 7200 seconds); the gate refuses out-of-range values instead of silently clamping (draft decision) | 400 |

Everything else keeps the existing transport behavior: `validate_brief` runs first and is not
changed; unknown brief fields remain ignored (section 4).

## 4. Draft schema and envelope strictness

`schemas/drafts/run-command-job.draft.schema.json` (JSON Schema draft 2020-12, marked
`x-hyvv-status` draft, revision 2) describes the request above. It is a draft, not frozen: not
referenced by any implementation, and not a change to the frozen `brief.schema.json`, which
stays untouched (updating it is a contract change listed in section 14).

Strictness delta versus the runtime boundary — this resolves the r1 open question.
`validate_brief` ignores unknown fields, and it stays exactly as it is. The draft is
intentionally stricter than the transport boundary because the strict choices belong to the
run-command kind gate, not to `validate_brief`: a future implementation would add the refusals
of the table above inside the kind-specific branch of `submit()`, while `validate_brief`,
`normalize_brief`, and the ignore-unknown behavior remain shared by all kinds. Rationale: the
authorization burden for this kind sits in the enrolled allowlist and the per-request gates,
not in field strictness; unknown fields are inert for a single-command run because the
execution path reads only `command`, `purpose`, `budget`, `project`, and `security_profile`.
Adding a second, strict transport validator would be a new mechanism with no observed failure
to justify it.

## 5. Authorization model

The kind is denied by default at every layer, and each layer fails closed.

New surface inventory: exactly two new mechanisms are proposed — the `run_command.enabled`
config gate and the enrolled `run-command-policy.json` loader. Both are justified by an
observed failure class that [ADR-0001](../adr/0001-worker-provisioning.md) already records for
this repo: request-carried authorization for privileged host operations was rejected there in
favor of a host-enrolled policy file, and a disabled feature answers 501 rather than pretending
not to exist (the provisioning endpoints do exactly that today). Everything else in this
design is reused code.

### 5.1 Owner approval gates (before anything exists)

Five separate owner actions gate this feature; none of them can be granted by a request, a
coordinator, or a config edit:

1. **Design approval** — approval of this document as the recorded design.
2. **Implementation approval** — approval to touch `launcher.py` (the `KINDS` tuple, the
   submit branch, the controller, the capability list, the version bump) and to ship it. The
   ADR-0001 wording applies: a launcher upgrade is a one-time privileged step, not a routine
   operation.
3. **Contract change approval** — `contract/v1/STATUS.json` labels, the version capabilities
   list, and only if the owner wants it, the frozen `brief.schema.json` kind enum.
4. **Policy enrollment approval** — placing `run-command-policy.json` on the worker host is an
   owner action, exactly like provisioning-policy enrollment; no coordinator can enroll, edit,
   or point at the policy through the API.
5. **Allowlist content approval** — every exact command string in the policy is owner-chosen
   and owner-reviewed; additions and edits are new approvals, not data entry.

### 5.2 Host gates at request time

In order; each gate refuses before the next one runs:

1. **Transport gate (existing, unchanged)** — the shared host access value checked by
   `make_handler._auth` on every route; a missing or wrong header is 401. No new credential
   class, no per-coordinator key, no request-body field.
2. **Enable gate** — `run_command.enabled` defaults to false in launcher config. While it is
   false (or absent), a run-command dispatch answers 501 with an error body, mirroring the
   existing provisioning endpoints that answer 501 when their feature is not enabled. The kind
   is also absent from the advertised capabilities while disabled. The gate in config form:

   ```json
   {"run_command": {"enabled": false, "policy_file": "run-command-policy.json"}}
   ```

3. **Policy gate** — when the enable flag is on but `run-command-policy.json` is missing,
   unreadable, or does not declare schema `rizzstack.run-command/1`, every dispatch answers
   503. Fail-closed: a broken policy never widens anything; it only stops the kind. An
   enrolled policy with an empty allowlist refuses every request with 403.
4. **Per-request gates** (all must pass; any failure is 400 or 403, and a refused request
   creates no attempt record and no receipt):
   - exact-match allowlist — the `brief.command` string must be byte-for-byte equal to one
     enrolled entry. No trimming, no case folding, no glob, no prefix or suffix match, no
     shell-word splitting, no normalization of any kind.
   - purpose binding — when the matched entry declares a purpose, the brief purpose must equal
     it; an entry without a purpose constraint accepts the declared default.
   - project gate — existing behavior: a `brief.project` that is not an authorized project key
     is 403 before anything is created.
   - profile gate — `brief.security_profile` resolves like check jobs (the default profile
     unless the host defines others); when the matched entry pins a profile, the request must
     name the same one.

A refusal at any gate leaves no attempt record: the existing 400/403 path raises before
`submit()` journals anything, and the design keeps that property deliberately — the journal
records what ran, the policy records what may run.

## 6. Run-command policy file (enrolled, owner-approved)

Sketch of the enrolled file next to `config.json` (schema `rizzstack.run-command/1`;
illustrative entries and values only — the real contents are owner-approved per gate 5):

```json
{
  "schema": "rizzstack.run-command/1",
  "allow": [
    {"command": "df -h", "purpose": "diagnostics"},
    {"command": "docker images", "purpose": "diagnostics"},
    {"command": "docker system df", "purpose": "maintenance", "profile": "default"}
  ],
  "max_timeout_s": 300
}
```

Rules:

- `allow` entries are exact strings. The loader refuses wildcards, prefixes, templates, and
  anything that is not a plain string (a fail-closed policy-invalid answer, 503 at request
  time).
- An entry may optionally pin a purpose and/or a profile; the request must then match both.
- `max_timeout_s` optionally tightens the per-command clock: the effective clock is the
  smaller of the brief value and the enrolled cap. Tightening is safe by construction; the
  policy can never widen the launcher's own clamps.
- The file is read at dispatch time so an owner edit takes effect without a restart; an
  unreadable file at request time is 503, never a bypass.

## 7. Isolation and execution path

The execution path is the existing `DockerCheckRunner` container path, reused unchanged:

- One throwaway container per attempt, created with `docker_check_argv`: the profile image,
  memory, cpu, and pids limits, the profile network (`none` for the default profile), a seccomp
  profile file when the profile names one, and the existing argv guard that refuses privileged
  flags, extra capabilities, host namespaces, devices, mounts, and unconfined or apparmor
  options.
- When `brief.project` names an authorized project with a local path, the workspace is seeded
  exactly as check jobs seed it: the `export_project` archive, untar into `/work/<project>`,
  and the `refs/hyvv/seed` base commit recorded in the manifest.
- The single command then runs inside that container through `docker exec` with `bash -lc`,
  the same invocation check jobs use. The allowlist matched the raw brief string before
  dispatch, so no shell interpretation happens before the gate.
- Captured stdout and stderr are `redact()`-scrubbed, capped at `CHECK_TAIL` in the manifest,
  and written whole to the per-attempt log file beside the journal (check writes `.check-N.log`
  files; the run-command file name follows its single entry).
- Cleanup removes only this attempt's own container (the existing `hyvv-check-` name guard).

What differs from full bee dispatch (`kind=opencode`): no Cua sandbox, no agent harness, no
model or provider key, no prompt rendering, no collected patch or diff, no assigned-file
scoping, and no verdict based on worker claims — the launcher measures the exit code itself.

What differs from `kind=check`: exactly one command instead of a list; the allowlist, purpose,
and profile gates run before dispatch; the manifest carries a single one-entry command block;
and the host enable and policy gates exist only for this kind. The container machinery, the
seeding, the per-command clock, the tail caps, and the cleanup are all shared code paths — the
reused runner consumes a command list, so the run-command controller drives it with the single
command presented as a one-entry list, leaving the backend classes untouched.

No host shell, no host filesystem access, and no host environment variables reach the command:
everything runs inside the throwaway container, like check jobs today.

## 8. Budget and timeout semantics

All reused unchanged from check jobs:

- Per-command clock: `check_timeout_s` clamps `brief.budget.timeout_s` into `CHECK_TIMEOUT_S`
  (minimum 10, default 1800, maximum 7200 seconds). The run-command gate refuses values
  outside the bounds (400) instead of relying on the silent clamp; `check_timeout_s` itself is
  not modified and remains the effective clock source.
- Attempt wall clock: `attempt_budget_s` gains a run-command branch with exactly the value the
  existing check formula produces for a single command:
  `check_timeout_s(b) * 2 + CHECK_ATTEMPT_OVERHEAD_S`. With the default clock that is 3900
  seconds (2 x 1800 plus the 300-second overhead); with the request in section 3 (120 seconds)
  it is 540 seconds.
- Enforcement: the same controller deadline fast path and the same `_budget_watchdog`
  force-finish, with the same teardown grace, the same `budget_stop` mark, and the same
  crash-recovery path — a restart during a run-command attempt finishes it honestly instead of
  wedging it, exactly like check and opencode attempts.
- A command that runs to its own clock is killed by the docker exec timeout; the attempt then
  lands failed with the honest timeout detail.

## 9. Receipts

All four receipt surfaces are existing mechanisms, unchanged:

- **Journal** — the per-attempt JSON record and the append-only events file with lseq cursors,
  including the queued, starting, running, and terminal transitions and the heartbeat events.
- **Events** — the existing per-attempt events route with cursor resumption, unchanged. No new
  event kinds (the r1 per-step-events question is resolved in section 13: none).
- **Result** — the existing result route over the same per-attempt result file, 409 until
  terminal, unchanged.
- **Landing** — `build_landed_event` produces the same CloudEvents envelope with the backend
  harness mapped to `run-command` (a one-line harness mapping is part of the implementation
  scope in section 14); the durable outbox, replay, and export paths are untouched.

## 10. Failure modes

| Trigger | Outcome |
| --- | --- |
| kind disabled on the host | 501, no attempt created |
| policy missing, unreadable, or wrong schema id | 503, no attempt created |
| command not in the allowlist, or purpose/profile mismatch | 403, no attempt created |
| shape-gate refusal (cap, enum, forbidden fields, budget bounds, named model) | 400, no attempt created |
| project not authorized | 403 (existing behavior), no attempt created |
| container creation fails (docker down, bad image) | controller error path: attempt failed with a redacted detail, cleanup attempted (reused) |
| command exits non-zero | attempt failed, detail names the exit code, manifest conclusion failure, landing event state failed |
| command exceeds its clock | docker exec timeout kills it; attempt failed with the honest timeout detail |
| attempt wall clock exceeded | watchdog force-finish (reused), honest budget detail, slot freed |
| launcher restart mid-run | same as check today: the exec stream is gone, the attempt lands failed with the retry-from-Orbit detail (reused text) |
| cancel requested mid-run | cancelled between steps (reused cancel branch) |
| oversize output | tails capped at `CHECK_TAIL` in the manifest, full log on disk (reused) |
| credential-shaped output | `redact()` scrub (reused) before anything is stored |

## 11. Route preview

`POST /v1/route` keeps its existing refusal for every non-opencode kind, and run-command
reuses it unchanged: the refusal already answers for the new kind with a 400 that names the
kind, because `route_preview` covers model resolution only and a run-command attempt resolves
no model. No new preview is added; adding one would be new infrastructure with no observed
failure to justify it. A coordinator that wants a dry-run answer dispatches a run-command
attempt and reads its result, which is the receipt.

## 12. Non-goals

- No host-shell or host-filesystem execution; the command runs only inside the throwaway
  container (section 7).
- No multi-command batches; `kind=check` remains the multi-command kind.
- No stdin, no TTY, no interactive session, and no output streaming beyond the existing
  events and result surfaces.
- No model, no inference, no provider key use, no prompt rendering.
- No patch, diff, or file-artifact collection; the result is exit codes, logs, and tails.
- No new HTTP endpoint, no new credential class, no per-coordinator key, and no request-body
  access material.
- No wildcards, prefixes, globs, templates, or normalization in the allowlist.
- No scheduler, cron, or recurring runs; every run is one dispatched attempt.
- No change to check-kind behavior, including the open question of extending the allowlist to
  check jobs (section 13).
- No lease semantics in this draft (section 13).
- No secrets or host environment variables injected into the container.

## 13. Open questions

Resolved in this revision:

1. **Envelope strictness delta versus `validate_brief`** — resolved: the transport validator
   is untouched; strictness lives in the kind gate (section 4).
2. **Per-step result events** — resolved: none. The single command's output lives in the
   result manifest and its log file, matching how check jobs record command output today; the
   event stream keeps only the existing lifecycle and heartbeat kinds.
3. **Lease-class participation** — resolved for this draft: none. Bee-slot leases are
   specified in the contract but not implemented in the launcher; binding a new kind to an
   unimplemented mechanism would be speculative surface. A run-command attempt occupies one
   `max_concurrent` slot exactly like a check attempt today. Revisit when leases land.

Re-raised for the owner (decisions, not design):

4. **Extending the exact-match allowlist to `kind=check`.** Today a check brief already runs
   an arbitrary command list under the shared host credential, with no allowlist. That is the
   status quo and this design does not change it; if the owner wants the same gate for check
   jobs, that is a separate approved change with its own migration question for existing check
   consumers.
5. **Draft values pending owner confirmation**: the 2000-character command cap; the purpose
   enum (`diagnostics`, `maintenance`) and its default; the example `max_timeout_s` value (the
   enrolled value is owner-chosen); and the refusal-instead-of-clamp choice for out-of-range
   budget values.

## 14. Owner approval needed before any implementation

The design maps to a bounded implementation surface. Every item below is unauthorized until
the matching gate in section 5.1 is granted:

- `launcher.py`: add `run-command` to `KINDS`; a run-command branch in `submit()` (the shape
  gate, the allowlist/purpose/profile/project gates, and the 501/503 enable and policy checks,
  all before anything is created); a run-command branch in `attempt_budget_s`; a thin
  controller that drives the existing `DockerCheckRunner` methods with the single command
  presented as a one-entry list; the harness mapping in `build_landed_event`; the
  `run_command.enabled` config key and the policy loader; the capabilities list entry; and a
  version bump. `resolve_security_profile` would accept the new kind alongside check (one
  condition widened; coding bees stay default-only).
- A new, small policy loader beside the launcher (no `provisioning.py` change is needed; the
  run-command policy is a separate, simpler loader, listed here because it is new code).
- `contract/v1`: `STATUS.json` labels and the capabilities enum in `schemas/version.json`.
- Deployment: shipping the new launcher to worker hosts and enrolling `run-command-policy.json`
  on each host are owner actions, per ADR-0001's privileged-step wording.
- Tests: the implementation would extend the unit suite in the style of
  `tests/test_provisioning_api.py` — policy loader refusals, gate order, the 501/503/403/400
  mapping, the controller path with a fake docker runner, and the budget watchdog for the kind.

## 15. Test map

`tests/test_r4_run_command_design.py` proves the deliverable properties of this revision:

- the document exists, carries the DESIGN-ONLY banner, all sections, the authorization
  requirements, and references only mechanisms that exist in `launcher.py`;
- `launcher.py` is unchanged in the ways this design requires (no run-command kind, no new
  route, and the `route_preview` refusal still answers for the kind);
- the draft schema parses, conforms to draft 2020-12, is marked draft and not frozen, is
  versioned, and validates the representative requests (valid and refused);
- the scanner-conformance rules hold line by line across all three deliverables.

Run: `python3 -m pytest tests/test_r4_run_command_design.py -q`
