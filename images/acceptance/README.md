# `acceptance` container class

A container class for CI check-run jobs (`secret-scan`, `acceptance-light`) that the
ordinary bee containers cannot run. Asked for by Hermes on hyvv #1; ordinary coding
bees are unchanged.

| File | What it is |
|------|------------|
| `Dockerfile` | `cua-bee:node24-oc1.18.32` + `bubblewrap` (Ubuntu 24.04 package) + `gitleaks` 8.21.2, sha256-pinned from the official `gitleaks_8.21.2_checksums.txt`, so the secret scan runs with `--network none`. |
| `seccomp/moby-default-v28.4.0.json` | Docker's default seccomp profile exactly as shipped in moby v28.4.0 (`vendor/github.com/moby/profiles/seccomp/default.json`, profiles v0.1.0; matches Docker Engine 28.4.0). sha256 `01536f1d1df938ae611eba20d6349e0de7a99b6ecdee1549427a0b01b8301e28`. |
| `seccomp/acceptance.json` | Derived by `derive_seccomp.py`: the two non-`CAP_SYS_ADMIN` `clone` rules' mask goes `0x7E020000 -> 0x6E020000` (permits `CLONE_NEWUSER`), and `unshare` is allowed. Nothing else changes. `python3 derive_seccomp.py --check` fails if it is stale. |

The container runs with no `--privileged`, no `--cap-add`, and seccomp and AppArmor
confinement left on.

## Why `docker run` instead of Cua

Cua 0.5.0's `SandboxCreateOptions` has no `security_opt`, seccomp, or capability
field. Its fields are `on, kind, runtime, image, name, token, pool, os, cpus,
memory_mb, ports, services, wait_for, ready_timeout_ms, env, fleet_*, warm,
max_pool_size, command, cloud, sidecars, registry_secret, build, network, overlays,
keep_on_failure, gpu`, and `Container` / `ImageBuild` / `Overlay` have none either.
The only other route is the daemon-wide `seccomp-profile` in `daemon.json`, but that
would change every container, coding bees included. So check jobs that use a
non-default profile are started by the launcher's `DockerCheckRunner`
(`docker run -d --security-opt seccomp=<file> --network none ...`, then
`docker exec` for each command, then `docker rm -f` on that container only).

## Launcher config

```json
"security_profiles": {
  "acceptance": {
    "image": "hyvv-acceptance:gl8.21.2-node24-oc1.18.32",
    "seccomp": "C:/path/to/hyvv/images/acceptance/seccomp/acceptance.json",
    "network": "none"
  }
},
"check_security_profile": "default"
```

A job sets `kind: "check"`, `brief.commands`, and optionally
`brief.security_profile: "acceptance"`. Coding bees (`opencode`, `agy-cua`) can only
use `default`, and naming any other profile returns 400. Profiles are limited to
`image`, `seccomp`, `network` (`none` or `bridge`), `memory_mb`, `cpus`,
`pids_limit` and `description`. Anything that mentions privileged, unconfined, or
SYS_ADMIN is refused when the config loads.

Check jobs return `check_run: {conclusion, security_profile, checks: [{command,
exit_code, duration_s, stdout_tail, stderr_tail, log_file}]}` and `patch: null`, so
`git checkout -B` inside a check has no effect on the result.

## Verified on Docker Engine 28.4.0 (Docker Desktop, WSL2), Nov 12 2026

| Profile | `unshare -Ur true` | `bwrap --unshare-user --dev-bind / / true` | `gitleaks version` |
|---------|--------------------|--------------------------------------------|--------------------|
| acceptance.json | exit 0 | exit 1 (see below) | `8.21.2`, exit 0 |
| Docker default | `unshare: unshare failed: Operation not permitted`, exit 1 | exit 1 | `8.21.2`, exit 0 |

bwrap still fails under `acceptance.json`. The cause comes from strace:

```
clone(child_stack=NULL, flags=CLONE_NEWNS|CLONE_NEWUSER|SIGCHLD) = -1 EPERM (Operation not permitted)
bwrap: No permissions to create new namespace, likely because the kernel does not allow non-privileged user namespaces.
```

bwrap always creates a mount namespace along with the user namespace, and
`CLONE_NEWNS` (`0x00020000`) is still in the clone mask. Even with the clone mask
widened, `mount(2)` is still denied without `CAP_SYS_ADMIN`:
`unshare -Urm true` gives `unshare: cannot change root filesystem propagation:
Operation not permitted`. bwrap also needs `mount`, `umount2` and `pivot_root`. This
profile stops at the requested change and is not widened further. Whether to also
allow `CLONE_NEWNS` plus `mount`/`umount2`/`pivot_root` is a decision for Kyle.
