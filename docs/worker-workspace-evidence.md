# Worker workspace readiness and patch evidence

For scoped coding work, configure a registered **local-path project** (including
projects created by governed provisioning). Host repository readiness and the
per-attempt sandbox checkout are separate checks. SDK-managed repo-only runs
cannot provide the measured baseline required for scoped patch collection and
are refused before model execution when `assigned_files` is non-empty.

## Readiness before model execution

The launcher exports the selected source revision and initializes a separate
Git repository in the attempt sandbox. It records:

- `workdir`: the sandbox directory passed to the agent;
- `local_base_rev`: the exported upstream source revision;
- `seed_commit`: the sandbox's initial commit, also held in `refs/hyvv/seed`.

An archive-seeded commit generally differs from the upstream source commit.
These identities must retain their mapping; they must not be compared as if
they were the same commit.

Directory preparation and Git initialization require an explicit zero shell
exit status. Missing status is a failure. Before launching the model, the
launcher verifies that the sandbox workdir is the Git top-level directory and
that its recorded seed resolves to a commit. Git resolves its own metadata, so
linked worktrees with a `.git` file are supported.

The actual SDK/model must honor the requested working directory. Validate that
boundary on the deployed backend as part of commissioning; a host repository
check alone is insufficient.

## Collection and failure semantics

Collection repeats the root/baseline verification and requires success from
staging, binary diff, changed-file enumeration, and file hashing. Git errors
never become ordinary empty patches. A missing or invalid seed is refused;
collection no longer falls back to the current `HEAD`, which could hide a
worker's committed changes. Changed paths are read from NUL-delimited Git output.

Failures carry a bounded, redacted `workspace_error` object in the durable
attempt journal and authenticated attempt response:

```json
{"code":"patch_collection_failed","stage":"workspace","exit_code":128,
 "detail":"fatal: not a git repository (or any of the parent directories): .git"}
```

Codes are `workspace_invalid` (setup/preflight) and `patch_collection_failed`
(collection). `stage` identifies the failing operation. `exit_code: null` means
no usable command status was obtained, or validation/transport failed before a
status existed. The controller records a failed attempt and canonical failed
terminal event. A failed collection does not create a normal result artifact.

A valid clean repository still produces an empty patch. Whether a no-change
result satisfies a task is the coordinator's acceptance decision. Worker commits
remain visible because the diff uses the saved seed rather than the final HEAD.
Successful result manifests also expose `observed.seed_commit`.

## Updating a worker and validating the fix

Use the approved idle-update process. Older in-flight coding attempts without
a recorded seed cannot safely resume patch collection under these checks;
drain the worker before updating. Verify the loaded source/artifact identity,
not just the shared `0.1.16` version string.

Local regression command (real Git and controller/event path; SDK/model fixture):

```bash
python -m unittest tests.test_workspace_evidence tests.test_acceptance_class \
  tests.test_assigned_scope tests.test_agy_cua tests.test_landing_events -v
```

For live commissioning, use the existing bounded job authorization and a
disposable registered project. Capture the running build, backend, sandbox ID,
exported revision, seed, requested agent cwd, actual agent cwd/Git root, patch
base and accepted diff. Confirm one real code change applies to its source
baseline and reaches verification/integration and validated RETURN.

Exercise broken-workspace and invalid-seed cases only in disposable fixtures.
They must report failed collection/attempts, never successful empty patches.
Before scaling dispatch after a workspace failure, correct its root cause and
confirm readiness on that same worker path. Preflight failures terminate before
model execution; a new attempt is not evidence that the environment was repaired.
