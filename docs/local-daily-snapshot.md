# Current Trading price snapshot

`stockdata.local_daily_snapshot` is an independent current-observation input
profile. It does not create an RQGM provider bundle or claim historical PIT
readiness. It binds genuine request/response captures for a fixed maximum of
eight current ETFs and five current reference indices, using a 420-calendar-day
request ending at the exact price session T.

```bash
python -m stockdata.local_daily_snapshot \
  --symbol 561980.SH --symbol 000300.SH --asof 2026-09-04 \
  --publisher-dir /absolute/local-publisher \
  --registry-sha256 EXTERNALLY_FIXED_SHA256 \
  --output-dir /absolute/new-price-snapshot-directory
```

Repeat `--symbol` for the exact symbols required by Trading's independently fixed
request. The capture does not read or update shared caches. Raw captures and
source failures are saved individually. ETF prices prefer Tencent and fall back
to BaoStock; index prices prefer BaoStock and fall back to Tencent. Execution
requests raw prices; ETF signal requests qfq. If Tencent itself returns only
`day`, its actual identity remains raw, matching the existing source behavior.
Tencent volume is converted from hands to shares explicitly. A usable primary
cannot be silently displaced by a fallback. Every selected source must cover T
and at least 20 finalized bars; insufficient history remains blocked.

The outer schema is `stockdata-trading-daily-snapshot/1`, with `asof`,
`decision_cutoff`, `symbols`, embedded `registry`, `artifact`, `source_receipts`,
`authority_envelope`, and `snapshot_sha256`. The artifact uses role
`local_daily_prices`; each `symbol@T` record has an execution and signal manifest.
Inner manifests remain shadow data products. The outer enrolled signature grants
only this separately verified current-observation input authority.

```python
from stockdata.local_daily_snapshot import verify_local_daily_snapshot
verify_local_daily_snapshot(
    snapshot, expected_registry_sha256=external_pin,
    expected_symbols=exact_symbols, asof=price_session,
    decision_cutoff=caller_cutoff,
)
```

Verification is pure and replays all rows, source ordering, adjustments, units,
raw receipt identities, hash bindings, signature and actual availability. Trading
must additionally enforce its data completeness, reference facts and risk policy.
No price snapshot alone authorizes a BUY. The optional supplement uses the same
externally fixed registry and a final cutoff after all source collection and
signing, while preserving the original price watermark T.
