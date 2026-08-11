## Context

`stock_data` already has immutable price receipts, forward decision-context capture,
corporate-action capture, historical-universe attestation, composite readiness, and a
six-artifact execution snapshot. Those capabilities are uneven: current full readiness
correctly blocks unenrolled universe publishers, activity-proxy status, and an absent
official rulebook. RQGM now needs one stable provider-side contract that binds these
artifacts without gaining permission to repair or reinterpret provider data.

The `stock_data` repository owns provider collection, storage, signing, and snapshot
materialization. RQGM owns only consumer verification and must remain data-blocked when the
provider cannot satisfy the contract.

## Goals / Non-Goals

**Goals:**

- Define one versioned, content-addressed provider contract compatible with the RQGM
  consumer contract.
- Bind checkout tree, immutable database, source receipts, adjustment policy, exact panel,
  readiness report, and companion snapshot.
- Require all decision and execution components independently, including a signed trading
  calendar and availability records.
- Provide executable acceptance tests for tamper, alias, incomplete-component,
  exact-panel, signature/trust, and price-only failure cases.

**Non-Goals:**

- Declare unavailable historical data ready.
- Let RQGM modify provider internals or mint provider receipts.
- Treat current snapshots, activity, inferred membership, or undocumented rules as
  historical authority.
- Implement exchange publisher enrollment or backfill missing records in this change.

## Decisions

### Use content identities rather than filesystem paths

Every bound artifact uses a lowercase SHA-256 content identity and explicit schema version.
Paths may locate bytes but never identify them. This prevents `latest.sqlite`, a checkout
directory, or another mutable alias from satisfying the contract.

Alternative: bind resolved paths and modification times. Rejected because two different
byte sets can reuse both and because cross-repository replay would not be deterministic.

### Keep nine independent readiness components

The provider contract fixes execution prices, signal prices, decision context, trading
calendar, universe, instrument status, corporate actions, market rules, and availability
records as the complete component set. Aggregate `ready=true` is valid only when every
component is ready and has no blockers.

Alternative: fold context/calendar/availability into price or universe readiness. Rejected
because it hides which evidence is absent and permits price-only upgrades.

### Separate contract construction from readiness authorization

A provider contract can be constructed only from content references, but construction does
not itself assert readiness. The readiness report and companion snapshot must be verified
against the contract, including exact component and panel identities. Signed authority
requires an enrolled trust-root identity; self-signed or unknown-key artifacts remain
blocked.

Alternative: infer readiness when all references are present. Rejected because references
can point to blocker reports or untrusted bytes.

### Preserve blockers until provider evidence exists

Current explicit blockers are retained. Acceptance tests prove they cannot be bypassed by
renaming artifacts, supplying only current context, or filling records after their decision
cutoff. Capability implementation and historical backfill proceed in later tasks under this
change.

## Risks / Trade-offs

- [Contract is stricter than current provider output] -> Version the new contract and keep
  current readiness blocked until each component is migrated.
- [Hashes prove content, not authority] -> Bind signer and trust-root identities and verify
  enrollment separately from content hashing.
- [Historical coverage is expensive] -> Evaluate one exact panel at a time and report
  component blockers without weakening full readiness.
- [Existing dirty work may overlap] -> Add isolated contract modules/tests and avoid
  rewriting current collectors in the contract-definition slice.

## Migration Plan

1. Freeze the provider schema and acceptance fixtures.
2. Add content-addressed provider contract construction and structural verification.
3. Adapt current readiness and execution snapshot outputs behind the contract without
   changing their existing blocker semantics.
4. Enroll external calendar, universe, status, corporate-action, and rulebook authorities
   in separate evidence-producing tasks.
5. Expose a companion receipt only after all nine components and the exact panel verify.

Rollback removes only the new contract consumer/export path; existing readiness and
snapshot commands remain unchanged.

## Open Questions

- Which external keys will be enrolled for exchange calendar/rulebook and historical
  universe/status publication? Until configured, those components remain blocked.
