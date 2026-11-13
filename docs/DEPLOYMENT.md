# hyvv deployment and host management (DRAFT)

Written 2026-11-12. Updated 2026-11-12 with Kyle's decisions (see ROADMAP.md). Remaining open decisions are in section 10. Values in angle brackets are
placeholders. No real hosts, addresses or keys appear in this document, and none
should ever be added.

## 1. What a hyvv host is

A hyvv host is one machine (Windows PC, Linux box or SSH server, Mac) that:

- runs the `hyvv` service under a non-root user;
- starts one sandbox per attempt (Cua: container, microVM or VM);
- exposes `/v1` (attempts, artifacts, usage, version, health, admin) on loopback
  or its tailnet address only, behind per-coordinator bearer keys;
- keeps a local journal and usage ledger and exports OTel data.

Coordinators (Orbit, bundlebees) talk to hosts only through `/v1`. The host
never knows which coordinator product is calling beyond its coordinator id.

## 2. How comparable systems do it (research summary)

| System | Install | Runs as | Enrollment | Drain | Config reload |
| --- | --- | --- | --- | --- | --- |
| GitHub Actions runner | tarball + `config.sh --unattended` | systemd unit / launchd agent / Windows service via `svc.sh` | registration token, expires after 1 h; JIT config for one job | ephemeral / JIT runs one job and exits | re-run config |
| Actions Runner Controller | Helm on Kubernetes | pods | listener asks GitHub for JIT config per runner | ephemeral pods | CRD update |
| GitLab Runner | packages, binary | systemd service (root or user) | runner auth token | `SIGQUIT`: stop taking jobs, exit when jobs finish | file polled every 3 s, `SIGHUP` forces reload; only `listen_address` needs restart |
| Buildkite agent | packages, script | service | agent token | `SIGTERM`: finish job then disconnect; `SIGQUIT` cancels | restart |
| Tailscale | `install.sh`, packages, brew, winget | system daemon | auth key: 1 to 90 days, reusable or one-off, ephemeral, pre-approved, tagged | n/a | `tailscale set` live |
| Nomad client | binary, packages | systemd | server address + ACL / gossip key | `nomad node drain -enable` with deadline marks node ineligible | partial `SIGHUP` |
| k3s agent | `curl get.k3s.io` with `K3S_URL` + `K3S_TOKEN` | systemd | node join token | `kubectl drain` (cordon then evict) | restart |
| Coder agent | injected by template | workspace process | per-workspace agent token | workspace stop | n/a |
| Daytona runner | Helm / packages | service | runner registers outbound | n/a | n/a |
| Cua | `cua` CLI | per-user daemon | none locally | sandbox delete; Linux VM idles off after 120 s | `cua config set` |
| OpenHands runtime | Docker image | container per session | runtime API key | session end | n/a |

What hyvv takes from this:

- Short-lived, single-use join tokens (GitHub runner, Tailscale, k3s), never a
  long-lived secret pasted into a script.
- Graceful drain on the service manager's stop signal (Buildkite, GitLab) and an
  explicit drain/cordon verb (Nomad, Kubernetes).
- Hot reload of most config with a short list of restart-only keys (GitLab).
- Tagged, labelled hosts so schedulers place work by capability (Tailscale tags,
  GitHub labels, Buildkite tags, Nomad node attributes).
- Outbound-only dialing (Coder, Daytona, GitHub runners) is the long-term
  option for hosts without a tailnet.

Sources: section 11.

## 3. Packaging

hyvv is Python today (one `launcher.py` plus the `cua` package), run with uv.

Recommendation: ship a normal Python project published to PyPI as `hyvv`, with a
locked dependency set, installed by uv into a versioned directory. uv brings its
own Python, so hosts need no system Python.

- Release artifacts: wheel + sdist, `checksums.txt`, a Sigstore bundle for
  `checksums.txt`, and GitHub build-provenance attestations.
- Installers: `install.ps1` (Windows) and `install.sh` (Linux, macOS). Both take
  `--version` (default: current stable), verify checksum and signature before
  running anything, and are idempotent.
