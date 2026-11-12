# /v1 semver policy

hyvv's HTTP API is versioned by URL prefix.

- The current prefix is `/v1`.
- `/v1` only ever gets additive changes. Additive changes are a minor bump of the hyvv release (for example 0.1.15 to 0.2.0 is reserved for a larger but still compatible addition; a new optional field or a new capability in `GET /v1/version` is a minor bump).
- A breaking change requires a new prefix: `/v2` is served *alongside* `/v1` for at least one release, so existing coordinators keep working.
- `GET /v1/version` returns `api_version` (currently `"v1"`) and a `capabilities` list such as `artifact_fetch`, `priority_routing`, and `go_fallback`. Clients pin the contract at a git tag and run the conformance tests against a mock or a live host.
- Removing a field, changing a status code or error `code`, tightening a required field, or renaming a capability is breaking.
- The usage record schema `bee.usage/v1` and the artifact schema `hyvv.worker-artifact/1` follow the same rule: a breaking change gets a new schema id, served next to the old one for one release.
