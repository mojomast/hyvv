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
| **Implemented in launcher 0.1.15** (`launcher-0.1.15`) | `GET /v1/health`, `GET /v1/attempts`, `POST /v1/attempts`, `GET /v1/attempts/{attempt_id}`, `GET /v1/attempts/{attempt_id}/events`, `GET /v1/attempts/{attempt_id}/result`, `POST /v1/attempts/{attempt_id}/review`, `POST /v1/attempts/{attempt_id}/cancel`, `GET /v1/projects`, `GET /v1/containers`, `POST /v1/config` | host bearer token on every route; priority_routing (deepseek: or priority high to DeepSeek official); go_fallback (automatic DeepSeek fallback on Go limits) |
| **Implemented in the /v1 reference only** (`reference`) | `GET /v1/attempts/{attempt_id}/artifact` | artifact_fetch (hyvv.worker-artifact/1); per-coordinator keys plus per-attempt artifact capability; result_sha256 pinned on first read; owner-only result files (POSIX 0600, Windows owner-only ACL); bee.usage/v1 records in the artifact payload |
| **Specified, not implemented** (`specified`) | `GET /v1/version`, `GET /v1/leases`, `POST /v1/leases`, `DELETE /v1/leases/{lease_id}`, `POST /v1/leases/{lease_id}/renew` | GET /v1/version capabilities list; bee-slot leases (capability leases, 429 capacity_exhausted) |
| **Proposed** (`proposed`) | — | project_parked refusal (409); coordinator-signed briefs (HMAC over the canonical brief with the per-coordinator key, verified at settle) |
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

## Proposed: parked or archived projects

**Proposed, aligns with workflow docs/18-parking-projects.md (MR.INDIA, in progress).** Not implemented in launcher v0.1.15.

`POST /v1/attempts` would read `.workflow/project-state.json` in the authorized project's repo (`state`, `reason`, `at`, `by`). If `state` is `parked` or `archived`, the host returns **409 `project_parked`** unless the request includes an explicit override plus a reason, both of which are recorded on the attempt. Details: [`proposals/project-parked.md`](proposals/project-parked.md). A pending conformance test lives in `tests/test_project_parked_proposed.py`.

## Specified, not implemented: bee-slot leases

hyvv is the shared dispatch point for the global bee cap (default 12, CI limited to 2, optional per-coordinator caps). Coordinators acquire, renew and release leases at `/v1/leases`. A cap hit is **429 `capacity_exhausted`** with `Retry-After`. See [`LEASES.md`](LEASES.md). Schemas are versioned under `schemas/lease*.json`. Orbit and bundlebees are clients; MR.INDIA writes those adapters and their contract tests.
