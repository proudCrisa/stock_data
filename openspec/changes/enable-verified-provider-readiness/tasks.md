## 1. Freeze Reachability And Trust Boundaries

- [x] 1.1 Add a characterization test proving the current materialize/export path cannot emit `ready=true`, including all five admitted signed components.
- [x] 1.2 Build an isolated nine-component positive fixture with ephemeral test trust, exact-panel receipts, separate raw/signal identities, and no production registry authority.
- [x] 1.3 Add negative fixtures for caller-forged readiness, unknown/self-signed keys, component-byte drift, missing records, post-cutoff evidence, and eight-of-nine completion.

## 2. Reconstruct Intrinsic Components

- [x] 2.1 Reconstruct canonical raw execution-price and declared signal-price payloads from the immutable database, adjustment identities, exact panel, and bound source receipts; reject byte or coverage mismatch.
- [x] 2.2 Reconstruct decision-context payloads from captured database rows and original receipts; reject inferred, activity-proxy, duplicate, or post-cutoff context.
- [x] 2.3 Make intrinsic component verification emit deterministic evidence identities, coverage counts, and explicit blockers without accepting caller component booleans.

## 3. Complete External And Availability Authority

- [x] 3.1 Keep calendar, universe, status, corporate-action, and market-rule admission restricted to the production enrolled registry and prove ephemeral test keys cannot load through it.
- [x] 3.2 Extend availability records to bind component, panel entry, recomputed canonical record hash, source receipt, event/availability time, and the phase-specific signed calendar cutoff; prices use the signed close-to-next-session-decision window while decision inputs use the same-session decision cutoff.
- [x] 3.3 Verify complete one-to-one availability closure over the other eight components and reject orphaned, duplicate, missing, unrelated-hash, or post-cutoff rows.

## 4. Freeze Execution-Complete Market Rules

- [x] 4.1 Define and validate a versioned dated market-rule payload covering scope, validity, lot size, T+1, listing age, suspension, price limits/exceptions, fees, taxes, slippage, and order lifecycle.
- [x] 4.2 Bind the complete canonical rule bytes and source receipts into signed authority and the companion snapshot; reject a policy ID or partial payload as authority.
- [x] 4.3 Add fixtures for regime boundaries and every rule field consumed by the canonical RQGM A-share replay.

## 5. Enable Verified Positive Aggregation

- [x] 5.1 Replace forced-false report construction with a fresh conjunctive report over verified intrinsic evidence, admitted external authorities, and complete availability closure.
- [x] 5.2 Replace the materializer's mandatory-false assertion with equality between materialized and independently exported/reverified readiness while preserving valid blocked bundles.
- [x] 5.3 Prove materialize, read-only export, and a second byte-for-byte verification return `ready=true` only for the complete isolated fixture.

## 6. Integration And Real-Data Audit

- [x] 6.1 Run the full `stock_data` tests, lint/type checks, strict OpenSpec validation, tamper/crash/replay tests, and `git diff --check` with zero failures.
- [x] 6.2 Run RQGM consumer-contract fixtures against positive and blocked provider bundles without weakening its nine-component, exact-panel, raw-price, or PIT gates.
- [x] 6.3 Audit current Tonghuashun, Tencent, Sina, and Baostock artifacts through the implemented path; record exact blockers and keep post-hoc rows at retrospective research grade.
- [x] 6.4 Permit registration of a new future 12-by-3 panel only after code, static rule/calendar authority, collector capability, and independent trust enrollment pass; never repair or reuse the expired panel.