- uv itself is installed at a pinned version if missing.
- Homebrew: a formula in `mojomast/homebrew-tap`. Formulae are built and
  checksummed by Homebrew and are not affected by the Homebrew 5 cask
  Gatekeeper/notarization rules.
- winget and Scoop manifests: phase 3.
- `.deb`/`.rpm`: only if users ask; the uv install path is the supported one.
- A single static binary (PyInstaller, Nuitka or a rewrite) is not planned. If
  one ships later, macOS needs Developer ID signing and notarization, and
  Windows needs Authenticode signing to avoid SmartScreen prompts.

### Directory layout

| | Windows | Linux (user) | Linux (system) | macOS |
| --- | --- | --- | --- | --- |
| Program | `%LOCALAPPDATA%\hyvv\versions\<v>` | `~/.local/share/hyvv/versions/<v>` | `/opt/hyvv/versions/<v>` | `~/Library/Application Support/hyvv/versions/<v>` |
| Current pointer | `current.txt` read by the `hyvv.cmd` shim | `current` symlink | `current` symlink | `current` symlink |
| Config | `%APPDATA%\hyvv\config.json` | `~/.config/hyvv/config.json` | `/etc/hyvv/config.json` | `~/Library/Application Support/hyvv/config.json` |
| State, journal, ledger | `%LOCALAPPDATA%\hyvv\state` | `~/.local/state/hyvv` | `/var/lib/hyvv` | `~/Library/Application Support/hyvv/state` |
| Logs | `%LOCALAPPDATA%\hyvv\logs` | journald | journald | `~/Library/Logs/hyvv` |

The current Windows path `%USERPROFILE%\.orbit-launcher\config.json` is migrated
once by `hyvv install` (secrets moved to the credential store, file kept as
`.bak` until Kyle removes it).

## 4. Running as a service, per OS

### Windows

- Default: a per-user Scheduled Task, trigger "At log on", hidden window,
  restart on failure, no time limit. Docker Desktop needs an interactive
  logged-in session, so a Windows service (WinSW, NSSM) running as another
  account cannot rely on it.
- Option for later: a Windows service via WinSW, only with a sandbox runtime
  that does not need a desktop session (for example Cua's own WSL2
  distribution, served on a per-user named pipe, or Docker Engine inside WSL).
- Memory: WSL2 takes up to 50 % of host RAM by default. `hyvv doctor` reads
  `%UserProfile%\.wslconfig`, reports the current cap and suggests one (for
  example `memory=16GB`, `processors=8`, `autoMemoryReclaim=gradual`). hyvv
  never edits `.wslconfig` and never runs `wsl --shutdown`; a change needs
  Kyle's yes because restarting WSL stops every running bee.
- Stop: there is no SIGTERM; the task's stop action calls `hyvv drain --wait`
  first, then stops the process.

### Linux

- Default `--mode user`: a `systemd --user` unit plus `loginctl enable-linger
  <user>` so it starts at boot and survives logout. This is the only step that
  needs sudo; if sudo is unavailable, the installer prints the one command for an
  admin.
- Sandbox engine: rootless Docker or rootless Podman. Cua's `auto` picks gVisor
  (`runsc`) when installed, else runc. Membership in the `docker` group is
  root-equivalent, so hyvv does not add users to it.
- `--mode system` for dedicated servers: a `hyvv` system user and a hardened
  unit:

```ini
[Service]
User=hyvv
ExecStart=/opt/hyvv/current/bin/hyvv serve
ExecReload=/bin/kill -HUP $MAINPID
KillSignal=SIGTERM          # hyvv treats SIGTERM as drain
TimeoutStopSec=2h           # longest attempt we allow to finish
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
StateDirectory=hyvv
LogsDirectory=hyvv
ReadWritePaths=/var/lib/hyvv
LoadCredentialEncrypted=deepseek:/etc/credstore.encrypted/hyvv-deepseek.cred
LoadCredentialEncrypted=go:/etc/credstore.encrypted/hyvv-go.cred
```

