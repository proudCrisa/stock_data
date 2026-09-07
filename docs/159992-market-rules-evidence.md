# 159992.SZ market-rules evidence

The first direct observation of the retained official sources was 2026-09-07.
They support `159992.SZ@2026-09-08` and later covered panels; they do not support
a 2026-09-07 morning decision.

The retained evidence index is
`/Users/cdzhangxueli/.stockdata/159992-authority-evidence-20260907/evidence-index.json`
(SHA-256 `864f873f6c0b1c1cd34fe63421c0ffa2b68c067a7993dc55c00363880d3bafc3`).
It binds the issuer classification, SZSE listing notice, current SZSE all-funds
and 20-percent lists, the current SZSE trading rules, the exact 25-announcement
response and attachments, and the separate retained local execution-cost policy.

The evidence establishes an SZSE domestic-equity ETF with T+1 secondary-market
sellability, 10-percent up/down price limits, a 100-unit lot, and a CNY 0.001
price tick. All 25 announcement attachments were hashed and reviewed. None
announces a distribution, split, fold, merger, or other price/share adjustment
event for the covered interval. The per-file result is retained as
`announcement-review.json` beside the evidence index.

BaoStock status and native-amount probes are retained in the same directory.
They are observational inputs only. Adding this market-rule scope does not add
`159992.SZ` to the fixed main-buy review set or the liquidity issuer allowlist,
and it grants no production signer or execution authority.

The reproducible signed example is
`signed-fixture-159992-20260908/fixture-manifest.json` in the evidence directory
(SHA-256 `f85465324b432becdeb2dfee253958c97e1f41f0bd37c6e138dcf18f3091a082`).
It admits market-rules artifact
`2fff4a0ac2b9071a77f06055658b1d0e706edd86c470bde9a4a9b783f2590ec1`
through the generic status-bound admission path. Its trust root, signer, and
2026-09-08 instrument status are synthetic test inputs. The retained BaoStock
probe covers 2026-09-07 only and is not bound as next-day status. The fixture
retains `owner=subsystem=satellite` candidate semantics and does not establish
complete BUY eligibility, main-owner conversion, broker action, or a production
signature.
