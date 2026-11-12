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
- `GET /v1/version` returns `api_version` and capabilities: `artifact_fetch`, `priority_routing`, `go_fallback`, and planned `leases`
- Semver: `/v1` only ever gets additive changes. A breaking change is `/v2`, served alongside `/v1` for one release
- Conformance tests, including artifact acceptance criteria `AC-001`–`AC-009`

A proposed additive check, **not yet planned**, would refuse `POST /v1/attempts` for projects whose `.workflow/project-state.json` is `parked` or `archived` (409 `project_parked`) unless the caller supplies an explicit override plus a reason. It is marked “proposed, aligns with workflow docs/18-parking-projects.md (MR.INDIA, in progress).” See [`contract/v1/proposals/project-parked.md`](contract/v1/proposals/project-parked.md).

`GET /v1/version` and `GET /v1/attempts/{attempt_id}/artifact` are **specified and tested against a reference implementation, but not yet implemented in launcher v0.1.15**. Coordinators should not read raw result files or runtime paths; they should use the artifact endpoint once it ships.

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

Linux / SSH-host install is a stub: `./scripts/install.sh` checks a few dependencies and exits 2 until that installer ships.

Provider keys belong in the environment or in a user-only file, never in the repo or in `config.json`:

| Route | Environment | Default file under the data dir |
| --- | --- | --- |
| OpenCode Go | `ORBIT_WORKER_GO_KEY` | `go.key` |
| Official DeepSeek | `DEEPSEEK_API_KEY` | `deepseek.key` |

The API token is created by `scripts/new-token.ps1` and is not printed.

## Contract

See [`contract/v1/README.md`](contract/v1/README.md) and [`contract/v1/SEMVER.md`](contract/v1/SEMVER.md).

Orbit and bundlebees should pin this tree at a tag and run `python -m unittest discover -s contract/v1/tests -v` against a mock.

## Development

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s contract/v1/tests -v
```

The Cua SDK contract test (`tests/test_sdk_contract.py`) is skipped when `cua` is not installed. Run it with `uv run --python 3.12 --with cua==0.5.0 python -m unittest tests.test_sdk_contract -v`.

## License

MIT. This matches [bundlebees](https://github.com/mojomast/bundlebees), [hermes-agent](https://github.com/mojomast/hermes-agent), and [comfyrizz](https://github.com/mojomast/comfyrizz). Some other mojomast repositories use a different license or do not yet have a `LICENSE` file.


A planned `/v1/leases` API makes hyvv the shared dispatch point for the global bee-slot cap (default 12, CI limited to 2). See [`contract/v1/LEASES.md`](contract/v1/LEASES.md). It is specified here and is not implemented in v0.1.15.
