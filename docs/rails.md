# Worker Rails replication (local implementation, opt-in)

Hyvv records worker observations in one `rails.sqlite` under its private local `data_dir`. Its existing launcher process reads flushed attempt JSONL and the persisted landing receipt; it needs no Hermes installation, sidecar, callback, or second scheduler. `worker_report` is the only producer authority. A tool completion or terminal receipt is not verification or acceptance.

## Private configuration

`rails_enabled` must be the JSON boolean `true` to enable recording. Export additionally requires **both**:

- `rails_read_token_file`: private file containing a distinct high-entropy read credential (32–512 printable non-space ASCII characters). Set `HYVV_RAILS_READ_TOKEN_FILE` to override the file location. Give this file owner-only permissions/ACL; values never belong in config, source control, or a URL. Reuse of the launcher's control credential is refused. Restart the launcher to rotate this credential or change the allowlist.
- `rails_read_peers`: list of exact numeric coordinator IP addresses. The handler checks the TCP peer, ignores forwarded-for headers, requires the numeric listener `Host` (including port, unless 80), and refuses browser `Origin`. Even loopback requires enrollment and the read credential.

Enroll a fixed numeric launcher endpoint with the coordinator using the existing protected launcher transport. This change adds no transport encryption: don't expose a cleartext bearer on an unprotected link. No credential/peer configuration means no export. Turning off `rails_enabled` on restart preserves existing source and ledger files. Do not place SQLite on a shared/network volume.

### Time-limited canary reader

Use an isolated canary launcher: the read credential covers its entire Rails chain. Give the canary coordinator a fresh read token, distinct from the control token, and list only that coordinator in `rails_read_peers`. In the private canary config set `rails_read_expires_at` to the **end of the approved window** as a JSON integer in UTC Unix seconds (0–253402300799 inclusive). Use a synchronized worker clock. For example, an approved cutoff of `2030-01-01T00:00:00Z` would be `"rails_read_expires_at": 1893456000`. This is an absolute timestamp, not a duration or a millisecond count.

Omitting the key keeps the existing reader without an expiry; an explicit invalid value (including `null`, a string, a boolean or a fraction) disables reads. The launcher loads the deadline with the token at setup and checks its clock on each Rails GET. At or after the cutoff, both endpoints return non-retryable `401 auth_required` before accessing the ledger. Preserve the last committed cursor on refusal. Restarting with the same deadline does not extend it; for a new approved window, install a fresh read token and new deadline before starting the canary launcher.

## Pull protocol

Both endpoints are `GET` on the existing listener. Only the replication reader can use them, and it cannot list attempts, dispatch, change config, read raw events, or use any other control route.

| Path | Response |
|---|---|
| `/v1/rails/head` | `chain_id`, `head` (`{seq,hash}` or `null`), `machine_alias: worker-1`, `origin`, `recording`, `completeness`, safe warning codes. A reachable ledger may have partial recording. |
| `/v1/rails/events?after=0&limit=100` | `rizzstack.rails-export/1`: `chain_id`, requested exclusive `after`, original event objects serialized in canonical bytes, `next_cursor` (last returned seq, or `after` if empty), `head`, `has_more`. Maximum 500 rows, 64 KiB per record, 1 MiB per response. |

Pages are SQLite read snapshots opened with `mode=ro` and `query_only`. GET never creates the ledger, reads a worker log/receipt, replays an event, or advances a source/consumer cursor. Re-request a disconnected page from the last **committed** origin cursor, compare the enrolled chain and verify contiguous seq/prev-hash/hash before importing it. A cursor beyond head gets `409 reset_required`; a missing prefix yields `409 source_gap`; off-policy or inconsistent rows yield `409 source_fork`; unreadable storage gets `503 source_unavailable`. Do not skip the prefix or auto-trust a replacement chain. An exported head is not a signature or an independent checkpoint.

## Coverage, privacy and recovery

- Cua 0.5.0 `AgentEvent` has `tool_id`, `tool_title` and `tool_status`. Parser v1 accepts structured fields plus the historical `worker:tool_call`/`worker:tool_update` forms evidenced in workflow's `docs/failure-evidence-2026-11-16/logs`. R00's earlier synthetic trace contains only state/turn events. `tests/test_rails_emission.py` and `tests/test_rails_native.py` use synthetic records; no production transcripts or fixtures are required.
- Only exact tool labels (e.g. `[tool execute] bash`), status enums and bounded opaque call IDs survive. An in-progress update is not a finish. Missing/unrecognized IDs produce row-local `unpaired:` references; do not match by adjacency. No command line, path, prompt, message, thought, argument, output, native title, hostname or private deployment ID enters Rails. Command family and duration are unknown when the source doesn't report them. Source identity uses launcher-minted `att_` hex IDs; other client-selected attempt IDs remain private and leave coverage partial. A new running state opens a distinct run lane within the attempt. Other grouping fields stay unknown.
- Final numeric usage comes from the allowlisted `rizzstackusage` receipt extension and is keyed once per attempt. Missing counts stay null. The legacy agy receipt substitutes zero for absent counts; its ambiguous zeros therefore stay null in Rails. No inferred cost and no replay-driven changes to bee-landed/1.
- Native and startup ingestion use the same parser, worker authority and stable `(attempt, log generation, lseq)` source keys. Rows, checkpoints and the byte cursor commit together. A JSONL trailing fragment waits for its newline; malformed complete rows produce a safe `source.error`. Observed rotation/shrink produces `source.gap` and a new generation. This is an append-only source protocol: a privileged in-place rewrite preserving file identity, size and sampled anchors is not reliably detectable. Keep off-host head checkpoints.
- Recording refusal leaves the execution/landing path intact. After repairing local storage, restart to replay durable logs and terminal receipt observations. A malformed/missing journal, unsupported attempt identity or retained gap leaves completeness partial; never treat that as complete campaign evidence. Original journal privacy/access rules remain important: the older control API and private source files are not a Rails projection.

The stdlib worker runtime is the four R02 modules from BundleBees, pinned by `contrib/rizzstack_rails/vendor-manifest.json` and included in both wheel and sdist. Verify it offline from the Hyvv root (the sibling checkout must contain the pinned Git object):

```sh
pin=$(python -c 'import json; print(json.load(open("contrib/rizzstack_rails/vendor-manifest.json"))["pin_label"])')
python scripts/vendor_rails.py ../bundlebees --rev "$pin" --check
```

Omit `--check` only for a deliberate refresh to a selected full commit. No network or manually edited `PYTHONPATH` is needed at runtime. Test with `python -m unittest tests.test_rails_emission tests.test_rails_native -v`; the same targeted command is included in the Windows CI job.
