# Bee landed events (`rizzstack.bee.landed.v1`)

hyvv records one durable terminal event per attempt. The event says that a bee
landed: the attempt reached `succeeded`, `failed`, or `cancelled`, and the
launcher has persisted the observed result (when one exists). The payload is the
canonical `rizzstack.bee-landed/1` shape: identities, state, revision, lease and
result references, never briefs, tokens, patches, logs or host paths.

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

`landed_event_id(attempt_id, state, transition)` is deterministic, so replaying
the same terminal observation addresses the same file. Without an explicit
`terminal_transition_id`, the transition defaults to the terminal state.

## CloudEvents envelope

```json
{
  "specversion": "1.0",
  "id": "urn:rizzstack:bee-landed:0123456789abcdef0123456789abcdef",
  "source": "hyvv://worker-1",
  "type": "rizzstack.bee.landed.v1",
  "time": "2026-11-14T12:34:56.789012Z",
  "subject": "att_0123456789abcdef",
  "datacontenttype": "application/json",
  "dataschema": "https://rizstack.dev/schemas/bee-landed/v1",
  "data": {
    "schema": "rizzstack.bee-landed/1",
    "feature_id": "feat_1",
    "job_id": "job_1",
    "task_id": "task_1",
    "attempt_id": "att_0123456789abcdef",
    "coordinator": {"identity": "orbit", "session": "sess_1"},
    "backend": {"harness": "opencode", "model": "deepseek-v4.1-flash"},
    "state": "succeeded",
    "source_rev": "0123456789abcdef0123456789abcdef01234567",
    "lease": {
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "assigned_files": ["src/module.py", "tests/test_module.py"]
    },
    "result": {
      "available": true,
      "artifact_sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
      "reference": "hyvv://attempts/att_0123456789abcdef/result"
    },
    "correlation_id": "corr_1",
    "causation_id": "cmd_1"
  }
}
```

`correlation_id`, `causation_id` and `terminal_transition_id` are optional. The
`data` object is closed; CloudEvents extension attributes belong at the envelope
root, not inside `data`.

### Field sources

| Field | Source |
| --- | --- |
| `id` | `urn:rizzstack:bee-landed:` + `sha256(attempt_id:state:transition)[:32]` |
| `source` | `launcher_id`, then `worker_id`, `source`, `host_id`, then the host name, as a `hyvv://` URI |
| `time` | the attempt's persisted terminal `updated_at` |
| `subject` | the launcher attempt id |
| `feature_id`, `job_id`, `task_id` | `brief.feature_id` / `brief.feature.id`, `brief.job_id` / `brief.job.id`, `brief.task_id` / `brief.task.id`; the attempt id when a standalone attempt has none |
| `attempt_id` | the launcher attempt id, falling back to `brief.attempt_id` |
| `coordinator.identity` | `brief.coordinator.id`, `brief.coordinator` string, or `brief.coordinator_id`; the launcher identity when absent |
| `coordinator.session` | `brief.coordinator.session`, `brief.coordinator_session`, `brief.session_id`, or `brief.session`; omitted when absent |
| `backend.harness` | `agy` for `agy-cua`, `check` for check jobs, otherwise `opencode` |
| `backend.model` | the attempt model, else `brief.model`, else the launcher default; omitted when absent |
| `source_rev` | `brief.source_rev`, lowercased and required to be 40 or 64 hex; a deterministic 64-hex value for legacy attempts without one |
| `lease.sha256` | `brief.lease_sha256`, else a nested `brief.lease.sha256`; otherwise SHA-256 of the canonical object `{"assigned_files": sorted(files), "source_rev": source_rev}` |
| `lease.assigned_files` | `brief.assigned_files`, else `brief.lease.assigned_files`; each entry is a repository-relative path |
| `result.available` | whether `<attempt_id>.result.json` exists and was readable |
| `result.artifact_sha256` | SHA-256 of the result artifact bytes; omitted when no artifact exists |
| `result.reference` | logical `hyvv://attempts/<attempt_id>/result`, never a filesystem path; omitted when no artifact exists |
| `correlation_id`, `causation_id` | `brief` or the attempt record, when present |

## Idempotency and conflicts

The identity of a landed event is `(attempt_id, terminal state)`.

- A duplicate observation of the same attempt and state returns the existing
  event. It does not write a second file and does not send a second notifier
  signal.
- Evidence is compared without the event time, so re-observing the same state
  later is idempotent.
- If the outbox already contains an event for the attempt with another terminal
  state, or with different evidence for the same state (for example another
  `source_rev`, lease digest, backend or result artifact hash), the write is
  refused with `LandedEventConflict`. The existing event is left untouched.
- Conflicting evidence found while recovering at startup is recorded in
  `Launcher.landing_conflicts`; startup continues and the existing event is not
  overwritten.

## Production points

`Launcher._set()` emits the event immediately after it persists a terminal
attempt state. `_finish()` and the check-run controller write the result artifact
before calling `_set()`, so a successful event normally carries the artifact
hash. Failed and cancelled attempts may have no result artifact; their event has
`result: {"available": false}` and no artifact fields.

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

The event only includes identifiers, state, the backend harness/model,
`source_rev`, lease digests, repository-relative assigned file paths, and logical
result references. It never includes the brief objective, prompt, tokens,
provider keys, patches, logs, local paths, or `result.json` contents. Lease
material is hashed, not embedded.

## Tests

```
python3 -m unittest tests.test_landing_events -v
```

When `WORKFLOW_SRC` names a pinned workflow checkout, every event is validated
with that checkout's canonical `tools/bee_landed.py` and
`schemas/bee-landed.schema.json`. Without it, the tests enforce the same contract
locally and fail closed on any missing or extra field.
