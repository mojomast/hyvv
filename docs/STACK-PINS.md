# Stack pins

Evidence for the external revisions hyvv is configured to build or run against.
Every revision is recorded in full; nothing is abbreviated. The referenced files
remain the sources of truth, and the build identity at the end is derived from
them rather than pinned back to the commit that contains this document.

## Workflow actions

All `uses:` references in `.github/workflows/ci.yml` and
`.github/workflows/release.yml` are immutable full commit SHAs.

| Action pin | Release |
| --- | --- |
| `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` | v7.0.1 |
| `astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2` | v10.3.3 |
| `actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8` | v4.2.2 |
| `sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532` | v4.1.3 |
| `pypa/gh-action-pypi-publish@7245906fb61fa33a13554f9d5fb856fe61758086` | v1.14.3 |

## Python dependency

`pyproject.toml` pins `cua==0.5.0`; `uv.lock` resolves that exact version to the
following artifacts and SHA-256 digests.

| Artifact | SHA-256 |
| --- | --- |
| `cua-0.5.0-py3-none-macosx_10_12_x86_64.whl` | `a0e0caecfd56327f5421c098fd63591ac046e4f9327c8821bfb104ef5f1c2c0a` |
| `cua-0.5.0-py3-none-macosx_11_0_arm64.whl` | `d5a208a443dbae80e441186ce308fb801b37bf0689a4be32a2346f9730f9785b` |
| `cua-0.5.0-py3-none-manylinux_2_31_aarch64.whl` | `f7ae88c3e00af0800587fe15380e87669c9db89a5e45cb7a44f90cd81d98924a` |
| `cua-0.5.0-py3-none-manylinux_2_31_x86_64.whl` | `bda6f8e45e6738278b5e0e98bd719a5c06acf8cd45d3d43dfec6a3d4e6359a03` |
| `cua-0.5.0-py3-none-win_amd64.whl` | `54583bccc9f7d0e72f31f778fcf0712b6fd73d2fce306abec0687a31b002b21d` |
| `cua-0.5.0-py3-none-win_arm64.whl` | `b6a3dda91334931f4561dc84b333d4853adc19ea0c52b9be5d28df6bba771427` |

## Container and tool pins

| Pin | Full revision or digest | Source |
| --- | --- | --- |
| Cua runtime image | `ghcr.io/trycua/linux@sha256:fd00735ceb6c65ab628d962640068c9f63076a49a62d86f6b6eb5ac4bbcbbf6b` | `config.example.json` |
| gitleaks | `8.21.2` / `sha256:5bc41815076e6ed6ef8fbecc9d9b75bcae31f39029ceb55da08086315316e3ba` | `images/acceptance/Dockerfile` |
| Docker default seccomp (moby v28.4.0) | `sha256:01536f1d1df938ae611eba20d6349e0de7a99b6ecdee1549427a0b01b8301e28` | `images/acceptance/seccomp/moby-default-v28.4.0.json` |

## Current build identity (generated)

The values below are derived from `pyproject.toml`, `uv.lock` and the release
asset builder. They are the identity CI builds and tests, not a new pin.

| Field | Value |
| --- | --- |
| Package | `hyvv` |
| Version | `0.1.16` |
| sdist | `hyvv-0.1.16.tar.gz` |
| wheel | `hyvv-0.1.16-py3-none-any.whl` |
| Server header | `orbit-launcher/0.1.16` |
| Editable lock entry | `hyvv 0.1.16` |

No commit hash is recorded for this build identity: doing so would pin the
document to its own containing commit.

## CI matrix scope

| Run | Status | Reason |
| --- | --- | --- |
| `push` to `main` | kept, never cancelled | Required main-branch coverage |
| `pull_request` | kept, superseded candidates cancelled | Required PR contexts |
| `merge_group` | kept, superseded candidates cancelled | Merge-queue candidates |
| `push` to `feat/**` | eliminated | The same commit already runs through its PR |

The preserved required contexts are the existing jobs: `provisioning (windows)`,
`test`, `scan` and the `CI` gate that requires `test` and `scan`. Repository
rules and required-context names are unchanged.
