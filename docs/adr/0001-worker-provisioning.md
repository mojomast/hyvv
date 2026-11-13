# ADR 0001 — Worker-host project provisioning

Status: proposed (draft PR, not deployed)
Scope: `hyvv` launcher; consumed by `bundlebees` M3.5

## Context

Every autonomous job currently assumes the worker already has the repository at a
known path and a matching entry in the launcher's authorized-project registry.
Neither exists for a new repository: `add-project` is a host admin script that
requires the fixture directory to exist already, and the launcher reads
`config.json` only at startup, so a registration would otherwise need a restart.

That makes an end-to-end run require manual SSH/PowerShell/Git/project-registration
steps — exactly the manual ceremony M3.5 exists to remove. The `rizzstack-smoke`
job (pinned `dde2aa42d09c05e43895b3120cb3312f765eb876`, contract sha256
`aa03cce9…`) is blocked on precisely this step.

## Decision

Provisioning is a **launcher-owned, policy-governed operation**. The coordinator
submits a declarative request; the worker host performs every filesystem and
registry action.

```
POST /v1/provision/project        (bearer token, same scope as the launcher API)
POST /v1/provision/cleanup
GET  /v1/provision/receipts
```

Request:

```json
{
  "repo": "mojomast/rizzstack-smoke",
  "source_rev": "<40 hex>",
  "profile": "smoke",
  "job_id": "smoke-20261113",
  "project": "rizzstack-smoke",
  "idempotency_key": "<uuid5(job, task, attempt)>"
}
```

Receipt: `schema`, `status`, `project`, `launcher_project`, `repo`,
`resolved_revision`, `workspace`, `profile`, `host`, `fixture`, `verified.head`,
`adopted`.

Authorisation comes from a host-enrolled policy file
(`provisioning-policy.json`, next to `config.json`), not from the request:

```json
{"schema": "rizzstack.provisioning/1",
 "profiles": {"smoke": {"repo_patterns": ["mojomast/rizzstack-smoke"],
   "workspace_root": "C:\\Users\\kyle\\orbit-worker-fixtures",
   "max_provisioned": 5, "ttl_hours": 24, "allow_cleanup": true,
   "git_actions": ["clone", "fetch", "checkout"],
   "task_scopes": ["smoke/answer.py"]}}}
```

`provisioning.enabled` defaults to **false**; a host that has not enrolled a
policy cannot provision at all, and the endpoints answer `501`.

## Invariants

1. **No request-selected paths.** The workspace is `workspace_root / project`,
   where `project` must match `^[a-z0-9][a-z0-9._-]{0,63}$`, contain no
   separator, and not be a Windows device name (`con`, `nul`, `com1`, …).
   Containment is checked with `commonpath` (native) and, on Windows, with an
   explicit drive-and-prefix check that is unit-tested on POSIX too.
2. **No reparse points.** Symlinks/junctions are refused before any write or
   delete, so a link inside the root cannot redirect cleanup outside it.
3. **No arbitrary execution.** The only child process is `git` with a fixed
   argument vector built from validated values; there is no shell, and no
   request field becomes an argument.
4. **Credentials stay on the host.** A repository-scoped read-only credential is
   supplied by the host's git credential helper. The launcher only asserts that
   the host declares one (`credential_ready_repos`); no token is stored, logged,
   returned, or placed in a URL. Errors are scrubbed for `ghp_`/`github_pat_`
   and `x-access-token`.
5. **Verify before registering.** `HEAD == source_rev`, `remote.origin.url`
   contains the repository identity, and the worktree is clean. Otherwise the
   journal ends `failed`/`checkout_mismatch` and nothing is registered.
6. **Durable registration, live without restart.** `save_projects` rewrites
   `config.json` atomically (tmp + `os.replace`) preserving all other keys, then
   the in-memory registry is updated. A failure leaves the previous file intact.
7. **Idempotent.** One journal record per idempotency key (fsynced, atomic
   replace) plus an `O_EXCL` per-key lock. Replaying a `ready` key returns the
   stored receipt; reusing a key for another repo/revision is a `409`
   `journal_conflict`; a concurrent duplicate is `409` `provision_in_progress`.
8. **Crash reconciliation.** A journal at `preparing` with an existing workspace
   is re-verified and adopted, never re-cloned. A registration that already
   exists and matches is adopted (`adopted: true`) instead of rewritten.
9. **Cleanup is bounded.** Only paths inside the profile root, only when the
   profile permits it, only when no attempt is running, and the journal record
   (evidence) survives the workspace deletion.

Lifecycle: `requested → authorized → preparing → ready`; `in_use`/`retiring`/
`cleaned` are driven by cleanup; `blocked`, `failed` and `uncertain` are terminal
until a human inspects them.

## Rejected alternatives

* **Extend the admin SSH channel with a `provision` op.** It would either accept
  a path/URL (arbitrary execution and arbitrary filesystem writes behind one
  forced command) or need the same policy engine anyway, and it could not update
  the running registry.
* **Coordinator-side clone over SSH.** Gives the coordinator host credentials and
  write access to the worker filesystem, and cannot register the project
  atomically. It also breaks the rule that the worker owns its own filesystem.
* **Restart the launcher after each `add-project`.** Rejected by the brief and by
  operational reality: a restart interrupts every running attempt.

## Consequences

* A launcher upgrade and a policy enrollment are **one-time privileged steps**;
  they are approval gates, not routine operations. Until the worker runs this
  code, the endpoints do not exist and the smoke stays blocked.
* `config.json` becomes launcher-written, so its formatting changes on first
  registration; every unknown key is preserved.
* `max_provisioned` and TTL accounting are enforced per profile; exceeding them
  is a `409`, not a silent eviction.
* Untested on the real Windows host: reparse-point detection, drive containment
  and credential-helper integration are covered by unit tests and pure-path
  tests, but the first real run must confirm them. That run is not authorized.

## Test map

`tests/test_provisioning.py` — policy refusals, exact-SHA checkout, duplicate
requests, crash-after-clone resume, lost acknowledgement adoption, wrong
HEAD/dirty checkout refusal, path traversal, symlink escape, cleanup containment
and in-use refusal, Windows name/drive rules.

`tests/test_provisioning_api.py` — routing and auth, durable registration that a
fresh process can read, four concurrent duplicates producing exactly one clone,
cleanup through HTTP, and that other projects in `config.json` are untouched.