### macOS

- Default: a per-user LaunchAgent at
  `~/Library/LaunchAgents/dev.hyvv.host.plist`, loaded with
  `launchctl bootstrap gui/<uid>`, `RunAtLoad`, `KeepAlive`, and a long
  `ExitTimeOut` so launchd's SIGTERM gives the drain time. A LaunchAgent starts
  only when the user logs in, which suits laptops.
- Headless Mac mini: `--mode system` writes a LaunchDaemon in
  `/Library/LaunchDaemons` with `UserName` set to a dedicated user, the same
  fix GitHub runner users apply because the runner's LaunchAgent will not load
  without a GUI session.
- Sandbox: Cua's own Linux runtime (Apple Virtualization, gVisor inside, up to
  75 % of RAM capped at 32 GiB by default, set with `runtime.linux_memory_gib`)
  when no container engine is installed; otherwise Colima (`--vm-type vz`),
  OrbStack or Docker Desktop. hyvv sets an explicit VM memory cap at install.
- Apple silicon: arm64 images only. The pinned Cua image digest is recorded per
  architecture. Rosetta (`--vz-rosetta`) only when a project needs amd64.
- macOS limits the machine to 128 VMs at once; irrelevant at our scale but
  `doctor` reports it.

## 5. Install, enrollment and bootstrap

### One-line install, done safely

The installer is downloaded from a versioned release URL, not a moving `main`
URL, and verified before it runs:

```sh
v=<version>
curl -fsSLO https://github.com/mojomast/hyvv/releases/download/v$v/install.sh
curl -fsSLO https://github.com/mojomast/hyvv/releases/download/v$v/install.sh.sigstore.json
cosign verify-blob install.sh --bundle install.sh.sigstore.json \
  --certificate-identity-regexp '^https://github.com/mojomast/hyvv/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
sh install.sh --version "$v" --mode user
```

A convenience `curl | sh` form is documented for people who want it, with the
pinned-and-verified form above as the recommended one.

### Enrollment from a coordinator over SSH

1. Orbit (budget and registry authority) mints a join token: single use,
   valid 15 minutes, bound to the coordinator id and requested labels.
2. Orbit connects over SSH and runs the pinned, verified installer.
3. Orbit pipes the join token on stdin: `hyvv enroll --token-file -`. Tokens
   never go in argv (visible in `/proc/<pid>/cmdline`) or in shell history.
4. hyvv creates a host id, picks the bind address (tailnet address if
   `tailscale` is up, else loopback), generates a per-coordinator bearer key,
   stores it in the OS secret store and returns the key and host id to Orbit
   once over the SSH channel.
5. Orbit calls `GET /v1/version` and `GET /v1/host` over the tailnet to confirm
   labels and capabilities, then marks the host ready.

Idempotency: re-running steps 2 to 4 on an enrolled host is a no-op that
reports the existing host id; `--replace` rotates the key and re-enrolls, like
the GitHub runner's `--replace`.

Tailnet join (if the host is not on the tailnet yet) uses a Tailscale auth key
that is tagged (for example `tag:hyvv`), pre-approved and single-use; tagged
devices have key expiry off by default, so ACLs on `tag:hyvv` must limit what
those hosts can reach.

## 6. Updates and rollback

`hyvv update [--version <v> | --channel stable|beta]`:

1. Download wheel set and `checksums.txt`; verify the Sigstore bundle and
   `gh attestation verify` provenance; verify every hash.
2. Install into `versions/<new>` (a fresh uv venv); run `hyvv doctor --offline`
   from the new version.
3. Drain: stop accepting new attempts (`POST /v1/attempts` returns 503 with
   `Retry-After`), let in-flight attempts finish up to `drain_deadline`
   (default 2 h), then park any still running as `interrupted` with their
   journal kept.
4. Flip `current` to the new version and restart through the service manager.
5. Health gate: within 60 s, `/v1/health` is ok, `/v1/version` reports the new
   version and all previously advertised capabilities, the sandbox engine
   answers, and one no-op sandbox starts.
