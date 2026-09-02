# stock_data Stage 2C: sz159655 imported frozen observation

Stage 2C materializes the previously missing `sz159655` shadow-only artifact for
an isolated self-comparison replay. It imports the already committed trading
preimage; the existing Stage 2 run spec and runtime remain unchanged. It does not
close the independent data or source-authentication blockers, fetch data,
authenticate Tencent, select a formal source, or alter any decision, account, or
strategy state.

## Frozen input

- Artifact kind: `trading_daily_formal_preimage`
- SHA-256: `263b811f817cec371fc8cd71037ad6c6bfebc22bf2bdb5691c901723616a88e9`
- Instrument: `sz159655`
- Window: `2025-07-03` through `2026-08-27`
- Rows: 282
- Observed provenance claim: Tencent `txkq_v4`, qfq, volume in hands
- Authenticated provenance: none

The importer checks the exact file bytes before parsing, then requires canonical
strict JSON and the fixed identity, window, row count, provenance claim, and
explicitly unavailable `amount` values. The full preimage is retained as the
import receipt's `response`, so every emitted row can be replayed without the
source database or network.

## Replay

Create a new empty output directory, then run from the stock_data repository:

```bash
TRADING_STAGE2_ROOT=/path/to/trading-agent/trading/docs/architecture/evidence/stockdata-shadow-stage2-2026-08-27
STAGE2C_OUTPUT=/tmp/stockdata-stage2c-sz159655
mkdir "$STAGE2C_OUTPUT"
python scripts/export_imported_frozen_daily.py \
  --preimage "$TRADING_STAGE2_ROOT/preimages/263b811f817cec371fc8cd71037ad6c6bfebc22bf2bdb5691c901723616a88e9.json" \
  --expected-sha256 263b811f817cec371fc8cd71037ad6c6bfebc22bf2bdb5691c901723616a88e9 \
  --imported-at 2026-09-03T03:00:00+08:00 \
  --output-root "$STAGE2C_OUTPUT"
```

The independent manifest schema is
`stockdata-imported-frozen-daily-manifest/1`. Its fixed profile is
`imported_frozen_observation`; authority is `shadow`, decision eligibility and
decision authority are false, actions are empty, source authentication is
`unverified`, reference binding is `imported_hash_bound`, and permitted uses are
only `offline_replay` and `shadow_compare`. Its independence status is
`self_comparison_only` and `cutover_ready` is always false because both sides are
derived from the same formal preimage.

This artifact is evidence for a Stage 2C shadow comparison only. It is not
compatible evidence for formal source admission or production source routing.
