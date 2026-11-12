# CI/CD for hyvv and its sibling repos (DRAFT)

Written 2026-11-12. Applies to hyvv, workflow, bundlebees, Orbit and the
plugins. Updated 2026-11-12 with Kyle's decisions (uv packaging, user-level default,
Linux and macOS in the same phase after Windows, $10/day warn-only spend,
bees as CI runners in phase 4). Nothing is merged or tagged without Kyle's yes.

## 1. Baseline every repo gets

| Area | Rule |
| --- | --- |
| Triggers | `pull_request` and `push` to the default branch; `merge_group` if a merge queue is used; never `pull_request_target` with a checkout of PR code |
| Token | Repo default `GITHUB_TOKEN` read-only; every workflow starts with `permissions: {}` and each job asks only for what it needs |
| Actions | Pinned to full commit SHAs (or a workflow dependency lockfile once adopted); Dependabot or Renovate opens the bump PRs; zizmor lints workflows, including its impostor-commit rule |
| Secrets | No long-lived cloud or registry secrets: PyPI trusted publishing and other OIDC exchanges with `id-token: write` only on the publishing job; anything that must stay a secret lives in a protected `release` environment |
| Secret scanning | gitleaks on every PR and push (gitleaks-action needs no licence for repos on a personal account; an org needs a free key), plus GitHub push protection; the house host/tailnet grep runs as a separate step |
| Code scanning | CodeQL for the language and for Actions workflows; dependency review on PRs; `pip-audit` or `npm audit` |
| Branch rules | Ruleset on the default branch: PR required, required checks green, no force-push, no deletion |
| Fork PRs | "Require approval for all external contributors"; fork PRs only ever run on GitHub-hosted runners |
| Releases | release-please Release PR (Conventional Commits); merging it, which needs Kyle's yes, creates the tag and GitHub Release; build and sign on GitHub-hosted runners only |

## 2. hyvv pipelines

### `ci.yml` (every PR and push)

- Lint and types: ruff, pyright (or mypy).
- Unit tests: matrix
  - `os`: `ubuntu-latest`, `ubuntu-24.04-arm`, `windows-latest`, `macos-latest` (arm64)
  - `python`: lowest supported and newest (via uv)
- Installer tests: `install.sh --dry-run` on Linux and macOS, `install.ps1
  -WhatIf` on Windows; service file rendering (systemd unit, launchd plist,
  Scheduled Task XML) checked against golden files.
- No real sandboxes here; hosted runners are for fast, hermetic checks.

### `contract.yml` (every PR)

- Validate the OpenAPI document and JSON Schemas (`hyvv.worker-artifact/1`,
  usage record v1, error envelope).
- Run the conformance suite (AC ids from the artifact endpoint contract,
  AC-001 to AC-009, reused verbatim) against:
  1. the reference mock server shipped in the repo, and
  2. a real `hyvv serve` started on loopback in the job with a fake sandbox
     driver.
- Fails on any non-additive `/v1` change (schema diff against the last tag)
  unless the PR targets `/v2`.

Orbit and bundlebees each run the same suite against the mock, pinned to a hyvv
tag (`HYVV_CONTRACT_REF=v0.x.y`). MR.INDIA owns those jobs.

