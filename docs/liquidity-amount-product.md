# Native liquidity amount product

`stockdata.liquidity_amount_product` replays BaoStock `amount` from raw daily
`query_history_k_data_plus` response strings. It does not compute close times
volume and does not alter any signal or execution-price identity.

`build_liquidity_amounts_product(captures, panel=..., decision_cutoff=...,
expected_watermark=...)` requires an exact panel and at least 20 positive finite
daily amounts per ETF. Rows must have been observed after their 15:00 China close
and no later than the decision cutoff. Each ETF must reach the exact watermark.

Records contain `panel_entry`, `code`, `date`, `effective_at`, `available_at`,
`source_receipt_ids`, `record_sha256`, and `payload={amount,amount_unit:'CNY'}`.
Canonical SHA-256 seals bind payloads, raw receipts and the complete product.
Verification rebuilds the product from the raw receipts, including field order,
request identity, amount values, and observation time.

The product's formal/eligible markers describe its intended component role.
They are not authentication: `source_authentication` is explicitly
`requires_trusted_signed_envelope`. A consumer MUST separately verify a trusted
publisher signature, its enrolled liquidity component role, and the complete
provider closure. A content hash alone must never admit a trading decision.

The initial exact ETF identity allowlist is supported by issuer publications:

- [159980, Dacheng prospectus](https://www.dcfund.com.cn/home/working/download/autoupload/1742473215380.pdf)
- [561980, China Merchants fund announcement](https://static.cmfchina.com/web/noticedetails/225146/index.html)

Unknown symbols fail closed. This verifies identity only; fund-specific market
rules and a dated instrument-status authority remain separate prerequisites.
Tests use synthetic captures and establish contract behavior, not actual
BaoStock ETF coverage, publisher enrollment, or present-day BUY eligibility.

## Provider capability evidence, 2026-09-05

The provider's official documentation API (read-only POST) lists ETF as security
type 5 in `https://www.baostock.com/helpdocs/api/markdown/stockBasic.md` and
`amount` as CNY in `https://www.baostock.com/helpdocs/api/markdown/stockKData.md`.
The human-facing routes are `/mainContent?file=stockBasic.md` and
`/mainContent?file=stockKData.md` on the same official host.

A bounded one-symbol, one-date read-only probe returned error code `0`, message
`success`, for `query_history_k_data_plus('sh.561980',
'date,code,close,volume,amount', start_date='2026-08-28',
end_date='2026-08-28', frequency='d', adjustflag='3')`:

```json
{"fields":["date","code","close","volume","amount"],"rows":[["2026-08-28","sh.561980","0.6930","388247300","273061615.0000"]]}
```

This establishes actual native ETF amount availability for that one observation.
It does not establish a complete 20-session product or historical availability
at a past decision cutoff. No production cache or account state was written.

## Signed supplement

`build_liquidity_authority_inputs(product)` returns `artifact`, `source_receipts`,
and `product`. The artifact has the standard six-field component record envelope;
the binding receipt seals the canonical complete product. Publish its artifact
with the existing `publish_authority_envelope(component='liquidity_amounts', ...)`
and an externally enrolled publisher holding that role. Supply every panel
entry's actual EOD decision cutoff, not its historical preopen cutoff.

`build_global_signals_authority_inputs(snapshot, panel=[symbol@asof],
available_at=...)` retains the complete Trading `trading-global-snapshot/1`
payload. Publish it through the same API with component `global_signals`.
Trading remains responsible for replaying global risk policy from its quotes.

`stockdata.main_buy_supplement.verify_main_buy_supplement(payload,
expected_registry_sha256=..., provider_manifest_sha256=..., asof=...,
decision_cutoff=...)` returns the unchanged payload on success or raises
`ValueError`. It reads no files or network and changes no state. The caller must
obtain the registry pin from its separately fixed request or verified price
snapshot, and must compare the supplement's symbols with its requested universe.

The payload contains `schema_version='stockdata-main-buy-supplement/1'`,
`provider_manifest_sha256`, `asof`, `decision_cutoff`, `symbols`, `registry`,
`liquidity`, `global_signals`, and `references`. Each component includes its
artifact, authority envelope, and exact source-receipt mapping. Liquidity also
embeds the original product; global signals embed the original snapshot.
References are the existing signed calendar, status, rules, universe, and
corporate-action artifacts. No v1 required-component set changes.

`asof` is the final price session T. The amount product covers at least 20
consecutive signed calendar sessions through T, including T. Calendar next-session
links prove continuity, including holidays; T must have closed before the frozen
supplement cutoff. A later caller cutoff may consume the same frozen supplement.

## Exact ETF rule scope

`market_rules` additionally accepts `stockdata-etf-market-rule-payload/1` for
561980.SH only. Its issuer classifies it as a domestic equity ETF, and the
[SSE 2026 trading rules](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)
effective 2026-07-06 establish share lots, tick size, settlement, and default
daily limits. Together with the [SSE ETF trading-unit FAQ](https://big5.sse.com.cn/site/cht/www.sse.com.cn/assortment/fund/etf/question/c/c_20240118_5734754.shtml),
the admitted scope is 100-share lots, CNY 0.001 tick, T+1 and 10% limits.
The [issuer product page](https://static.cmfchina.com/web/fundDetail/561980/index.html)
and [listing notice](https://www.cmfchina.com/upload/20230829/202308291693269077514.pdf)
identify the exact security. Payloads must carry `instrument_id`, `fund_type`,
`classification_source`, and `rule_source`, in addition to existing fields.
Commission, transfer fees, stamp duty and slippage remain supplied by the
existing signed policy; this implementation supplies no new fee defaults.

Other ETFs, including 159980's commodity-futures settlement regime, are not yet
admitted by the market-rule validator. Their independent amount identity does
not imply trading permission. Unknown identities and missing sources fail closed.

## Remaining real-run inputs

The checkout's `stockdata/enrolled_trust_registry.json` has zero roots and zero
publisher enrollments. This work does not create production trust or keys.
The supplement tests use explicitly synthetic keys and receipts, including a
dated signed no-event corporate-action fixture. They must not be published as
actual provider facts. A real run still needs an externally pinned registry,
authorized signing keys, the original v1 price provider bundle, a complete
20-session native amount capture, and dated calendar/status/universe/CA/rules
and global receipts. No such complete real-run input closure is produced here.
