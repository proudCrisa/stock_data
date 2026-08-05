# Research Data Artifacts

These collectors use the free Baostock service. They are designed to improve
historical research coverage and are intentionally not execution-grade
authorities.

## Local Artifacts

- Trading calendar: `~/.stockdata/research/calendar/`
- Corporate-action observations: `~/.stockdata/research/corporate-actions/`
- Historical index membership: `~/.stockdata/research/index-universe/`

Each artifact contains a content-addressed directory with a `manifest.json`
and a canonical JSONL payload. The manifest records the provider, query,
retrieval time, row count, and content hash.

## Refresh Commands

```sh
PYTHONPATH=. python3 scripts/fetch_baostock_calendar.py \
  --start 2015-01-01 --end 2026-08-05 \
  --output-root "$HOME/.stockdata/research/calendar"

PYTHONPATH=. python3 scripts/fetch_baostock_corporate_actions.py \
  --database "$HOME/.stockdata/cache-raw.sqlite" \
  --observation-date 2026-08-05 \
  --output-root "$HOME/.stockdata/research/corporate-actions"

PYTHONPATH=. python3 scripts/fetch_baostock_index_universe.py \
  --date 2025-12-31 --date 2026-06-30 \
  --output-root "$HOME/.stockdata/research/index-universe"
```

## Evidence Limits

Baostock observations do not prove that a record was available before a
historical decision cutoff. The corporate-action collector does not claim a
complete revision ledger, and the index collector covers index membership only,
not the complete historical stock universe or instrument-status authority.
The manifests therefore set `point_in_time_verified=false`,
`revision_complete=false` where applicable, `complete_panel=false` for index
membership, and `execution_grade=false`. These artifacts may guide research
allocation and diagnostics, but they cannot unlock task 8.6 or task 14.3.
