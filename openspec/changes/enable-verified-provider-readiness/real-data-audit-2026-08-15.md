# Real Data Audit - 2026-08-15

## Scope

This read-only audit evaluated the current local Tonghuashun, Tencent, Sina, and
Baostock assets against the 12-symbol by 3-session panel registered on 2026-08-12
for sessions 2026-08-13, 2026-08-14, and 2026-08-17. Presence in a cache is not
treated as point-in-time authority.

## Inventory

| Source | Local evidence | Result |
|---|---|---|
| Tonghuashun | 85,727 qfq rows, 473 symbols, 2023-05-29 through 2026-08-14 | Zero of the 12 registered symbols are present. The database has no Tonghuashun collection receipts. These rows remain retrospective signal/research data. |
| Tencent | 120 raw rows for 12 symbols, 2026-07-27 through 2026-08-14, with 120 append-only receipts | The registered 36-cell panel is missing 24 price rows. It cannot form complete execution or signal price authority. |
| Sina | 7 pre-open and 11 post-close context observations; 84 pre-open and 132 post-close status observations for the 12 symbols | The registered panel is missing 24 decision-context rows and 12 finalized context rows. Signed calendar authority is also absent. |
| Baostock | 53,961 qfq rows for 79 symbols with 633 receipts; separate raw research cache has 58,767 rows for 25 symbols through 2026-07-30 | For the registered panel, 12 rows for 2026-08-17 are absent, 12 rows for 2026-08-13 were retrieved post hoc on 2026-08-15, and the 2026-08-14 rows lack a known next-session boundary. qfq cannot be execution-price authority. |

The verified research inventory also reports Baostock calendar, corporate-action,
and index-universe artifacts and Tencent history artifacts as
`research_vendor_only`, `execution_grade=false`, and/or `research_only=true`.

## Provider-Path Results

The current 36-cell panel remains `ready=false` for every audited database/source
combination. Exact blockers observed through `check_full_execution_readiness` are:

- Tencent raw: `missing_panel_rows=24`, `missing_decision_context_rows=24`,
  `missing_finalized_context_rows=12`, and `missing_corporate_action_coverage=24`.
- Tonghuashun qfq: `missing_panel_rows=36`, `no_selected_rows`, missing forward
  context/corporate-action tables, and `execution_prices_require_raw_adjustment`.
- Baostock qfq: `missing_panel_rows=12`, `post_hoc_availability=12`,
  `unknown_next_session=12`, and `execution_prices_require_raw_adjustment`.
- Every path also lacks enrolled signed trading-calendar and complete market-rule
  authority and complete `/2` component-availability closure.
- The production trust registry contains no trust roots or signer enrollments, so
  synthetic test authority cannot be reused as production authority.

## Grade Decision

These assets may support E1 retrospective research and falsification within their
declared coverage. They do not satisfy E2 PIT search, task 8.6 replay, elite,
release, or profit evidence. No historical receipt, timestamp, or missing panel
cell was repaired or backdated during this audit.

