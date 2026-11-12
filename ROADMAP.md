# hyvv roadmap

hyvv is the worker-host runtime: it runs AI coding-agent bees in sandboxes on one
machine and exposes the versioned `/v1` HTTP API that coordinators (Orbit,
bundlebees, anything else) call. This roadmap covers how hosts are packaged,
installed, enrolled, updated and managed on Windows, Linux and macOS.

Status: DRAFT, written 2026-11-12, updated the same day with Kyle's decisions
(see below). Design detail lives in `docs/DEPLOYMENT.md`; CI/CD and bees-as-runners
detail lives in `docs/CI.md`.

## Layers

| Layer | Repo | Role |
| --- | --- | --- |
| Method | workflow | How work is planned, verified and recorded |
| Orchestration | bundlebees | Splits work into bees and leases |
| Control surface | Orbit | UI, onboarding, host registry, SSH install |
| Host runtime | **hyvv** | Runs bees on one host behind `/v1` |

Ownership: hyvv (repo, contract, installers, service units, updater) is owned by
Grok Bot. Orbit onboarding (worker-host step, SSH install, version and capability
checks), contract tests in Orbit and bundlebees, and the workflow docs index are
owned by MR.INDIA.

## Principles

1. Agent-agnostic: no Orbit or bundlebees code inside hyvv; they meet at `/v1`.
2. User-level by default, system-level when asked (headless servers).
3. Never listen on a public interface: loopback or the tailnet address only.
4. Secrets live in the OS secret store, never in `config.json`.
5. Every update can be undone: versioned install dirs, health gate, auto-rollback.
6. Every release is verifiable: sha256 checksums, Sigstore signature, GitHub
   build provenance.
7. Observability is part of the contract: the usage record v1 and OTel GenAI
   metrics ship with every host.

## Phase 0: Windows parity and a real installer (now)

Goal: brrrrrrrr runs hyvv from a proper install instead of hand-swapped files.

- [ ] Package hyvv as a Python project (`pyproject.toml`, entry point `hyvv`),
      dependencies pinned with hashes (`uv.lock`); PyPI name `hyvv` reserved.
- [ ] CLI verbs: `install`, `uninstall`, `start`, `stop`, `status`, `doctor`,
      `drain`, `uncordon`, `reload`, `update`, `rollback`, `token rotate`,
      `enroll`, `logs`.
- [ ] Versioned layout: `<data>/versions/<ver>/` (one uv venv each) plus a
      `current` pointer; keep the last 2 versions.
- [ ] Windows service model: per-user Scheduled Task "at log on", hidden,
      restart on failure (Docker Desktop needs the logged-in session).
- [ ] `install.ps1`: pinned version, verifies sha256 and signature before
      running anything, idempotent re-run, `-WhatIf` dry run.
- [ ] Secrets moved out of `config.json` into Windows Credential Manager
      (DPAPI-backed) via `keyring`; config keeps `secret://<name>` references.
- [ ] Drain: `POST /v1/admin/drain` and `hyvv drain` stop new attempts
      (503 + `Retry-After`) and let in-flight attempts finish up to a deadline.
- [ ] Config reload without restart: file watch + `hyvv reload`
      (`POST /v1/admin/reload`) for hot keys; `bind`/`port` still need restart.
- [ ] `hyvv update`: download, verify, drain, swap `current`, restart, health
      gate, auto-rollback. Retires the idle-swap waiter scripts.
- [ ] `doctor` reports WSL2/Docker VM memory and recommends a `.wslconfig` cap;
      it never edits `.wslconfig` or runs `wsl --shutdown` itself.
- [ ] GET `/v1/version` capabilities add `drain`, `reload`, `self_update`.
- [ ] Release pipeline: GitHub Actions builds sdist/wheel, `checksums.txt`,
      cosign keyless bundle, `actions/attest-build-provenance`.

Exit: brrrrrrrr reinstalled with `install.ps1`, one update and one forced
rollback done end-to-end with bees running, no secret in `config.json`.

## Phase C: CI/CD baseline for every repo (runs alongside phase 0)

Repos: hyvv, workflow, bundlebees, Orbit, plugins. Owner: Grok Bot for hyvv and
the shared reusable workflows; MR.INDIA for Orbit, bundlebees contract jobs
and the workflow docs.

