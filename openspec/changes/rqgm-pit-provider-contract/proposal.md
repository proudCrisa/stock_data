## Why

RQGM cannot claim point-in-time or execution-grade evidence from price history alone. The
`stock_data` repository must own a versioned, independently verifiable provider contract
covering the complete decision and execution context, while missing authority remains an
explicit blocker rather than being reconstructed inside RQGM.

## What Changes

- Declare `stock_data` as the repository owner and sole provider-side writer for the RQGM
  PIT data contract.
- Add a versioned provider contract binding checkout, immutable database, source receipts,
  adjustment policy, exact panel, component readiness, and companion snapshot identities.
- Require decision context, signed trading calendar, historical universe, instrument
  status, corporate actions, market rules, execution/signal prices, and availability
  records as separate fail-closed components.
- Add provider acceptance tests for content identity, signature/trust identity, exact-panel
  coverage, tamper detection, component completeness, and price-only non-readiness.
- Preserve current blockers for capabilities that do not yet have enrolled historical or
  signed authority; this change does not manufacture missing records.

## Capabilities

### New Capabilities

- `rqgm-pit-provider-authority`: Versioned provider contract and acceptance behavior for
  complete RQGM point-in-time data evidence and companion snapshots.

### Modified Capabilities

None.

## Impact

The change affects provider contracts and tests under `stockdata/` and `tests/`, plus the
provider OpenSpec tree. RQGM remains a read-only consumer. Existing price, forward-context,
corporate-action, historical-universe, readiness, and execution-snapshot code is reused
only when it satisfies the new contract; unavailable signed calendar, status, rulebook, or
receipt authority continues to return `ready=false`.
