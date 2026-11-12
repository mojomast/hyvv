# Planned: global bee-slot leases

Status: **planned**. Not implemented in launcher v0.1.15. Advertise the
`leases` capability from `GET /v1/version` once the routes ship.

hyvv is the shared dispatch point for the global bee-slot cap. Orbit and
bundlebees are clients of this API. MR.INDIA owns the client adapters and
their contract tests; this tree owns the versioned schemas.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/leases` | Acquire. Body: `coordinator`, `project`, `role`, `count`, `ttl`, `purpose` |
| `POST` | `/v1/leases/{id}/renew` | Extend a live lease (optional new `ttl`; otherwise the original ttl from now) |
| `DELETE` | `/v1/leases/{id}` | Release the slots immediately |
| `GET` | `/v1/leases` | List live leases and current cap usage |

`role` is `work`, `verify`, or `coordinator`. `purpose` is `work`, `verify`, or `ci`.

Schemas: `hyvv.lease/1` in `schemas/lease.json`, plus `lease-create.json`,
`lease-renew.json`, and `lease-list.json`.

## Caps

Taken from host config, not hardcoded in coordinators:

- Global cap: default **12** (the combined cap across coordinators).
- Per-coordinator caps, when set in config.
- `purpose: ci` is limited to **2** slots inside the global cap.

Expired leases are released automatically when their TTL elapses. Until then,
`count` occupies cap.

## Refusal

When a request would exceed the global cap, a coordinator cap, or the CI cap,
the host returns **429** with `code` / `error` **`capacity_exhausted`** and a
**`Retry-After`** header (seconds until a slot is expected to free, or the
shortest live TTL). The body is the standard error envelope. This is retryable.
