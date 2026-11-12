# hyvv `/v1` contract

This directory is the source of truth for the worker-host HTTP API. Orbit and bundlebees should pin this tree at a tag and run the conformance tests in CI against a mock.

hyvv is a vocabulary, not a dependency on bundlebees. Any coordinator that speaks this contract can use a hyvv host.

## Layout

| Path | What it is |
| --- | --- |
| `openapi.yaml` | The `/v1` API, including planned endpoints |
| `schemas/` | JSON Schemas for attempts, artifacts, errors, usage (`bee.usage/v1`), and `GET /v1/version` |
| `error_codes.json` | Artifact-endpoint status codes, reused verbatim in the conformance tests |
| `SEMVER.md` | `/v1` is additive-only; a break means `/v2` served alongside `/v1` for one release |
| `reference.py` | In-memory reference for `GET /v1/attempts/{attempt_id}/artifact` |
| `tests/` | Conformance tests, including OA1 acceptance criteria `AC-001`–`AC-009` |
| `tests/test_http_v1.py` | HTTP conformance for the implemented `/v1` endpoints; in-process hyvv by default, a live host with `HYVV_BASE_URL` + `HYVV_TOKEN_FILE` (read-only unless `--writes`) |
| `../__main__.py` | One-command runner: `uv run python -m contract` |
| `proposals/` | Proposed `/v1` additions that are not yet planned work |
| `schemas/project-state.json` | Proposed `.workflow/project-state.json` shape |

## Implementation status at v0.1.15

Already implemented in `launcher.py`:

- `GET /v1/health`, `GET /v1/attempts`, `GET /v1/attempts/{id}`, `GET /v1/attempts/{id}/events`, `GET /v1/attempts/{id}/result` (legacy full manifest; see below)
- `GET /v1/projects`, `GET /v1/containers`
- `POST /v1/attempts`, `POST /v1/attempts/{id}/review`, `POST /v1/attempts/{id}/cancel`, `POST /v1/config`
- Auth: `Authorization: Bearer`. The process binds loopback and/or a Tailscale address only.
- Routing capabilities that the contract will advertise as `priority_routing` and `go_fallback`.

Planned (specified here, not in launcher v0.1.15):

- `GET /v1/version` (`api_version` plus capabilities)
- `GET /v1/attempts/{attempt_id}/artifact` (`hyvv.worker-artifact/1`), the coordinator-only artifact fetch, advertised as the `artifact_fetch` capability
- Coordinator-key plus per-attempt capability authorization for that endpoint
- Pinning `result_sha256` on first read, 0600+rename writes, `O_NOFOLLOW` plus realpath reads
- Usage records in the artifact payload shaped as `bee.usage/v1`
- `POST/GET/DELETE /v1/leases` (global bee-slot lease system, capability `leases`)

`GET /v1/attempts/{id}/result` is the historical manifest and may contain host and work-dir fields. Coordinators must use the artifact endpoint once it ships, not the raw result or runtime files.

## Artifact endpoint

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

## Planned: bee-slot leases

hyvv is the shared dispatch point for the global bee cap (default 12, CI limited to 2, optional per-coordinator caps). Coordinators acquire, renew and release leases at `/v1/leases`. A cap hit is **429 `capacity_exhausted`** with `Retry-After`. See [`LEASES.md`](LEASES.md). Schemas are versioned under `schemas/lease*.json`. Orbit and bundlebees are clients; MR.INDIA writes those adapters and their contract tests.