- [ ] `permissions: {}` at workflow level, per-job grants; repo default token read-only.
- [ ] All actions pinned to full SHAs (or the workflow lockfile), Dependabot/Renovate bumps, zizmor lint.
- [ ] gitleaks + push protection + host/tailnet grep on every PR and push.
- [ ] CodeQL (language + Actions), dependency review, pip-audit / npm audit.
- [ ] OS matrix on GitHub-hosted runners: ubuntu x64 + arm64, windows, macos arm64.
- [ ] `/v1` contract suite on every PR: hyvv vs mock and vs a real loopback `hyvv serve`; Orbit and bundlebees vs mock pinned to a hyvv tag.
- [ ] Fork PRs: "Require approval for all external contributors"; never on self-hosted runners; no `pull_request_target`.
- [ ] Branch rulesets: PR + required checks, no force-push, no deletion.
- [ ] release-please Release PR per repo (merge = Kyle's yes) and a tag-driven `release.yml` calling one reusable, SHA-pinned workflow in `mojomast/workflow`.
- [ ] Signed releases: sha256 checksums, cosign keyless bundles, GitHub artifact attestations (SLSA v1 Build L3 via reusable workflow), PyPI trusted publishing (OIDC, no stored token).
- [ ] Channels: canary on brrrrrrrr, then beta, then stable, promoted by a PR that edits `channels/*.json`.

Exit: every repo green on the baseline; one hyvv release cut end to end,
verified with `gh attestation verify` and `cosign verify-blob`, canaried on
brrrrrrrr and promoted.

## Phase 1: Linux and macOS together, after Windows

Linux and macOS are built in the same phase, after Windows parity (phase 0). Goal: Orbit can add a Linux SSH host or an Apple-silicon Mac and have it running bees in under 5 minutes.

### Linux via systemd, SSH enroll from Orbit


- [ ] `install.sh` (POSIX sh): pinned version, checksum + signature check,
      installs uv if missing (pinned uv version), never needs root for the
      default mode.
- [ ] Default mode `user`: `systemd --user` unit + `loginctl enable-linger`
      (the one step that needs sudo, or ask the admin), rootless Docker or
      Podman.
- [ ] Opt-in mode `system`: dedicated `hyvv` user, hardened system unit
      (`NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths=`),
      secrets via `LoadCredentialEncrypted` (systemd-creds).
- [ ] Stop semantics: `KillSignal=SIGTERM` means drain, `TimeoutStopSec`
      long enough for the longest attempt; `ExecReload` sends SIGHUP.
- [ ] Sandbox: Cua `auto` (gVisor when installed, else runc) on Docker or
      Podman; `doctor` checks engine, rootless, cgroup v2, KVM.
- [ ] Join tokens: coordinator mints a single-use, 15-minute join token;
      `hyvv enroll --token-file -` reads it from stdin (never argv), exchanges
      it for a per-coordinator bearer, idempotent (`--replace` to re-enroll).
- [ ] Bind: autodetect the tailnet address (`tailscale ip`), else loopback;
      refuse `0.0.0.0` unless `--i-know` plus a warning in `doctor`.
- [ ] `.deb`/`.rpm` deferred; uv-based install is the supported path.
- [ ] MR.INDIA: Orbit "Add SSH host" flow runs the installer over SSH, polls
      `/v1/version`, shows labels and capabilities.

Exit: a fresh Ubuntu 24.04 SSH host and a Fedora host enrolled from Orbit,
reboot survives, update + rollback works, bees run under gVisor.

### macOS (same phase)

Goal: an Apple-silicon Mac (laptop or Mac mini) runs bees.

- [ ] Default: per-user LaunchAgent (`~/Library/LaunchAgents/dev.hyvv.host.plist`,
      `launchctl bootstrap gui/<uid>`), `KeepAlive`, long `ExitTimeOut` for drain.
- [ ] Headless Mac mini: LaunchDaemon in `/Library/LaunchDaemons` with
      `UserName` set to a dedicated user (`--mode system`).
- [ ] Sandbox: Cua's own Linux runtime (Apple Virtualization, gVisor inside)
      when no engine is present; otherwise Colima (`--vm-type vz`), OrbStack or
      Docker Desktop. arm64 images only; Rosetta only on request.
- [ ] Pinned Cua image must be multi-arch (arm64 digest recorded per arch).
- [ ] Secrets in the login Keychain (agent) or System keychain / 0600 file
      (daemon).
- [ ] Homebrew formula in `mojomast/homebrew-tap` (formula, not cask, so no
      Gatekeeper/notarization gate for a Python tool).
- [ ] Memory cap for the sandbox VM via Cua `runtime.linux_memory_gib` or the
      engine's VM settings, reported by `doctor`.

Exit: a MacBook (agent) and a Mac mini (daemon) enrolled from Orbit, sleep and
reboot handled, update + rollback works.

## Phase 3: fleet features

- [ ] Host registry contract: `GET /v1/host` returns host id, labels, capacity,
      load, version, drain state; heartbeat every 30 s (Orbit polls, or hyvv
      pushes when outbound mode exists).
- [ ] Labels: `os`, `arch`, `gpu`, `sandbox`, `max_concurrent`, `models`,
      `region`, custom `key=value`.
- [ ] Cordon/drain from Orbit for one host or a label selector.
- [ ] Global concurrency budget: hyvv requires a lease id on create when
      `budget.required=true`; Orbit (budget authority) issues leases against
      the combined cap (today 12 bees across coordinators).
- [ ] Token rotation with overlap (`hyvv token rotate --grace 24h`), optional
      Tailscale identity check (`tailscale whois`) on top of bearer.
- [ ] OTel export: OTLP traces, logs and GenAI usage metrics to Phoenix or any
      collector; usage ledger stays the local source of truth.
- [ ] Update channels (`stable`, `beta`), opt-in auto-update with a
      maintenance window, minimum-version floor enforced by coordinators.
- [ ] Optional outbound-dial mode for hosts without a tailnet (Coder/Daytona
      style), behind a capability flag.
- [ ] winget and Scoop manifests; `.deb`/`.rpm` if users ask.

## Phase 4: bees as runners

- [ ] `verify` attempt kind on `/v1` (capability `verify_jobs`): pinned repo + commit + commands, JUnit + artifact back; coordinators post GitHub check runs through their GitHub App.
- [ ] Optional `agent: true` on verify jobs: a bee investigates failures, metered and leased like any bee.
- [ ] Ephemeral GitHub Actions runners from hyvv: one JIT runner per job in a fresh sandbox, destroyed after; per-job `generate-jitconfig` first, the runner scale set client later.
- [ ] Labels `hyvv`, `hyvv-linux`, `hyvv-windows`, `hyvv-macos`, `hyvv-gpu`, plus arch.
- [ ] `ci` lease class with a sub-cap (proposed 2) inside the combined 12-bee cap; CI spend tagged `source=ci` and counted in the $10/day warn-only line.
- [ ] Public-repo guardrails: trusted events only (push, schedule, dispatch, merge queue, tags, same-repo PRs), runner group limited to selected repos/workflows, no secrets on hyvv runners, egress allowlist, isolated Tailscale ACL group.
- [ ] `e2e.yml` on hyvv runners: real Cua sandboxes on each OS label, nightly and on push to main.

Exit: hyvv's own e2e suite runs nightly on hyvv runners for all three OSes;
a fork PR provably cannot reach a hyvv runner (tested with a throwaway fork).


## Decisions recorded 2026-11-12 (America/Toronto)

Kyle approved the following for this repo:

- Package hyvv as a Python package installed with uv, with signed releases.
- Run it as the logged-in user by default; a system service is opt-in.
- Do Linux and macOS at the same time after Windows.
- Build with bees on the Windows PC: at most 4 bees from this bot, combined cap 12 across coordinators.
- Reserve the `hyvv` name on PyPI and set up CI and branch rules.
- Spend warning is $10/day, warn-only.
- Bees as CI runners come later, in phase 4.

Open decisions that were not in that list (enrollment, auto-update, Homebrew tap, reusable release workflow location, and so on) still need a yes before they move.

## Open decisions for Kyle

See `docs/DEPLOYMENT.md` section 10 and `docs/CI.md` section 7 for the reasoning. Recommendation first.

1. Packaging: Python package via uv/PyPI now; single binary later only if needed.
2. Default service mode: user-level everywhere, system-level opt-in.
3. Enrollment: Orbit-minted single-use join token over SSH, per-coordinator bearer.
4. Updates: manual or coordinator-triggered with drain + auto-rollback; auto-update opt-in in phase 3.
5. Global bee budget: Orbit issues leases; hyvv enforces its local cap.
6. Signing: cosign keyless + GitHub provenance + sha256; no Apple Developer ID until a binary ships.
7. Connectivity: inbound over tailnet for v1; outbound-dial mode later.
8. Reserve PyPI `hyvv` and the Homebrew tap now; domain optional.
9. Linux engine: support rootless Docker and Podman; gVisor when present.
10. Release flow: release-please Release PR merged by Kyle, tag-driven signed release.
11. Reusable release workflow lives in `mojomast/workflow`, pinned by SHA.
12. Self-hosted runners on public repos: trusted events only; fork PRs always on GitHub-hosted.
13. Runner provider: per-job JIT first, scale set client after it leaves preview.
14. CI share of the cap: `ci` lease class, sub-cap 2 of the combined 12.
15. Rollout channels: canary on brrrrrrrr, then beta, then stable, promoted by PR.
