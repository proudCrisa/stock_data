## Why

`stock_data` already retains append-only forward provider receipts containing
point-in-time non-OHLCV observations, but RQGM has no provider-neutral,
content-addressed artifact through which it can consume those observations. Direct
access to provider-private tables would couple RQGM to collection internals and leave
the physical source evidence outside the feature identity.

## What Changes

- Add a local, read-only exporter for a frozen symbol panel and one explicitly selected
  source receipt.
- Verify the receipt response hash, observation time, source rows, requested panel,
  finite values, and canonical ordering before export.
- Emit an immutable feature snapshot containing canonical records, source evidence,
  field semantics, content hashes, and an explicit evidence-grade boundary.
- Preserve `execution_grade=false`: the artifact may support PIT retrospective research
  after its recorded availability time, but it cannot satisfy full execution readiness,
  release, or production authority.
- Add a standalone module entry point so this change does not depend on modifying or
  weakening existing provider-readiness commands.

## Capabilities

### New Capabilities

- `pit-feature-snapshot-export`: Export verified local provider observations as a
  provider-neutral, content-addressed, research-only feature snapshot.

### Modified Capabilities

None.

## Impact

The change adds one `stockdata` module, focused tests, and an OpenSpec contract. It reads
the existing local evidence database in immutable mode and writes only to a caller-chosen
output root. It performs no collection, network access, authority admission, signing, or
mutation of provider evidence. RQGM may consume the resulting artifact in a separate
repository change while retaining its own activation, restricted-reader, replay, and
evidence-grade gates.
