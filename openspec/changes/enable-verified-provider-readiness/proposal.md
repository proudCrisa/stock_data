## Why

`stock_data` can construct and verify the nine-component RQGM provider contract, but its
materializer is intentionally hard-coded to emit `ready=false` even when every supplied
authority has been independently admitted. The expanded real market cache therefore
cannot enter execution-grade replay through any legitimate positive path, while changing
the boolean alone would bypass point-in-time and authority guarantees.

## What Changes

- Add one fail-closed positive readiness aggregation path whose result is true only when
  all nine exact-panel components are independently verified and have no blockers.
- Rebuild execution prices, signal prices, and decision context from the immutable
  database and bound source receipts instead of trusting caller-supplied component JSON.
- Continue to require enrolled external authority envelopes for trading calendar,
  universe, instrument status, corporate actions, and market rules.
- Complete availability verification by binding every availability row to the actual
  component record, source receipt, and signed calendar decision cutoff.
- Bind the complete dated market-rule bytes used by canonical replay, including price
  limits, lot/T+1 behavior, fees, taxes, validity intervals, and policy identity.
- Allow materialize, export, and byte-for-byte re-verification to return `ready=true` only
  for a complete admitted bundle; preserve explicit blockers for every incomplete,
  post-cutoff, self-signed, inferred, or post-hoc input.
- Add positive and negative end-to-end fixtures plus a read-only audit of current local
  data. Retrospectively downloaded Tonghuashun rows remain research-grade unless their
  original point-in-time authority and receipts satisfy the same contract.

## Capabilities

### New Capabilities

- `verified-provider-readiness`: Positive nine-component provider admission,
  materialization, export, and deterministic re-verification without weakening PIT,
  source-receipt, trust, exact-panel, or raw-execution requirements.

### Modified Capabilities

None.

## Impact

The change affects provider materialization, readiness-report verification, component
availability, companion snapshot binding, context/corporate-action reconstruction, market
rule schemas, CLI output, and provider acceptance tests in `stock_data`. RQGM remains a
read-only consumer; its nine-component gate is not weakened. Existing blocked bundles stay
blocked, and no historical data is upgraded merely because it is present in the cache.
