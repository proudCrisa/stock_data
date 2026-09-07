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

## Versioned liquidity qualification

The liquidity issuer allowlist is a data-product qualification boundary. It
does not express user investment permission, candidate promotion, cash
allocation, order eligibility, or broker authority.

`stockdata-liquidity-etf-qualification/1` remains the default and retains its
original eight-symbol set. `stockdata-liquidity-etf-qualification/2` explicitly
adds `159992.SZ`; callers must select it. All existing native-CNY-amount,
minimum-20-observation, exact-panel, finality, cutoff, replay, and signature
requirements continue to apply.

The retained V2 product uses 26 exact sessions from 2026-08-03 through
2026-09-07, observed after the final session close and before the
2026-09-08 09:25 decision cutoff. Its product identity is
`3203c8ff42bca7c5eec28ce10485c1543ef236ee9dd0f8b08715c2d3bd73d87b`.

The verified-input bundle is
`verified-inputs-159992-20260908/bundle-manifest.json` in the evidence directory.
Its SHA-256 is
`ce1e08a35409e7adb6f66ca615a21f8b361d780b336bd13ae25d06798fd336f8`.
It also contains a signed `corporate_actions` input with `events=[]`, backed by
the exact query receipt and 25-attachment review through
`corporate-actions-evidence.json` (SHA-256
`90acfebed775e61d17e310ab800c44710cd41f2aa2fc3a4d929440616db17b94`).
The artifact is dated for the 2026-09-08 panel, but the retained announcement
query covers only 2025-07-11 through 2026-09-07. It does not establish corporate
action completeness through the 2026-09-08 decision cutoff or any later panel.
The bundle uses a synthetic trust root and signer, not a production-qualified
signer. Real 2026-09-08 instrument status remains unavailable and is not
inferred from the 2026-09-07 probe.
