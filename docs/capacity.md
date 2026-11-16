# Capacity

## Bee ceiling: 14 (owner directive, final)

`MAX_WORKERS` in `launcher.py` is the hard clamp for `max_concurrent` — every
value is clamped into `1..MAX_WORKERS` at config load, and `POST /v1/config`
rejects anything above it with `400 max_concurrent must be 1..14`. It was
raised **10 → 14** by owner directive, which supersedes the interim cap-12
shared-with-Forge note (the amendment is recorded here; the coordinator keeps
implementation lanes 8–10 typical, leaving headroom in the shared ceiling for
coordinator-driven work).

## Runtime-only `max_concurrent` setter

`POST /v1/config {"max_concurrent": N}` accepts `1..MAX_WORKERS` and applies
`N` to the running launcher's in-memory cfg only:

- **Disk config unchanged.** `max_concurrent` is deliberately excluded from
  `CONFIG_HOT_KEYS` (`model`, `models`, `prefer_zoo`, `projects`), so the
  setter never persists — the config file keeps whatever value it already has.
- **Restart reverts.** Because nothing is written, the next restart re-clamps
  from the file, undoing the runtime change.
- **Deployed-version caveat.** Deployed **0.1.17** was built when
  `MAX_WORKERS = 10`, so deployed hosts still clamp at 10 and a setter value
  above 10 is refused there. The new ceiling of 14 takes effect only after the
  idle-gated deploy of this version.

## Test

`python3 -m pytest tests/test_worker_cap.py -q` proves the clamp accepts 14
and still refuses >14 (config load and the `/v1/config` patch path).
