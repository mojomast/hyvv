# Bee landed events (`rizzstack.bee.landed.v1`)

hyvv records one durable terminal event per attempt. The event says that a bee
landed: the attempt reached `succeeded`, `failed`, or `cancelled`, and the
launcher has persisted the observed result (when one exists). It is intentionally
small. It carries identities, state, revision and evidence hashes, never briefs,
tokens, patches, logs or host paths.

## Outbox

```
<data_dir>/events/outbox/<event_id>.json
```

Every event is written with:

1. a temporary file in the same directory,
2. `flush()` and `fsync()` on the file,
3. `os.replace()` onto `<event_id>.json`,
4. `fsync()` on the outbox directory.

A crash therefore leaves either no event or one complete event, never a torn
JSON file. The file is the replayable source of truth. Delivery to a notifier is
a separate, best-effort signal; a notifier failure never removes or rewrites the
outbox entry.

`landed_event_id(attempt_id, state)` is deterministic for one terminal
observation, so replaying the same observation addresses the same file.

## CloudEvents envelope

```json
{
  "specversion": "1.0",
  "id": "bee-0123456789abcdef0123456789abcdef",
  "source": "hyvv://worker-1",
  "type": "rizzstack.bee.landed.v1",
  "time": "2026-11-14T12:34:56.789012Z",
  "subject": "att_0123456789abcdef",
  "datacontenttype": "application/json",
  "dataschema": "https://rizstack.dev/schemas/bee-landed/v1",
  "data": {
    "event_id": "bee-0123456789abcdef0123456789abcdef",
    "feature_id": "feat_1",
    "job_id": "job_1",
    "task_id": "task_1",
    "attempt_id": "att_0123456789abcdef",
    "coordinator": {"id": "orbit", "session": "sess_1"},
    "harness": "opencode",
    "model": "deepseek-v4.1-flash",
    "state": "succeeded",
    "source_rev": "0123456789abcdef0123456789abcdef01234567",
    "lease_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "result": {
      "available": true,
      "artifact_sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
      "reference": "hyvv://attempts/att_0123456789abcdef/result",
      "schema": "orbit.local-worker.result/v1"
    },
    "evidence_sha256": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
  }
}
```

### Field sources

| Field | Source |
| --- | --- |
| `id` / `data.event_id` | deterministic attempt id + terminal state |
| `source` | `launcher_id`, then `worker_id`, `source`, `host_id`, then the host name, as a `hyvv://` URI |
| `time` | the attempt's persisted terminal `updated_at` |
| `feature_id`, `job_id`, `task_id` | `brief.feature_id` / `brief.feature.id`, `brief.job_id` / `brief.job.id`, `brief.task_id` / `brief.task.id` |
| `attempt_id` | the launcher attempt id, falling back to `brief.attempt_id` |
| `coordinator.id` | `brief.coordinator.id`, `brief.coordinator` string, or `brief.coordinator_id` |
| `coordinator.session` | `brief.coordinator.session`, `brief.coordinator_session`, `brief.session_id`, or `brief.session` |
| `harness` | `agy` for `agy-cua`, `check` for check jobs, otherwise `opencode` |
| `model` | the attempt model, else `brief.model`, else the launcher default |
| `source_rev` | `brief.source_rev` |
| `lease_sha256` | `brief.lease_sha256`, else a SHA-256 of the canonical `brief.lease` value |
| `result.available` | whether `<attempt_id>.result.json` exists and was readable |
| `result.artifact_sha256` | SHA-256 of the result artifact bytes |
| `result.reference` | logical `hyvv://attempts/<attempt_id>/result`, never a filesystem path |
| `evidence_sha256` | SHA-256 over the terminal evidence fields |

## Idempotency and conflicts

The identity of a landed event is `(attempt_id, terminal state)`.

- A duplicate observation of the same attempt and state returns the existing
  event. It does not write a second file and does not send a second notifier
  signal.
- Evidence is compared without the event time, so re-observing the same state
  later is idempotent.
- If the outbox already contains an event for the attempt with another terminal
  state, or with different evidence for the same state (for example another
  `source_rev`, `lease_sha256`, model or result artifact hash), the write is
  refused with `LandedEventConflict`. The existing event is left untouched.
- Conflicting evidence found while recovering at startup is recorded in
  `Launcher.landing_conflicts`; startup continues and the existing event is not
  overwritten.

## Production points

`Launcher._set()` emits the event immediately after it persists a terminal
attempt state. `_finish()` and the check-run controller write the result artifact
before calling `_set()`, so a successful event normally carries the artifact
hash. Failed and cancelled attempts may have no result artifact; their event has
`result.available: false`, `artifact_sha256: null`, and `reference: null`.

If the process dies after the journal says terminal but before the event is
written, the next `Launcher` construction scans terminal journal entries and
writes the missing events. Existing events are left alone.

## Local notifier

No broker is required. Configure one local signal target:

```json
{
  "landed_notifier": {"type": "file", "path": "/run/hyvv/landed.jsonl"}
}
```

or a Unix stream socket (the default socket mode):

```json
{
  "landed_notifier": {"type": "unix", "path": "/run/hyvv/landed.sock"}
}
```

String forms are accepted: `"unix:///run/hyvv/landed.sock"`,
`"unix:/run/hyvv/landed.sock"`, `"file:///run/hyvv/landed.jsonl"`, or a plain
file path. `landed_notifier`, `landing_notifier`, `bee_landed_notifier`,
`event_notifier`, `outbox_notifier`, `landed_notify`, and `notifier` are accepted
config keys; `{"landed": {"notifier": ...}}` is also accepted. Shorthand
`notify_socket` / `landed_socket` (Unix) and `notify_file` / `notify_path` /
`landed_file` (file) are accepted too.

The file notifier appends one compact JSON line per event and fsyncs it. The Unix
notifier connects, writes one compact JSON line, and closes. For a datagram
socket set `{"type": "unix", "mode": "dgram", ...}`. `timeout_s` defaults to 2.

If no notifier is configured, or the notifier fails (missing socket, permission
error, directory path, timeout), the outbox entry remains durable. Delivery is
at-least-once; replay later with:

```python
launcher.replay_landed_events()
```

or, outside a `Launcher`:

```python
launcher.replay_landed_events(data_dir, cfg)
```

This re-sends every `*.json` event in the outbox to the configured notifier and
returns `(delivered, total)`.

## Privacy

The event only includes identifiers, state, the harness/model, `source_rev`,
hashes, and a logical result reference. It never includes the brief objective,
prompt, tokens, provider keys, patches, logs, local paths, or `result.json`
contents. Lease material is hashed, not embedded.

## Tests

```
python3 -m unittest tests.test_landing_events -v
```
