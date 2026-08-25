## Why

The prospective collector currently binds only a database path and structural fingerprints, so a same-path SQLite replacement or rollback can preserve the registered schema and cohort while breaking collection continuity. Capture also lacks a durable attempt history, which means a process crash, partial price commit, or repeated phase cannot be distinguished from a clean, ordered prospective collection.

## What Changes

- Add an immutable collector genesis that binds a no-follow regular SQLite file, a stable database UUID, the fixed cohort, and a separate append-only continuity ledger.
- Add a canonical JSONL hash-chain ledger, an exclusive process lock, ordered phase/step attempts, independent postconditions, and fail-closed crash recovery.
- **BREAKING**: advance future panel registration to `rqgm-forward-panel-registration/4`; registrations using `/3` or earlier cannot be migrated into point-in-time evidence and are rejected by the `/4` capture path.
- Make finalized collector prices append-only: an existing finalized row may be encountered only as a byte-equivalent idempotent no-op and can never be semantically overwritten.
- Take provider database snapshots through a consistent SQLite snapshot while holding the continuity lock, and require a verified continuity closure in provider bundle input `/2`.
- Preserve the RQGM-facing provider export envelope and all nine component readiness rules. Collector continuity is a provenance admission gate only: it can block materialization or export but can never grant `READY` to any component or aggregate.
- Preserve the existing external trust-root, signer enrollment, signed authority, future-data collection, independent Judge, and release-authorization blockers.

## Capabilities

### New Capabilities

- `forward-collector-continuity`: Defines collector genesis and physical identity, append-only finalized evidence, durable capture attempts and recovery, registration `/4`, consistent provider snapshots, and fail-closed RQGM compatibility.

### Modified Capabilities

None. The repository has no archived main capability specs; this change supplements the active provider-readiness work without changing its nine-component readiness authority.

## Impact

- Prospective collector preparation, registration, capture orchestration, SQLite access, and CLI entry points under `stockdata/`.
- Provider materialization and read-only provider export bundle parsing; provider bundle input moves to `/2`, while the RQGM-facing verified export envelope remains compatible.
- New continuity fixtures in `stock_data` and fail-closed cross-repository fixtures in `super-trader-rqgm`.
- Existing `/3` registrations and databases without a pre-registration genesis remain blocked and require a newly registered future panel rather than retrospective repair.
