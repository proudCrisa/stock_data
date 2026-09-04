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
