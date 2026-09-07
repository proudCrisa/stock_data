# Current Trading price snapshot

`stockdata.local_daily_snapshot` is an independent current-observation input
profile. It does not create an RQGM provider bundle or claim historical PIT
readiness. It binds genuine request/response captures for a fixed maximum of
nine current ETFs and five current reference indices, using a 420-calendar-day
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
source failures are saved individually. This local profile uses Tencent only
for ETF prices; index prices prefer BaoStock and fall back to Tencent. Execution
requests raw prices; ETF signal requests qfq. If Tencent itself returns only
`day`, its actual identity remains raw. Trading retains its existing signal-only
fund-split normalization for that raw fallback; execution stays raw.
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

## Profile version 2 source qualification

Profile `trading-current-local-prices/2` adds `159992.SZ` for current-observation
price capture. The issuer page identifies it as the Yinhua CSI Brand Name Drug
ETF with asset class `Equity` and investment scope `China A`. The Shenzhen Stock
Exchange notice identifies security code `159992` as the Yinhua CSI Brand Name
Drug Industry ETF and states that exchange trading began on 2020-04-10.

- Issuer classification: `https://www.yhfund.com.cn/en/investment/quantitative/index.shtml`, observed content SHA-256 `91a78468fdd8fb12fb4b78dd5f3bab375af6126419ed61caf7758c63f329446f`, content as of 2026-06-30.
- Exchange listing notice: `https://www.szse.cn/disclosure/notice/t20200407_575761.html`, published 2020-04-07, observed content SHA-256 `fd482957577be0297b6a6ff856aaae46a688064c2d4d63c05fc2d77d7aeb54fa`.
- Applicable exchange rule: `https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf`, observed content SHA-256 `9b66f8b0db70f84a25ef1ccb4ee2351001724e408117552d75f6d8993483c586`.
- First local observation: `2026-09-07T19:53:40+08:00`.

`PROFILE_EFFECTIVE_FROM[PROFILE_V2]` is `2026-09-07`. That date only bounds
profile qualification. A new signed capture must still supply its own
`available_at` and `decision_cutoff`; this document supplies neither value and
cannot backdate source availability. Version 1 retains its original eight-ETF
universe and remains replayable by the version-aware verifier.

The qualification supports the current ETF identity and the existing Tencent
raw/qfq price route only. It does not provide historical PIT price evidence or
add `159992.SZ` to `ETF_RULE_SCOPES` or the reviewed main-ETF reference set. A
positive formal action still lacks independently receipt-bound evidence for:

- market rules under the exact ETF primary-source scope;
- same-session listing and suspension status;
- complete official corporate-action announcement coverage and review; and
- at least 20 exact-session native CNY amount observations for liquidity.

Until those contracts are completed, the price profile grants no liquidity,
research, recommendation, decision, broker, or order authority.
