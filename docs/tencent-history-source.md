# Tencent Historical Research Source

`stockdata.fetch_tencent_history` uses Tencent's current historical endpoint:

`https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get`

The request is made once per requested calendar year with `symbol=sh600519` or
`sz000001`. Tencent may return its latest 640 bars regardless of the requested
window, so the parser applies a second local date filter and records the actual
coverage in the artifact manifest.

The response keys are kept distinct:

- `day`: raw prices
- `qfqday`: signal-research forward-adjusted prices
- `hfqday`: signal-research backward-adjusted prices

Tencent volume is returned in lots and is converted to shares by multiplying by
100. The raw HTTP response is stored in `receipt.json`; `manifest.json` stores
only its content hash and a redacted summary. All artifacts are explicitly
`research_only=true` and `execution_grade=false`. They cannot unlock execution
readiness or release authority.

Example commands:

```bash
python3 scripts/fetch_tencent_history.py \
  --code 561980.SH --start 2026-07-01 --end 2026-07-10 \
  --adjustment-mode qfq \
  --output-root ~/.stockdata/research/tencent-history

python3 scripts/compare_tencent_baostock.py \
  --code 600519.SH --start 2026-07-01 --end 2026-07-10
```

The comparison must use `raw` on both sides. A volume difference within one
Tencent lot is reported as equivalent, not silently rewritten.
