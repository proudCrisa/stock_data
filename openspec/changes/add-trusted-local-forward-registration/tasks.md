## 1. Registration Contract

- [x] 1.1 Add exact `/5 trusted_local_mechanical` registration and static-prerequisite schemas while preserving `/4` unchanged.
- [x] 1.2 Bind canonical source receipts, calendar, market rules, panel timing, and collector capability without signature-shaped fields.
- [x] 1.3 Preserve exclusive registration, idempotent crash recovery, and exact `REGISTRATION_BOUND` continuity binding.

## 2. Downstream Verification

- [x] 2.1 Reverify `/5` static inputs before registered capture and reject drift before any provider call.
- [x] 2.2 Carry `/5` through continuity closure, provider materialization, and export without granting readiness.
- [x] 2.3 Add RQGM read-only `/5` consumption while keeping `/4` fail-closed semantics unchanged.

## 3. Evidence

- [x] 3.1 Add positive, shape, timing, drift, replacement, no-overwrite, crash-recovery, and mixed-schema tests.
- [x] 3.2 Register the fresh 2026-09-07 panel and verify exactly one matching `REGISTRATION_BOUND` event without running replay.
- [x] 3.3 Run focused regressions in both repositories and record readiness facts without completing RQGM task 8.6b early.

## 4. E1 Research Replay Export

- [x] 4.1 Freeze the exact `stockdata-rqgm-research-replay-export/1` field sets and implement its read-only provider verifier with mandatory, separately recomputed `expected_bindings`; reject omitted or `None` closure without changing `/5` readiness or `stockdata-rqgm-provider-export/1`.
- [x] 4.2 Require exact independent closure over the mixed `.SH`/`.SZ` 36-cell panel, offset-aware registration strictly before every session, all twelve ordered terminal collector steps, immutable checkout/bundle/database/ledger/continuity identities, receipt IDs and binding sets, separate adjustments, and nine mechanically complete components; reject stale selection, drift, backfill, unused bindings, and partial closure.
- [x] 4.3 Bind content-addressed local research authorization, shared-cash, provider market-rule/cost, and risk policies; require exact `stockdata-market-rule-cost-policy-binding/1` fields and canonical body hash, exact equality between its `market_rule_artifact_reference` and the provider market-rule component, and exact expected-closure equality over the entire binding; require downstream RQGM research-authorization/shared-cash/risk equalities, `transaction_cost_policy_reference == market_rule_cost_policy_binding.policy_reference`, and market-rule/corporate-action component equalities without adding a final-plan reverse reference; reject result feedback, detached defaults, and policy drift, and fix max evidence at E1 with every authority grant false.
- [x] 4.4 Add cross-repository contract coverage for eligible E1 export; omitted expected closure; SH/SZ panel and offset/session timing; missing, extra, or reordered cells and steps; late/backfill and stale registration; exact receipt/component/policy drift including recomputed payload hashes; missing, extra, malformed, rehashed, expected-closure-drifted, or market-rule-detached `market_rule_cost_policy_binding`; downstream transaction-cost-to-binding-policy drift; result feedback; forged authority; and provider-readiness/Judge/release/production rejection without adding signatures or network dependencies.
- [x] 4.5 Implement the pure read-only `stockdata-rqgm-research-replay-materialization/1` builder and verifier over explicit canonical bytes for all nine components plus shared-cash and risk policy bodies; require every item to reproduce the export and independent expected-closure reference, reject paths/current-state fallbacks/result-shaped inputs, and grant no readiness or authority.
- [x] 4.6 Implement the pure `stockdata-rqgm-research-replay-export/1` emitter from mandatory independently produced `expected_bindings`; fix every E1/no-authority field in production, pass the emitted object through the existing verifier, reject malformed closure without repair, and prove direct handoff into the materialization builder without paths, current-state lookup, results, signatures, or network dependencies.
- [x] 4.7 Implement `resolve_trusted_local_research_replay_inputs` over one formally published absolute `bundle.json` and mandatory replay-policy binding; reuse retained-fd/no-follow/physical-identity/continuity/ABA verification, independently derive expected bindings and all nine canonical component payloads without reading a candidate export, reject claimed completeness and partial cleanup, and prove separate handoff to the existing export and materialization builders without upgrading regular readiness or granting authority.
- [x] 4.7a Rebuild the collector receipt domain from retained database bytes and keep the two registration prerequisite receipts exclusively bound to calendar and market rules.
- [x] 4.7b Reconstruct and byte-compare universe and instrument-status records, including the canonical full-membership universe identity and exact source-receipt bodies.
- [x] 4.7c Require three verified pre-open corporate-action captures and materialize only genuine zero-event cells; reject positive or ambiguous events until a versioned normalization contract exists.
- [x] 4.7d Require bidirectional closure inside each receipt domain before emitting their sorted collision-free union to availability and `source_receipt_references`; preserve both canonical generic market-rule ST branches in availability closure.
- [x] 4.8 Add one production materializer CLI from `/5` registration plus collector database to output directory: require the same completed retained snapshot and twelve terminal steps; derive the panel, two adjustments, prerequisite-backed calendar/rules/two receipts, reconstructed intrinsic/forward components, and production-built availability; call `materialize_provider_bundle` without live-path reopen, fixtures, callbacks, or caller semantic bytes; publish only by final rename; and test success, incomplete 0/12 no-write, identity/semantic drift, cleanup failure, no partial output, regular `ready=false`, and no authority upgrade.
- [ ] 4.9 Add one provider-owned, read-only, one-shot trusted-local research replay bridge over exactly one formal bundle and one canonical policy request; fixed-call the existing resolver, export builder, and materialization builder exactly once each in that order; return only the exact canonical provider export, expected closure, and materialization envelope; reject caller composition, alternate steps, partial output, drift, and result-shaped input; and add no replay, second authority, writer, readiness, or downstream authority.

## Evidence (2026-08-26)

- Current registration: `/Users/cdzhangxueli/.stockdata/rqgm-forward-registration-v6-20260907.json`, SHA-256 `5d1db50f1ab0c24419e667e8950d3e6ac035d98eada9d2d083eb9bbb1a6d31de`.
- Panel: 36 panel cells (12 symbols by 3 sessions) for 2026-09-07 through 2026-09-09, SHA-256 `6c21072dd8788f15e665ce6848675ccdace73c74c05c47726acc8d218c5b8695`.
- Explicit local fact inputs: calendar SHA-256 `9e31919109627efe47a5cd11fb30b7e4fafd4b46c82d6794f75216573f5071d5`; market rules SHA-256 `0b38994382dbc93c6ac473ad801dd6e7082ea9a8233034b2004a4f38532c5313`.
- Materialized calendar and market-rule artifacts: SHA-256 `63dd06cc19f5ac3046bf6724ac689b04ac4ba36d3fe64ec3c10d9600c25b2188` and `906fe6f96ac521fadfbbad506b7ef719e35ec082da4ff8a7e6b1471e9b9b0e00`; receipt closure is exact for all 36 calendar and 72 rule bindings.
- Continuity ledger contains exactly one matching `REGISTRATION_BOUND`; an idempotent registration retry left registration and ledger bytes unchanged.
- Regression evidence: all 1,390 collected `stock_data` tests passed; RQGM direct and contract/receipt tests passed 155/155; strict OpenSpec validation passed 7/7 and 10/10 respectively.
- Registration status remains `AWAITING_FULL_SNAPSHOT_READINESS`; local mechanical authority did not complete RQGM task 8.6b or grant readiness, Judge, release, or production authority.
