# hyvv `/v1` contract

This directory is the source of truth for the worker-host HTTP API. Orbit and bundlebees should pin this tree at a tag and run the conformance tests in CI against a mock.

hyvv is a vocabulary, not a dependency on bundlebees. Any coordinator that speaks this contract can use a hyvv host.

## Layout

| Path | What it is |
| --- | --- |
| `openapi.yaml` | The `/v1` API; every operation has `x-hyvv-status` |
| `STATUS.json`, `status.py` | Status labels and the README table renderer |
| `schemas/` | JSON Schemas for attempts, artifacts, errors, usage (`bee.usage/v1`), and `GET /v1/version` |
| `error_codes.json` | Artifact-endpoint status codes, reused verbatim in the conformance tests |
| `SEMVER.md` | `/v1` is additive-only; a break means `/v2` served alongside `/v1` for one release |
| `reference.py` | In-memory reference for `GET /v1/attempts/{attempt_id}/artifact` |
| `tests/` | Conformance tests, including OA1 acceptance criteria `AC-001`–`AC-009` |
| `tests/test_http_v1.py` | HTTP conformance for the implemented `/v1` endpoints; in-process hyvv by default, a live host with `HYVV_BASE_URL` + `HYVV_TOKEN_FILE` (read-only unless `--writes`) |
| `../__main__.py` | One-command runner: `uv run python -m contract` |
| `proposals/` | Proposed `/v1` additions that are not yet planned work |
| `schemas/project-state.json` | Proposed `.workflow/project-state.json` shape |

## Implementation status

Four labels, one per endpoint and feature. `STATUS.json` is the source, every `openapi.yaml` operation carries the same `x-hyvv-status`, and `tests/test_status_labels.py` fails if a label is dishonest: a `launcher-0.1.15` route must answer on the in-process launcher, any other route must be absent from it, and every `reference` item must name the reference code and test that prove it.

<!-- status:begin (generated from contract/v1/STATUS.json; python -m contract.v1.status --write) -->
| Status | Endpoints | Features |
| --- | --- | --- |
| **Implemented in launcher 0.1.15** (`launcher-0.1.15`) | `GET /v1/version`, `GET /v1/health`, `GET /v1/attempts`, `POST /v1/attempts`, `GET /v1/attempts/{attempt_id}`, `GET /v1/attempts/{attempt_id}/events`, `GET /v1/attempts/{attempt_id}/result`, `POST /v1/attempts/{attempt_id}/review`, `POST /v1/attempts/{attempt_id}/cancel`, `GET /v1/projects`, `GET /v1/containers`, `POST /v1/config` | host bearer token on every route; priority_routing (deepseek: or priority high to DeepSeek official); go_fallback (automatic DeepSeek fallback on Go limits); GET /v1/version capabilities list |
| **Implemented in the /v1 reference only** (`reference`) | `GET /v1/attempts/{attempt_id}/artifact` | artifact_fetch (hyvv.worker-artifact/1); per-coordinator keys plus per-attempt artifact capability; result_sha256 pinned on first read; owner-only result files (POSIX 0600, Windows owner-only ACL); bee.usage/v1 records in the artifact payload; project-state dispatch gate (409 project_parked / project_archived / project_state_changed, override plus reason, snapshot_path containment) |
| **Specified, not implemented** (`specified`) | `GET /v1/leases`, `POST /v1/leases`, `DELETE /v1/leases/{lease_id}`, `POST /v1/leases/{lease_id}/renew` | bee-slot leases (capability leases, 429 capacity_exhausted) |
| **Proposed** (`proposed`) | — | coordinator-signed briefs (HMAC over the canonical brief with the per-coordinator key, verified at settle) |
<!-- status:end -->

Auth on implemented routes: `Authorization: Bearer` with the per-host token. The process binds loopback and/or a Tailscale address only.

`GET /v1/attempts/{id}/result` is the historical manifest and may contain host and work-dir fields. Coordinators must use the artifact endpoint once it ships, not the raw result or runtime files.

## Artifact endpoint (reference only)

`GET /v1/attempts/{attempt_id}/artifact`

