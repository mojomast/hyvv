# ADR 0002: signed dispatch tickets at the attempt boundary

Status: **accepted** (Kyle, chat t1650u): Ed25519 tickets, rolled out `ticket_mode` off -> report -> enforce. Open for the owner: where the coordinator signing key lives and its rotation rule. Wave C of the Nov 24 next-level review ([rizzstack #17](https://github.com/mojomast/rizzstack/pull/17)); generalizes `NEXT.md` W3-1.

## Why hyvv owns this ADR

The guarantee is "an ungated create is impossible by construction". Only the component that creates
attempts can make that true, and that is hyvv's `POST /v1/attempts`. Issuing a ticket is a bundlebees
preflight concern and the ticket's JSON shape belongs to the wave B contracts in workflow, but the
refusal, the key handling and the replay cache live here, so the decision lives here too.

## Problem

Wave 0 made the coordinator run `preflight` before dispatch, and Orbit now *warns* about ungated
local-worker creates (orbitdesktop #88). But hyvv accepts any authenticated create. Orbit, the
control plane and bee-ci can still create attempts that never passed the risk floor, the contract
gate or the research gate. A warning counts them; it does not stop them.

## Decision (proposed)

1. **Ticket.** `preflight` returns, on success, a compact JWS (`alg: EdDSA`, Ed25519) over:
   - `brief_sha256`: RFC 8785 (JCS) digest of the brief exactly as it will be POSTed
   - `project`, `source_rev`, `assigned_files_sha256`, `risk_tier`, `contract_sha256` (or null)
   - `assignment`: the worker binding (W3-1's case; null when any worker may run it)
   - `aud` (the hyvv instance id), `iat`, `exp` (at most 10 minutes later), `jti` (random, single use)
   - `kid`: the issuer key id
2. **Verify in hyvv.** `POST /v1/attempts` requires `brief.ticket`. hyvv checks the signature
   against configured issuer public keys, `aud`, `exp`, that `jti` was not seen (a bounded on-disk
   replay cache, kept until `exp`), and that the JCS digest of the received brief (minus `ticket`)
   equals `brief_sha256`. Any failure is a 403 with one of `ticket_missing`, `ticket_invalid`,
   `ticket_expired`, `ticket_replayed`, `ticket_brief_mismatch`. Fail closed: an unreadable key
   config refuses every create.
3. **Asymmetric keys.** hyvv holds only public keys. A component that can reach hyvv cannot mint
   tickets. The coordinator's private key is a host secret like the other keys (never in a repo).
4. **One path in.** Orbit, the control plane and bee-ci call `bundlebees preflight` (CLI or the
   future `bundlebees mcp`) to get a ticket; none of them signs.
5. **Rollout mode.** `ticket_mode: off | report | enforce` in hyvv config, `report` first: accept
   and record why the ticket would fail. Mirrors bundlebees `CONTRACT_GATE_MODE`.

## Options considered

| Option | For | Against |
|---|---|---|
| Ed25519 JWS (chosen) | hyvv cannot forge; standard tooling; small | key distribution step |
| HMAC shared secret | simplest | every holder can mint tickets, so a compromised Orbit could self-approve |
| mTLS identity only | no new token | proves who called, not that preflight ran on *this* brief |
| hyvv runs preflight itself | one place | hyvv would need bundlebees' state, contracts and research gate; breaks layering |

## Consequences

- hyvv gains a small verifier and a replay cache; `/v1` stays additive (`ticket` is a new field).
- Retries need a fresh ticket (`jti` is single use); `_backoff` resumes inside one attempt and does
  not create a new one, so HY-2 is unaffected.
- The ticket digest needs one canonical JSON everywhere: wave D's RFC 8785 core fixes the current
  `ensure_ascii` split (hyvv `launcher.py:259` vs bundlebees `tdd.py:685`). Wave C depends on D's
  canonical-JSON slice.

## Exit

`ticket_mode: enforce` on the worker host, and a create without a valid ticket is refused in a test
that drives Orbit's local-workers route, the control plane and bee-ci.

## Decisions for the owner

- Approve the ticket design (Ed25519 recommended over HMAC).
- Where the coordinator signing key lives, and the rotation rule.
- The date `ticket_mode` moves from `report` to `enforce`.
