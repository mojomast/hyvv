# hyvv

Worker-host runtime for local agent sandboxes. A hyvv process runs on a PC or SSH host, owns the sandboxes, holds provider keys without ever returning them, and exposes a narrow token-scoped HTTP API on loopback or a Tailscale address only.

**hyvv is a vocabulary, not a dependency on bundlebees.** The bee-themed names (hyvv, bees, beekeeper) are a shared language. Orbit, bundlebees, or any other coordinator can drive a hyvv host by speaking the versioned `/v1` contract.

## How the pieces fit

```
workflow (the method)  -->  bundlebees (orchestration)
Orbit (control surface) -->  bundlebees
bundlebees / any coordinator  -- /v1 contract -->  hyvv (this repo, one host)
hyvv --> sandboxes, usage ledger, optional Phoenix
```

This repository is the fourth layer: the runtime on each machine where work happens. It is not part of Orbit or bundlebees, so a host can be installed on its own.

## Status

The imported runtime is **v0.1.15**. It already routes unprefixed and `go:` models to OpenCode Go, and `deepseek:` or `priority: high` to the official DeepSeek API, with automatic DeepSeek fallback.

The `/v1` contract in `contract/v1` is the source of truth going forward:

- OpenAPI plus JSON Schemas for attempts, artifacts (`hyvv.worker-artifact/1`), errors, and usage records (`bee.usage/v1`)
- `GET /v1/version` returns `api_version` (`v1`, the contract version) and `version` (the hyvv / launcher release, currently `0.1.15`), plus the capabilities this host supports (`priority_routing`, `go_fallback`). `artifact_fetch` and `leases` are advertised only once those routes ship
- Semver: `/v1` only ever gets additive changes. A breaking change is `/v2`, served alongside `/v1` for one release
- Conformance tests, including artifact acceptance criteria `AC-001`–`AC-010`

The project-state dispatch gate from workflow `docs/18-parking-projects.md` is implemented in the `/v1` reference only, not in launcher 0.1.15. `POST /v1/attempts` with a `project` reads that project's `.workflow/project-state.json` and refuses parked or archived projects (409 `project_parked` / `project_archived`), unless an override flag plus a reason is given. The override is persisted and attributed to the authenticated caller. It also requires `project_state_generation` (409 `project_state_changed`), fails closed on invalid files, and checks `snapshot_path` containment. See [`contract/v1/README.md`](contract/v1/README.md#project-state-dispatch-gate-reference-only).

Nothing below the first row ships in the launcher yet. `GET /v1/attempts/{attempt_id}/artifact` is implemented and tested only in the `/v1` reference (`contract/v1/reference.py`); `/v1/leases` is specified but unimplemented; the project-state gate (parking) is reference-only. Coordinators should not read raw result files or runtime paths; they should use the artifact endpoint once the launcher ships it.

<!-- status:begin (generated from contract/v1/STATUS.json; python -m contract.v1.status --write) -->
| Status | Endpoints | Features |
| --- | --- | --- |
| **Implemented in launcher 0.1.15** (`launcher-0.1.15`) | `GET /v1/version`, `GET /v1/health`, `GET /v1/attempts`, `POST /v1/attempts`, `GET /v1/attempts/{attempt_id}`, `GET /v1/attempts/{attempt_id}/events`, `GET /v1/attempts/{attempt_id}/result`, `POST /v1/attempts/{attempt_id}/review`, `POST /v1/attempts/{attempt_id}/cancel`, `GET /v1/projects`, `GET /v1/containers`, `POST /v1/config` | host bearer token on every route; priority_routing (deepseek: or priority high to DeepSeek official); go_fallback (automatic DeepSeek fallback on Go limits); GET /v1/version capabilities list |
| **Implemented in the /v1 reference only** (`reference`) | `GET /v1/attempts/{attempt_id}/artifact` | artifact_fetch (hyvv.worker-artifact/1); per-coordinator keys plus per-attempt artifact capability; result_sha256 pinned on first read; owner-only result files (POSIX 0600, Windows owner-only ACL); bee.usage/v1 records in the artifact payload; project-state dispatch gate (409 project_parked / project_archived / project_state_changed, override plus reason, snapshot_path containment) |
| **Specified, not implemented** (`specified`) | `GET /v1/leases`, `POST /v1/leases`, `DELETE /v1/leases/{lease_id}`, `POST /v1/leases/{lease_id}/renew` | bee-slot leases (capability leases, 429 capacity_exhausted) |
| **Proposed** (`proposed`) | — | coordinator-signed briefs (HMAC over the canonical brief with the per-coordinator key, verified at settle) |
<!-- status:end -->

## Install

