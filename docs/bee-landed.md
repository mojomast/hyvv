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

### Usage extension attribute (`rizzstackusage`)

Optionally, the envelope carries the attempt receipt's usage block as the
CloudEvents extension attribute `rizzstackusage` (envelope root, sibling of
`id`, `source`, `data`; never inside `data`). The launcher emits the attribute
today, but nothing consumes it yet: no coordinator-side reader exists in the
current stack. Two consumer changes are explicitly pending — the bundlebees
envelope allowlist still has to learn `rizzstackusage`, and the orbitdesktop
outbox source still has to cross to this event type — so until both land the
attribute rides the event unread, and consumers that ignore unknown envelope
attributes are unaffected.

```json
{
  "specversion": "1.0",
  "type": "rizzstack.bee.landed.v1",
  "id": "urn:rizzstack:bee-landed:0123456789abcdef0123456789abcdef",
  "rizzstackusage": {
    "input": 120,
    "output": 45,
    "cache_read": 30,
    "cache_write": 12,
    "reasoning": 8,
    "thinking": 5,
    "total": 195,
    "tool_calls": 7,
    "stop_reason": "end_turn",
    "model": "zoo:glm-5.3-flash",
    "route": "zoo"
  },
  "data": { "schema": "rizzstack.bee-landed/1" }
}
```

Schema evolution: `data` is closed under the frozen `rizzstack.bee-landed/1`
schema, so a `usage` field inside `data` would be a breaking change requiring a
new schema id and a repin of the canonical validator. The envelope-root extension
attribute is the additive path the CloudEvents spec (and this document, above)
already provides: consumers that ignore unknown attributes are unaffected, the
`data` field set stays frozen, and no version bump or repin is needed.

Attributes are optional and scrubbed:

| Attribute | Source | Rule |
| --- | --- | --- |
| `input`, `output`, `cache_read`, `cache_write`, `reasoning`, `thinking`, `total` | the receipt's `observed.usage` (provider usage object or JSON string), normalized onto these canonical counts | numeric, non-negative integers under the magnitude bound: a count at or above 1,000,000,000 is refused (absent, never clamped — no single run consumes a billion tokens or issues a billion tool calls); a count the provider never reported is absent, never zero-filled (a zero the provider actually reported is kept) |
| `tool_calls` | the receipt's `observed.tool_calls` | non-negative integer under the same magnitude bound, else absent |
| `stop_reason` | the receipt's `observed.stop_reason` | at most 64 characters. A known launcher stop reason (`end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `tool_calls`, `content_filter`, `length`, `stop`, `error`, `timeout`, `aborted`, `cancelled`, `canceled`, `interrupted`, `refusal`, `unknown`, `other`) passes lowercased. Any other value must survive strict scrubbing: it is refused — the field stays absent — when the redactor would rewrite it or when it is a URL, a host/IP, a host:port or a filesystem path |
| `model` | the attempt model, then the receipt's `source.model` | identifier only. The value passes through the redactor and the same URL/path refusal: a value the redactor would rewrite, or one that looks like a URL, a host/IP, a host:port or a filesystem path, is refused and the field stays absent (a dotted version segment such as `v4.1` is kept) |
| `route` | provider path that served the run: `zoo` / `go` / `deepseek` from the model prefix or allow-list entry, `free` for keyless free-pool entries, `agy` for agy-cua | absent when it cannot be derived |

Legacy receipt spellings are accepted as input and never emitted: each is
mapped onto the canonical count it names, and the canonical name wins when both
spellings appear in one usage object.

| Legacy spelling (accepted as input only) | Emitted key |
| --- | --- |
| `input_tokens`, `prompt_tokens` | `input` |
| `output_tokens`, `completion_tokens` | `output` |
| `cache_read_tokens` | `cache_read` |
| `cache_write_tokens`, `cache_creation_input_tokens` | `cache_write` |
| `reasoning_tokens` | `reasoning` |
| `thinking_tokens` | `thinking` |
| `total_tokens` | `total` |

Alias forms without separators (`inputTokens`, `cacheRead`) normalize the same
way; the bare `input` / `output` / `total` shapes are themselves canonical.

Everything else the receipt's usage block may carry — prompts or completion
text, tool arguments, file lists, provider quota state, daily totals, local log
paths, keys — is dropped here, never copied through. A receipt without at least
one token count yields no attribute at all, so events for attempts without usage
are byte-identical to the pre-usage shape.

Idempotence is unchanged: the attribute lives outside the conflict evidence, the
event id stays `(attempt_id, terminal state, transition)`, and a re-observed
attempt returns the existing durable event whether or not the new observation
carries usage. The outbox file is written once; the pending coordinator-side
consumer will deduplicate by the attempt key, so a re-emitted event never
double-counts.

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
| `rizzstackusage` | the receipt's scrubbed usage block (see above); absent without token counts |

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
hash — and, when the receipt's `observed.usage` holds token counts, the
`rizzstackusage` attribute. Failed and cancelled attempts may have no result
artifact; their event has `result: {"available": false}` and no artifact fields
(and no usage attribute unless their receipt recorded token counts).

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
result references. It never includes the brief objective, prompt text, provider
keys, patches, logs, local paths, or `result.json` contents. Lease material is
hashed, not embedded. The one exception is the `rizzstackusage` attribute, which
adds only the scrubbed numeric counts and identity fields described above —
`tool_calls`, a validated-or-refused `stop_reason`, and the model/route
identity; every other part of the receipt's usage block (quota state, daily
totals, local log paths, prompt or completion text) is dropped before the event
is built.

## Tests

```
python3 -m unittest tests.test_landing_events -v
python3 -m pytest tests/test_bee_landed_usage.py -q
```

When `WORKFLOW_SRC` names a pinned workflow checkout, every event is validated
with that checkout's canonical `tools/bee_landed.py` and
`schemas/bee-landed.schema.json`. Without it, the tests enforce the same contract
locally and fail closed on any missing or extra field.
