# Changelog

## 0.1.19

- Fixed Windows install/update payloads dropping `rails_emitter.py`, `rails_log_source.py`, `rails_export.py` and the four vendored `rizzstack_rails` runtime files. Windows source validation now covers the full declared payload, the Unix installer checks all six runtime imports, and isolated replay tests construct `RailsEmitter` with `rails_enabled` so a re-deploy cannot silently disable Rails export.
- Kimi is a first-class provider key source, so `k3.1-pro` and `kimi-for-coding` lanes use the Kimi API key kind (#43).
- Nous Portal key kind, with the `nous:hy4-flash`, `nous:deepseek-v4.1-flash-1017` and free Nous models, a fallback chain and pacing (#44).
- Every attempt gets a wall clock, taken from `brief.budget.timeout_s` or the host `default_timeout_s` (default 3600 s), with an optional `require_budget` refusal (#45).
- slop-lint also runs on `merge_group` (#48).
- Version 0.1.19 in `launcher.py`, `pyproject.toml`, `uv.lock` and the `/v1` status label `launcher-0.1.19` (#49).

## 0.1.18

- First tagged release (`v0.1.18`, PR #41).