hyvv is a Python package installed with **uv**. Signed releases (checksums, Sigstore, GitHub provenance, PyPI trusted publishing) are produced from tags by `.github/workflows/release.yml`. It runs as the logged-in user by default; a system service is opt-in.

```bash
uv tool install hyvv
# or, from a checkout:
uv sync
uv run hyvv version
```

Windows is first. From a checkout:

```powershell
.\scripts\install.ps1                 # copy into %USERPROFILE%\hyvv
.\scripts\install.ps1 -Prefix D:\hyvv
.\scripts\install.ps1 -Update         # wait until the host is idle, then swap
```

`-Update` replaces the old version-specific idle-swap waiters: it waits for zero running or queued attempts, copies the new `launcher.py`, health-checks, and rolls back on failure. It never overwrites `config.json`, `*.token`, or `*.key`, and it never prints those values.

Then edit `%USERPROFILE%\.orbit-launcher\config.json` (seeded from `config.example.json` on a first install), run `.\scripts\new-token.ps1`, and `.\scripts\start.ps1`. The data directory name `.orbit-launcher` is kept so existing hosts keep working.

`config.example.json` binds `127.0.0.1`. To accept coordinators on a tailnet, set `bind` to the host's Tailscale address. Binding `0.0.0.0` is refused.

On Linux and macOS, `./scripts/install.sh [--dry-run] [--mode user|system] [--prefix DIR] [--version VER]` installs into a versioned uv venv as the logged-in user (`--mode system` copies files only in 0.1.15; service units come later).

Releases keep PyPI and GitHub assets apart: `uv build` writes only the sdist and wheel to `dist/` (published to PyPI), and `scripts/build_release_assets.py` writes `install.sh`, `install.ps1` and `checksums.txt` to `release-assets/`, which `release.yml` signs and uploads to the GitHub Release. CI runs the same build on every PR without tagging; `tests/test_release_metadata.py` checks that pyproject, the runtime version and the installer examples agree.

Provider keys belong in the environment or in a user-only file, never in the repo or in `config.json`:

| Route | Environment | Default file under the data dir |
| --- | --- | --- |
| OpenCode Go | `ORBIT_WORKER_GO_KEY` | `go.key` |
| Official DeepSeek | `DEEPSEEK_API_KEY` | `deepseek.key` |

The API token is created by `scripts/new-token.ps1` and is not printed.

## Contract

See [`contract/v1/README.md`](contract/v1/README.md) and [`contract/v1/SEMVER.md`](contract/v1/SEMVER.md).

### Run the contract tests (one command)

```bash
uv run python -m contract            # or: scripts/contract-test.sh  /  scripts/contract-test.ps1
```

By default this starts an in-process hyvv (`launcher.py` with its scripted fake backend, no sandboxes or keys) on a free loopback port and runs every test in `contract/v1/tests`: the schema checks, the artifact acceptance criteria `AC-001`–`AC-009` against `reference.py`, and the HTTP conformance suite `test_http_v1.py` for the implemented `/v1` endpoints (auth, health, projects, containers, attempt listing against `schemas/attempt.json`, rejected writes, and a create → events → result → review lifecycle).

To check a running host instead, give its base URL and a token file:

```bash
HYVV_BASE_URL=http://127.0.0.1:47821 HYVV_TOKEN_FILE=path/to/orbit-api.token uv run python -m contract
# or: uv run python -m contract --base-url URL --token-file PATH
```

Live runs are read-only: they never create, review or cancel attempts, and the only POSTs are requests the host must reject (400/403/404). Add `--writes` (or `HYVV_CONTRACT_WRITES=1`) to also create one small attempt. The URL and token are never printed.

Orbit and bundlebees should pin this tree at a tag and run the same command in their CI against the in-process host or a mock.

## Development

```bash
uv run python -m unittest discover -s tests -v   # 82 unit tests
uv run python -m contract -v                     # 46 contract tests
```

The Cua SDK contract test (`tests/test_sdk_contract.py`) is skipped when `cua` is not installed. Run it with `uv run --python 3.12 --with cua==0.5.0 python -m unittest tests.test_sdk_contract -v`.

## License

MIT. This matches [bundlebees](https://github.com/mojomast/bundlebees), [hermes-agent](https://github.com/mojomast/hermes-agent), and [comfyrizz](https://github.com/mojomast/comfyrizz). Some other mojomast repositories use a different license or do not yet have a `LICENSE` file.


A specified (not implemented) `/v1/leases` API makes hyvv the shared dispatch point for the global bee-slot cap (default 12, CI limited to 2). See [`contract/v1/LEASES.md`](contract/v1/LEASES.md). It is specified here and is not implemented in v0.1.15.