6. If any check fails: flip `current` back, restart, report `rolled_back` with
   the reason to the coordinator and the log.
7. Keep the previous 2 versions; `hyvv rollback` flips to the last good one.

Version skew:

- `/v1` only gets additive changes; breaking changes go to `/v2`, served next to
  `/v1` for at least one minor release.
- Coordinators feature-detect through `GET /v1/version` capabilities, never by
  comparing version strings.
- Coordinators support hosts up to 2 minor versions behind (the Kubernetes
  model lets kubelets lag up to 3 minors). A coordinator may set a minimum
  version floor for security fixes and stop scheduling onto older hosts, the way
  GitHub blocks job assignment to runners missing critical updates.
- Auto-update is off by default in phases 0 to 2 and opt-in from phase 3
  (`stable` channel, maintenance window). Promotion through channels is in
  `docs/CI.md`.

## 7. Fleet management

- Registry: Orbit owns the registry. hyvv exposes `GET /v1/host`: host id,
  hostname alias, labels, capabilities, `max_concurrent`, running attempts,
  drain state, version, last update result. Orbit polls every 30 s; three
  misses mark the host offline.
- Labels: `os`, `arch`, `gpu`, `sandbox` (`docker`, `podman`, `cua-wsl`,
  `cua-vz`, `lume`), `max_concurrent`, `models`, plus `key=value` custom labels.
- Cordon and drain: `hyvv cordon` (no new attempts), `hyvv drain [--deadline]`
  (cordon + wait), `hyvv uncordon`; matching admin endpoints for Orbit.
- Config reload: hot keys (`max_concurrent`, models and routing, projects,
  budgets, labels, log level) reload on file change (polled every 3 s), on
  SIGHUP (Linux, macOS) or on `hyvv reload` / `POST /v1/admin/reload`
  (all OSes). Restart-only keys: `bind`, `port`, sandbox engine.
- Secrets: never in `config.json`; the config holds `secret://<name>`
  references.
  - Windows: Credential Manager (DPAPI-protected, per user).
  - macOS: login Keychain (agent) or System keychain (daemon).
  - Linux user mode: libsecret when a session bus exists, else a 0600 file in
    `~/.config/hyvv/secrets/`.
  - Linux system mode: `systemd-creds` encrypted credentials via
    `LoadCredentialEncrypted`, read from `$CREDENTIALS_DIRECTORY`.
- Observability: the usage record v1 stays the local source of truth (JSONL
  ledger). hyvv exports OTLP traces, logs and GenAI metrics
  (`gen_ai.client.inference.usage.*` token counters) to Phoenix or any
  collector, with prompt and content capture off by default.
- Budgets: each host enforces its own `max_concurrent`. The combined bee cap
  (today 12 across coordinators) is enforced by Orbit issuing leases; with
  `budget.required=true`, a create without a valid lease is refused with 409
  `lease_required`. The daily $10 spend line is warn-only and computed from the
  usage ledgers.

## 8. Security

- Least privilege: non-root user; no `docker` group; rootless engines on Linux;
  VM-backed sandboxes on Windows (WSL2) and macOS (Apple Virtualization).
- Network: bind to loopback or the tailnet address only; `0.0.0.0` refused
  unless forced, and `doctor` flags it. Tailscale ACLs allow only coordinators
  to reach `tag:hyvv` on the hyvv port.
- Auth: one bearer key per coordinator, stored hashed on the host; rotation with
  overlap (`hyvv token rotate --grace 24h`); optional `tailscale whois` check
  that the caller's node matches the coordinator's tag. mTLS is not needed while
  traffic stays inside WireGuard; revisit for outbound mode.
- Sandbox: gVisor by default on Linux where available, runc otherwise; no host
  mounts other than the attempt workspace; no Docker socket in the sandbox;
  egress allowlist per project (phase 3).
- Supply chain: signed releases, pinned dependencies with hashes, provenance
  verified before every install and update.
- Provider keys: held by hyvv, injected per attempt, never written to the
  journal, the ledger or logs, never returned by any endpoint.

