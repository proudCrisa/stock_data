## Why

The RQGM owner has explicitly designated the local `stock_data` dataset as the trusted
market-data authority. Requiring a new external trust registry and five signed roles for
each prospective panel adds an authority claim that is neither available nor needed for
this local research boundary, while the point-in-time properties that matter remain
mechanically verifiable.

## What Changes

- Add `rqgm-forward-panel-registration/5` with the fixed authority mode
  `trusted_local_mechanical`.
- Preserve registration `/4` and all of its signed-authority semantics unchanged.
- Bind canonical source receipts, calendar, market rules, exact panel, collector physical
  identity, genesis, cohort, and continuity ledger without claiming external signature
  authority.
- Reverify the same immutable inputs before every capture and provider materialization.
- Keep local trust negative-only: it cannot grant component readiness, Judge authority,
  release eligibility, or production authority.

## Impact

- Prospective registration, capture, continuity, materialization, and export under
  `stockdata/`.
- RQGM read-only registration parsing and compatibility fixtures.
- A new prospective registration is still required; historical panels cannot be upgraded.
