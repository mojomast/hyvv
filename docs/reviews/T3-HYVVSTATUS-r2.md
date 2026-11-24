VERDICT: APPROVE

## Findings

1. **minor** — `contract/v1/openapi.yaml:531-557` (`GET /v1/landing/events`): the operation documents `200`, `400`, `401`, `409` and `503`, but not the `501` the handler returns when the landing-export module is unavailable (`launcher.py:2967-2968`). The response list is therefore not exhaustive. Concrete fix (follow-up; not required for this slice): add `"501": description: Landing export is unavailable on this host` alongside the other responses.

2. **minor** — `contract/v1/openapi.yaml:519-526` (`GET /v1/provision/capabilities`): the documented response schema lists only `schema`, `enabled` and `profiles`, while the handler returns additional fields (`backend`, `states`, `supports_cleanup`, `supports_receipt_lookup`, `mirrors`, `idempotency`; `launcher.py:2921-2928`, `provisioning.py:772-782`). The schema is not wrong (additional properties are allowed) but is incomplete. Concrete fix (follow-up): add the optional properties with their types. No test depends on this.

3. **minor** — Stale version prose outside the generated blocks: `README.md:31` ("not in launcher 0.1.15") and `contract/v1/README.md:65` ("Launcher 0.1.15 does not enforce it"), plus `contract/v1/schemas/version.json:5` ("Implemented in launcher 0.1.15"). The README files were in this lease only for the generated status blocks, and `schemas/version.json` was outside it; the brief forbids out-of-spec edits, so leaving these untouched is correct here. Concrete fix for the coordinator in a separate change: replace the pinned old version with "the launcher" or `0.1.18`. The only old-version string in a test is an informational assertion message (`contract/v1/tests/test_project_state_gate.py:299`), which is also outside this lease and does not affect behaviour.

4. **minor (informational, no action for this PR)** — Four further provisioning routes are registered by the launcher but remain absent from `STATUS.json`/`openapi.yaml`: `GET /v1/provision/receipts`, `GET /v1/provision/receipts/{receipt_id}`, `POST /v1/provision/project`, `POST /v1/provision/cleanup` (`launcher.py:2930-2938`, `launcher.py:3024-3036`). Under the approved probe they answer `501` because provisioning is disabled (`build_provisioner` returns `None`, `launcher.py:2720-2725`), so SPEC's "leave any route you cannot confirm out" applies; labelling them `launcher-0.1.18` would fail `test_launcher_labels_match_the_runtime`. Concrete fix (optional follow-up): document them once the probe can enable provisioning, or introduce a label for "implemented but disabled". Do not add them as `launcher-0.1.18` under the current test.

## Acceptance criteria and evidence

- **Label rename (SPEC 1).** `contract/v1/STATUS.json:3` carries the `launcher-0.1.18` key and text, all 15 implemented endpoint entries and 4 implemented feature entries name it (`STATUS.json:9-24,31-33,40`), `contract/v1/status.py:17` maps it to "Implemented in launcher 0.1.18", and all 15 `x-hyvv-status` lines in `openapi.yaml` name it. The prose/example inside `openapi.yaml` (`info.description`, `/v1/version` example) was updated consistently. `test_every_label_is_one_of_four` (test file line 54) and `test_version_is_labelled_implemented_in_the_launcher` (line 59) compare against `LAUNCHER`, derived from `VERSION = "0.1.18"` at `launcher.py:38`.

- **New routes (SPEC 2).** Three routes were added, each with a live `200` from the in-process probe and a handler line:
  - `POST /v1/route` — `launcher.py:3014-3022` (probe: `200`).
  - `GET /v1/provision/capabilities` — `launcher.py:2921-2928` (probe: `200`, `enabled:false` on a host without provisioning).
  - `GET /v1/landing/events` — `launcher.py:2966-2985` (probe: `200`).
  `openapi_ops()` yields exactly the 20 entries in `STATUS.json` (15 launcher, 1 reference, 4 specified), with matching labels and no unlabelled operation, so `test_openapi_matches_status_json` also passes.

- **No promotion (SPEC 3).** `GET /v1/attempts/{attempt_id}/artifact` remains `reference` (probe: `404 not found`), all four lease entries remain `specified` (probe: `404 not found` / unsupported-method `501`), and the project-state gate feature remains `reference`; the probe confirms they are still absent from the handler.

- **README blocks (SPEC 4).** `python3 -m contract.v1.status --write` exits 0 and leaves the tree unchanged; both generated tables name `launcher-0.1.18` and include the three new routes. `test_readme_tables_are_current` compares both blocks to `status.block()`.

- **Approved test untouched and not weakened.** The only test-file hunks in the packet are the coordinator's RED version: the probe rule (`501`, or `404` with `error: not found`), the route-loop and all other assertions are unchanged; only the fixed `launcher-0.1.15` label was replaced by `LAUNCHER`. No gate, sandbox, check or assertion was relaxed, and no test was skipped.

- **Scope and hygiene.** The change is confined to the five leased files; the approved test is not edited by the bee; no new dependency, network call, credential, host, IP or private absolute path appears in the added text. `STATUS.json` keeps two-space JSON and its existing key order; the README diffs are generated table rows only.

## Test commands

The review sandbox has no `pytest` installed and network access is prohibited, so the two mandated pytest invocations fail before collection; the suite is unittest-based, and I ran the equivalent runners.

- `timeout 900 python3 -m pytest -q -p no:cacheprovider contract/v1/tests/test_status_labels.py` → exit 1 (`No module named pytest`).
- `timeout 900 python3 contract/v1/tests/test_status_labels.py -v` → exit 0; `Ran 7 tests ... OK`.
- `timeout 2400 python3 -m pytest -q -p no:cacheprovider` → exit 1 (`No module named pytest`).
- `timeout 2400 python3 -m unittest discover -q -s . -t .` → exit 0; `Ran 606 tests ... OK (skipped=7)` (matches the supplied 606-case baseline with no new failures).
- `timeout 120 python3 -m contract.v1.status --write` → exit 0; `git status --short` empty afterwards (README blocks already current).
- In-process launcher probe (same config as the approved test) → exit 0; new routes `200`, reference/specified routes `404 not found`/`501`, and the four unlisted provisioning routes `501`.