**Today (PR #1):** `ci.yml` already runs the contract suite on every OS and
Python in the matrix with `uv run python -m contract -v`. That is the in-process
target from item 2 above: `launcher.py` started on loopback with its scripted
`FakeBackend`, plus the `reference.py` mock for the artifact endpoint. The same
command runs locally. Point it at a live `/v1` host with `HYVV_BASE_URL` and
`HYVV_TOKEN_FILE` (or `--base-url` / `--token-file`); live runs are read-only
unless `--writes` is passed. A separate `contract.yml` and the schema diff
against the last tag are still planned.

### `security.yml`

gitleaks, zizmor, CodeQL (python, actions), dependency review, pip-audit.

### `release.yml` (on the tag release-please creates)

Calls a reusable workflow kept in one place (proposed:
`mojomast/workflow/.github/workflows/release-python.yml`, referenced by full
SHA) so the signing identity is the reusable workflow, which is what GitHub
documents for SLSA v1 Build Level 3 with artifact attestations.

1. Build wheel and sdist with uv on `ubuntu-latest`.
2. Write `checksums.txt`.
3. `actions/attest-build-provenance` on every artifact (permissions:
   `id-token: write`, `attestations: write`, `contents: write` for the release
   upload only).
4. `cosign sign-blob --bundle` on `checksums.txt`, `install.sh`, `install.ps1`
   (keyless, GitHub OIDC identity).
5. Publish to PyPI with trusted publishing (PyPI attestations on by default).
6. Upload artifacts, bundles and checksums to the GitHub Release.
7. Open (not merge) the Homebrew tap bump PR via a GitHub App token.
8. Mark the release `beta` in `channels/beta.json`.

`slsa-framework/slsa-github-generator` is no longer actively maintained; use
GitHub artifact attestations instead, verified with `gh attestation verify`.

### `e2e.yml` (end-to-end on real sandboxes)

Runs on hyvv runners (section 4), trusted events only: push to `main`, nightly
schedule, manual dispatch by a maintainer. Brings up a real Cua sandbox on each
OS label, runs one attempt end to end with a stub model, checks artifact and
usage record.

## 3. Staged rollout channels

| Channel | Who gets it | Gate to the next |
| --- | --- | --- |
| `canary` | brrrrrrrr only, `hyvv update --channel beta` | 24 h soak with real bees, zero rollbacks, health gate passed, no new errors in the usage ledger or Phoenix |
| `beta` | opt-in hosts | 72 h, no rollback reports from any host |
| `stable` | everyone, default for new installs | n/a |

Promotion is a PR that edits `channels/stable.json` (version + digest), so it is
reviewed, signed through the normal release path, and reversible by reverting
the PR. Hosts verify the channel file's signature before acting on it.

The same idea applies to Orbit and bundlebees deployments: canary first (the
existing Orbit instance or a staging copy), promote by PR. Deploys to Orbit,
moltussy-1 or the VPS still need Kyle's yes each time.

## 4. Bees as runners

Two separate features.

### 4a. hyvv as a provider of ephemeral GitHub Actions runners

- hyvv runs a scale-set listener (GitHub's runner scale set client, public
  preview since February 2026, works without Kubernetes) or, as a simpler
  first step, requests one JIT config per job through
  `POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig`.
- Each job gets a fresh Cua sandbox with the official runner inside, started
  with `--jitconfig` passed through a 0600 env file or stdin, never host argv.
  A JIT runner runs one job and is removed by GitHub. The sandbox is destroyed
  afterwards.
- Labels: `hyvv`, `hyvv-linux`, `hyvv-windows`, `hyvv-macos`, `hyvv-gpu`,
  plus arch (`x64`, `arm64`).
- Capacity: the scale set reports `min(free host slots, CI leases)`. CI jobs
  use host slots and an Orbit lease of class `ci`, with a sub-cap (proposed
  2) inside the combined 12, so CI never starves bees.
- Credentials: a GitHub App with only the runner-administration permission on
  selected repos; its key in the OS secret store; installation tokens are
  short-lived. The runner sandbox never sees the App key, provider keys or the
  hyvv bearer.
- Sandbox rules: no host mounts, no Docker socket, egress allowlist (GitHub,
  package registries), wiped after one job; VM boundary on Windows and macOS,
  gVisor on Linux where available.

### 4b. hyvv-native `verify` jobs

- New attempt kind `verify` on `/v1` (additive, capability `verify_jobs`): a
  pinned repo and commit plus a command list (tests, `/v1` conformance suite,
  build), no model by default.
- Optional `agent: true` lets a bee investigate a failure; that path is
  metered like any other bee run.
- Results come back as the normal artifact plus a JUnit report. The coordinator
  (bundlebees or Orbit) posts them as a GitHub check run using its GitHub App
  (`checks: write`), so GitHub never connects to the tailnet.
- Use: bundlebees verify steps, cross-repo contract runs (Orbit at HEAD against
  hyvv at HEAD), long or heavy suites, and anything that needs the tailnet.

### 4c. Self-hosted runners and public repos

GitHub recommends self-hosted runners only for private repos, because anyone
can fork a public repo and open a PR that runs code on the runner. hyvv,
bundlebees and the plugins are public, so:

1. Fork PRs never run on hyvv runners. Jobs that target `hyvv-*` labels use
   `if:` guards so they only run for `push`, `schedule`, `workflow_dispatch`,
   `merge_group` and tags, or for PRs whose head repo is the same repo.
2. The repo or org setting "Require approval for all external contributors"
   is on.
3. The runner group (or repo-level scale set) is limited to selected repos
   and, where available, selected workflows.
4. Runners are JIT and ephemeral: one job, then the sandbox is gone.
5. No secrets on hyvv runners; signing and publishing stay on GitHub-hosted
   runners.
6. No `pull_request_target`; no caches written by untrusted runs.
7. hyvv hosts used as runners sit in a Tailscale ACL group that cannot reach
   other hosts or coordinators' admin ports.

## 5. GitHub-hosted or hyvv runner?

| Job | Where | Why |
| --- | --- | --- |
| Lint, unit tests, OS matrix, contract vs mock | GitHub-hosted | Free and unlimited standard runners for public repos, including macOS and Windows; clean VM per job |
| Fork PRs (any job) | GitHub-hosted | Safety: never self-hosted |
| Build, sign, attest, publish | GitHub-hosted | Provenance isolation; OIDC identity |
| Real-sandbox e2e (Cua, WSL2, Apple Virtualization) | hyvv runner | Needs nested virtualization and real engines |
| GPU jobs | hyvv runner (`hyvv-gpu`) | Hosted GPU runners cost money |
| Jobs that need the tailnet (live contract against a canary host) | hyvv `verify` job | Tailnet stays private |
| Private repos with heavy minutes | hyvv runner | Hosted minutes are billed on private repos; self-hosted are free today |
| Agent evals or bee-assisted verify | hyvv `verify` with `agent: true` | Metered by the usage ledger and leases |

## 6. Cost and token budgets

- Compute: CI jobs use host slots and `ci`-class leases (sub-cap 2 inside the
  combined 12).
- Tokens: only `verify` jobs with `agent: true` and scheduled evals spend
  tokens. They go through the same routing (OpenCode Go by default, DeepSeek
  direct for priority) and write usage records like any bee.
- The daily $10 line stays warn-only and includes CI-triggered spend, shown
  as both list-price dollars and Go allowance percent; CI usage is tagged
  `source=ci` so it can be split out in Phoenix.
- Each scheduled eval or agent job declares `budget_usd`; hyvv stops the attempt
  when the estimate passes it.

## 7. Open decisions (CI/CD and runners)

| # | Decision | Recommendation |
| --- | --- | --- |
| 10 | Release flow | release-please Release PR, merged by Kyle, tag-triggered signed release |
| 11 | Where the reusable release workflow lives | `mojomast/workflow`, pinned by SHA from each repo |
| 12 | Self-hosted runners on public repos | Allowed only for trusted events with the 7 rules above; fork PRs always on GitHub-hosted |
| 13 | Runner provider first step | Per-job JIT config first; scale-set listener once the client leaves preview |
| 14 | CI share of the bee cap | `ci` lease class, sub-cap 2 of the combined 12; tokens only when `agent: true` |
| 15 | Channels | canary on brrrrrrrr, then beta, then stable, promoted by PR |

## 8. Sources

- Self-hosted runners and public repos: https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners
- GitHub Well-Architected Actions security: https://learn.github.com/well-architected/library/application-security/recommendations/actions-security/
- Secure use reference (least privilege, secrets): https://docs.github.com/en/actions/reference/security/secure-use
- Workflow dependency lockfiles: https://docs.github.com/en/actions/concepts/security/workflow-dependency-lockfiles
- OIDC: https://docs.github.com/en/actions/concepts/security/openid-connect
- OWASP GitHub Actions cheat sheet: https://cheatsheetseries.owasp.org/cheatsheets/GitHub_Actions_Security_Cheat_Sheet.html
- `pull_request_target`: https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target
- Fork PR approval: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/approve-runs-from-forks
- JIT runners API: https://docs.github.com/en/enterprise-cloud@latest/rest/actions/self-hosted-runners
- JIT runners removed after one job: https://help.github.com/en/actions/hosting-your-own-runners/removing-self-hosted-runners
- Runner scale set client: https://github.com/actions/scaleset and https://github.blog/changelog/2026-02-05-github-actions-early-february-2026-updates/
- ARC: https://docs.github.com/en/actions/concepts/runners/actions-runner-controller
- Hardening self-hosted runners on public repos: https://www.stepsecurity.io/blog/defend-your-github-actions-ci-cd-environment-in-public-repositories
- Artifact attestations and SLSA L3: https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/increase-security-rating
- slsa-github-generator status: https://github.com/slsa-framework/slsa-github-generator/
- PyPI trusted publishing and attestations: https://docs.pypi.org/attestations/producing-attestations/
- release-please: https://github.com/googleapis/release-please-action/
- gitleaks-action licence: https://github.com/gitleaks/gitleaks-action/
- Runner pricing: https://docs.github.com/en/billing/reference/actions-runner-pricing and https://docs.github.com/en/actions/reference/runners/github-hosted-runners
