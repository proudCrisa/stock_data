# RQGM PIT Provider Contract Migration Evidence

Date: 2026-08-02

## Admission State

The provider contract implementation is complete, but E2 consumer admission remains
data-blocked. The provider-owned trust registry is intentionally empty, and complete
availability records cannot become ready until companion verification binds authoritative
trading-calendar decision cutoffs. No historical authority was generated or relabeled.

## Migration

1. Use `stockdata-rqgm-provider-contract/1` with separate
   `execution_adjustment_identity` and `signal_adjustment_identity` references.
2. Use readiness request fields `execution_adjustment_sha256` and
   `signal_adjustment_sha256`; the former must describe raw execution prices.
3. Materialize a `stockdata-companion-snapshot/1` over checkout, database, source
   receipts, exact panel, both adjustment identities, all nine components, coverage, and
   the pinned provider trust registry.
4. Run `stockdata-cli rqgm-provider-export --bundle-file <bundle.json>`. The command is
   read-only and verifies all companion bytes before returning the contract and readiness
   receipt.
5. RQGM consumes both adjustment references and preserves provider `ready=false` as
   `DATA_BLOCKED`.

## Rollback

Stop invoking `rqgm-provider-export` and keep E2 admission disabled. The legacy price,
forward-context, corporate-action, and six-artifact execution-snapshot commands are not
modified by the companion export path. Reverting the provider-export, companion,
availability, authority, and dual-adjustment changes removes the new path without changing
the underlying database or source receipts. The export command never repairs or writes
provider evidence.

## Current Gate

`rqgm-provider-materialize` creates a content-addressed input closure from the
actual database, exact panel, source receipts, both adjustment identities, and
all nine component files. It rejects research-only receipts, missing components,
duplicate receipts, and an existing output directory.

It does not attest a publisher, backfill availability, or turn retrospective
collection into PIT evidence. Its report remains explicitly fail-closed with
`provider_component_authority_not_attested` until independently enrolled and
verified authorities exist.

## Verification

- `python3 -m pytest` in `stock_data`: `256 passed, 3 deselected`.
- `python3 -m pytest tests/ -q` in `super-trader-rqgm`: `2584 passed`.
- Focused provider export/companion/CLI tests: `33 passed`.
- Focused RQGM consumer contract/readiness/snapshot compatibility tests: `43 passed`.
- `python3 -m py_compile` passed for the provider authority, availability, adjustment,
  companion, contract, and export modules.
- `git diff --check` passed before the final cross-repository regression.

These results prove contract behavior and fail-closed integration only. They do not prove
PIT readiness, E2 admission, strategy quality, profitability, or production readiness.
