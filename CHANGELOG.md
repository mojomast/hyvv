# Changelog

## Unreleased

- The launcher reports the build it runs, never a tag it merely sits near. `GET /v1/version`,
  `GET /v1/health`, the `Server` header, `doctor`, `data_dir/runtime_status.json` and attempt
  manifests now use `git describe --tags --always --dirty` (`0.1.19+25.g7f73b7c`, `.dirty` for a
  modified tree), so a worker 25 commits past `v0.1.19` can no longer answer `0.1.19`. An install
  without `.git` reads the describe output baked in `hyvv-build.json`
  (`scripts/bake_build_info.py`) or recorded in `hyvv-deployed.json`; with only a SHA it reports
  `<release>+g<sha>`, and with no identity at all `<release>+unknown`.
- `GET /v1/version` adds `release` (the tag base) and `build` (describe, commit, commits past tag,
  dirty, source); `GET /v1/health` adds the same. `hyvv-deployed.json` may carry `describe` and a
  describe-derived `version`, and the receipt republishes them.

## 0.1.19

- Kimi is a first-class provider key source, so `k3.1-pro` and `kimi-for-coding` lanes use the Kimi API key kind (#43).
- Nous Portal key kind, with the `nous:hy4-flash`, `nous:deepseek-v4.1-flash-1017` and free Nous models, a fallback chain and pacing (#44).
- Every attempt gets a wall clock, taken from `brief.budget.timeout_s` or the host `default_timeout_s` (default 3600 s), with an optional `require_budget` refusal (#45).
- slop-lint also runs on `merge_group` (#48).
- Version 0.1.19 in `launcher.py`, `pyproject.toml`, `uv.lock` and the `/v1` status label `launcher-0.1.19` (#49).

## 0.1.18

- First tagged release (`v0.1.18`, PR #41).
