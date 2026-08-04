## 1. Freeze Provider Contract

- [x] 1.1 Define the repository owner, provider schema versions, exact content-identity reference rules, and fixed nine-component readiness set.
- [x] 1.2 Implement deterministic provider-contract construction and verification over checkout, database, source receipts, adjustment, panel, readiness, and companion snapshot identities.
- [x] 1.3 Add acceptance tests for mutable aliases, wrong kinds/versions, missing components, duplicate receipts, tampering, and price-only non-readiness.

## 2. Bind Provider Evidence

- [x] 2.1 Adapt full execution readiness to emit the exact nine components without weakening current blockers.
- [x] 2.2 Bind enrolled signer and trust-root identities for calendar, universe, status, corporate actions, and market rules.
- [x] 2.3 Require immutable source-response receipts and effective/availability times for every exact-panel component row.
- [x] 2.4 Bind separate raw execution-price and declared signal-price adjustment identities.

## 3. Companion Snapshot

- [x] 3.1 Extend the execution snapshot to include decision context, trading calendar, availability records, checkout, database, panel, adjustments, and receipt-set identities.
- [x] 3.2 Implement companion snapshot verification and fail before exposing readiness when any descriptor or artifact drifts.
- [x] 3.3 Add crash, replay, signature, post-cutoff, subset/superset panel, and content-tamper tests.

## 4. Consumer Integration

- [x] 4.1 Expose a read-only provider command that emits the versioned contract and verified readiness receipt for one exact panel.
- [x] 4.2 Run the RQGM consumer-contract fixtures against provider output and preserve `ready=false` for every unresolved authority.
- [x] 4.3 Capture migration, rollback, and verification evidence before allowing any E2 consumer use.