## 9. Bees as CI runners

See `docs/CI.md` section 4. Summary: hyvv can serve ephemeral GitHub
Actions runners (one JIT runner per job, inside a fresh sandbox) for private
repos and trusted events only, and offers a native `verify` attempt kind for
contract and test jobs that coordinators post back as GitHub check runs.

## 10. Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Packaging | Python package via uv and PyPI; no single binary for now |
| 2 | Default service mode | User-level on all OSes; system-level opt-in for headless hosts |
| 3 | Enrollment | Orbit-minted single-use 15-minute join token over SSH stdin; per-coordinator bearer |
| 4 | Updates | Manual or coordinator-triggered with drain and auto-rollback; opt-in auto-update in phase 3 |
| 5 | Global bee budget | Orbit issues leases; hyvv enforces local cap and checks leases when required |
| 6 | Signing | Sigstore keyless + GitHub attestations + sha256; Apple and Authenticode signing only if a binary ships |
| 7 | Connectivity | Inbound over tailnet for v1; outbound-dial mode later behind a capability |
| 8 | Linux engine | Support rootless Docker and Podman, prefer what is installed, gVisor when present |
| 9 | Names | Reserve PyPI `hyvv` and `mojomast/homebrew-tap` now |
| 10 to 14 | CI/CD and runners | See `docs/CI.md` section 7 |

## 11. Sources

- GitHub runner service install: https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application
- GitHub runner token lifetime and `--replace`: https://cirunner.dev/blog/github-actions-self-hosted-runners/
- GitHub runner update policy (63 days, critical updates block jobs): https://docs.github.com/en/actions/reference/runners/self-hosted-runners
- GitHub runner on macOS, LaunchAgent vs LaunchDaemon: https://github.com/actions/runner/pull/2465 and https://mnapoli.fr/running-github-actions-mac-mini
- ARC JIT flow: https://docs.github.com/en/actions/concepts/runners/actions-runner-controller
- GitLab Runner signals and reload: https://docs.gitlab.com/runner/commands/ and https://docs.gitlab.com/runner/configuration/advanced-configuration/
- Buildkite agent lifecycle: https://buildkite.com/docs/agent/lifecycle
- Tailscale auth keys: https://tailscale.com/docs/features/access-control/auth-keys
- Tailscale updates: https://tailscale.com/docs/features/client/update
- Nomad drain and eligibility: https://developer.hashicorp.com/nomad/commands/node/eligibility
- k3s agent install: https://docs.k3s.io/installation/configuration
- Coder networking: https://coder.com/docs/admin/networking/
- Daytona architecture: https://www.daytona.com/docs/architecture
- Cua local runtimes: https://cua.ai/docs/cua-sdk/guides/local-runtimes
- OpenHands runtime: https://docs.openhands.dev/openhands/usage/architecture/runtime
- Rootless Docker and linger: https://docs.docker.com/engine/security/rootless/tips/
- Docker group is root-equivalent: https://docs.docker.com/engine/install/linux-postinstall/
- systemd sandboxing: https://wiki.archlinux.org/title/Systemd/Sandboxing
- systemd credentials: https://systemd.io/CREDENTIALS/
- Python keyring on headless Linux: https://github.com/jaraco/keyring/issues/569
- WSL config: https://learn.microsoft.com/en-us/windows/wsl/wsl-config
- Docker Desktop as a Windows service: https://github.com/docker/for-win/issues/11396
- Mac container runtimes: https://ddev.com/blog/docker-performance/
- Homebrew 5.0 cask Gatekeeper change: https://brew.sh/2025/11/12/homebrew-5.0.0/
- Apple notarization: https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
- uv tools and installer pinning: https://docs.astral.sh/uv/concepts/tools/ and https://docs.astral.sh/uv/getting-started/installation/
- GoReleaser signing and attestations (pattern reference): https://goreleaser.com/customization/sign/sign/
- Kubernetes version skew: https://kubernetes.io/releases/version-skew-policy/
- OTel GenAI token metrics: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/client-inference.md