- Auth, two parts. (1) `Authorization: Bearer <coordinator key>`: a per-coordinator key that authenticates a principal; the host stores only key hashes and the coordinator identity always comes from the key, never from a request field. The caller must be the attempt's owner (`403 not_owner`). (2) `X-Hyvv-Capability: <capability>`: a per-attempt capability returned once, only to the creating principal, in the 201 response to `POST /v1/attempts` (`artifact_capability`). The host stores only its SHA-256 and compares in constant time. It is bound to that attempt, scoped to its artifact and result, and can expire (TTL) or be revoked. Missing is `401 capability_required`; wrong or another attempt's is `403 capability_invalid`; `403 capability_expired` and `403 capability_revoked` refuse replay. Peer is loopback or Tailscale.
- Tests: `AC-010a`–`AC-010g` in `tests/test_artifact_ac.py`.
- Response schema: `hyvv.worker-artifact/1`.
- Default size cap: 8 MiB (`HYVV_ARTIFACT_MAX_BYTES`). Oversized results return `413 artifact_oversized` before the body is parsed.
- Guarantees: idempotent, byte-identical reads; `result_sha256` pinned on first read; result files written owner-only via rename (POSIX mode 0600; on Windows the ACL is reset to a single full-control entry for the host user's SID, with inheritance removed, before any data is written; see `privatefs.py`) and read with `O_NOFOLLOW` plus realpath (Windows uses the equivalent no-reparse-point check). List, get, and logs never carry patch text or host paths.

Error codes (HTTP → `code`):

| HTTP | `code` |
| --- | --- |
| 400 | `invalid_attempt_id` |
| 401 | `unauthorized`, `capability_required` |
| 403 | `not_owner`, `capability_invalid`, `capability_expired`, `capability_revoked` |
| 404 | `unknown_attempt`, `result_missing` |
| 409 | `result_pending`, `no_result_for_state`, `artifact_mismatch`, `artifact_changed`, `artifact_unsafe` |
| 413 | `artifact_oversized` |
| 502 | `artifact_malformed` |
| 503 | `worker_disconnected` (retryable) |

## Project-state dispatch gate (reference only)

Implemented in `project_state.py` and `ArtifactStore.create_attempt` (the `/v1` reference). Launcher 0.1.15 does not enforce it, so the launcher test in `tests/test_project_parked_proposed.py` stays pending. The gate follows workflow `docs/18-parking-projects.md` and `tools/project-state/check.py` at mojomast/workflow `2bcf0c5` (`workflow.project-state/v1`). Tests: `tests/test_project_state_gate.py` (`AC_PARK_*`).

When `POST /v1/attempts` names a `project`, hyvv reads `.workflow/project-state.json` from that project's configured repo root. It checks, in order:

1. **Authorized project**, or 403 `project_not_authorized`.
2. **Valid state**, or 400 `invalid_project_state` (fail closed). The file is refused if it is a symlink, `.workflow/` is a symlink, it is over 256 KiB, it is unreadable, it has unknown fields (including the old `changed_at`/`changed_by` names), the transition is not allowed, a resume is not by `human:*`, or the last history entry does not match. `snapshot_path` (required when parked or archived) must be relative, with no empty, `.` or `..` segment, no backslash, and at most 244 characters. No component may be a symlink, the target must be a regular file, and its resolved path must stay inside the repo root. `detail` names the failure: `snapshot_path_invalid`, `snapshot_path_symlink`, `snapshot_path_escape` or `snapshot_missing`. An absent file defaults to `active` at generation 0, and the attempt records `defaulted: true`.
3. **Generation**, or 409 `project_state_changed`. The file's `generation` must equal its history length, and the request's `project_state_generation` (required) must equal the file's. A park that lands between the dispatcher's read and the start is refused this way.
4. **State.** `parked` returns 409 `project_parked`, and `archived` returns 409 `project_archived`. The body carries `project`, `state`, `at`, `by`, `reason`, `generation` and `message`. `project_parked_override: true` with a non-empty `project_parked_override_reason` allows the dispatch; a flag without a reason is 400 `override_reason_required`. The override records the authenticated principal (never the `coordinator` request field), the time, the reason, and the state's `state`/`at`/`by`/`reason`/`generation`. It is stored on the attempt and in the owner-only `dispatch-overrides.json` before the attempt is created.

The gate never cancels in-flight attempts. The checks run on every new dispatch; an idempotent replay of an existing `attempt_id` returns the existing attempt.

Spec difference to flag: docs/18 §7 proposes one code, `project_parked`, for both states (with `state` in the body). This reference returns `project_archived` for archived projects, as Kyle's 5:37 PM request specified. It also returns `project_state_changed` (not `invalid_project_state`) when the file's generation does not equal its history length.

## Specified, not implemented: bee-slot leases

hyvv is the shared dispatch point for the global bee cap (default 12, CI limited to 2, optional per-coordinator caps). Coordinators acquire, renew and release leases at `/v1/leases`. A cap hit is **429 `capacity_exhausted`** with `Retry-After`. See [`LEASES.md`](LEASES.md). Schemas are versioned under `schemas/lease*.json`. Orbit and bundlebees are clients; MR.INDIA writes those adapters and their contract tests.
