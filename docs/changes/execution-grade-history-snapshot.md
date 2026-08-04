# Execution-Grade History Snapshot

## Scope

Add a separate immutable export for RQGM continuous portfolio replay. The export
contains six independently hashed canonical JSONL artifacts: raw execution prices,
signal prices, corporate actions, instrument status, historical universe membership,
and dated market rules.

This change does not promote the existing qfq SQLite snapshot. It does not synthesize
historical ST, suspension, membership, corporate-action, or availability records.
Callers must provide records collected from explicit authorities; missing artifacts
fail closed.

## Follow-Up Data Work

- Raw and adjusted daily prices may be populated from the existing BaoStock paths,
  with retrieval time retained per row.
- Corporate actions and historical listing/ST/suspension state require dedicated,
  versioned collectors and source receipts.
- Historical universe membership and the dated exchange rulebook require separate
  authorities; current membership is not an acceptable substitute.
- RQGM real replay remains blocked until every artifact passes byte-level and
  record-level validation for the frozen panel interval.

## Validation Status

There is currently no independently verifiable execution-grade export in this
repository. A local cache timestamp or a self-generated manifest cannot certify
that a historical record was available at its historical trade-date cutoff.
RQGM continuous replay therefore remains blocked until every required artifact
comes from an explicit authority and passes byte-level and record-level
validation for the frozen panel interval.

## Machine-Readable Readiness

Use the read-only readiness command before asking RQGM to create or replay an
execution snapshot:

```bash
stockdata-cli execution-readiness \
  --database /path/to/cache.sqlite \
  --source baostock \
  --adjustment-mode raw \
  --adjustment-version baostock-adjustflag-3 \
  --panel-file /path/to/frozen-split-overlay.json
```

The command returns JSON with `ready`, schema details, row and receipt counts,
identity coverage, the requested panel size, and stable blocker codes. It opens
the database read-only and does not migrate legacy schemas. Exact panels accept
the existing `splits.search-validation` overlay shape or a `panel` list of
`SYMBOL@YYYY-MM-DD` values.

Readiness requires the v4 composite price identity, append-only receipt guards,
final rows, exact panel coverage, valid response hashes, response/bar equality,
and receipt observation time bound to the row availability time. A local receipt
proves what this cache observed and when; it does not turn a 2026 capture into
authoritative 2025 publication evidence or replace signed historical universe
membership.

The 2026-07-27 audit of `~/.stockdata/cache-raw.sqlite` reports 2,232 finalized
raw rows and zero receipt-linked rows. Its current status is therefore
`ready: false` with blocker `missing_receipts`; these legacy rows remain research
data and cannot unlock RQGM execution-grade replay.

## Forward Evidence Capture

`forward-capture` maintains a dedicated raw-price database for one immutable
cohort. The first run binds canonical cohort JSON and its SHA-256 inside the
database behind no-update/no-delete triggers. Later changes to symbols, start
date, source, adjustment mode, or adjustment version fail before fetching.

```bash
stockdata-cli forward-capture \
  --database ~/.stockdata/rqgm-forward-evidence.sqlite \
  --codes-file /path/to/frozen-symbols.txt \
  --start 2026-07-27 \
  --source tencent
```

Each run synchronizes only missing dates, builds the exact product of captured
sessions and cohort symbols, then returns both sync status and execution
readiness. `ready: true` here means only that the raw price panel has valid,
timely receipts; it does not certify universe, status, corporate-action, or rule
artifacts.

Price availability follows the replay contract: a full daily bar cannot be
available before 15:00 on its effective date; same-day post-close capture is
valid; cross-day capture must occur on the frozen panel's next session strictly
before 09:25. Intermediate dates, 09:25 or later, and unknown terminal sessions
fail closed.

The 2026-07-27 live acceptance found BaoStock published only through 2026-07-24,
so it cannot meet this next-open SLA by itself. `tencent-qt-daily-v1` captures
the same-day quote response after finalization, preserves the raw HTTP text in
the immutable receipt, and converts Tencent volume from lots to shares. The
12-symbol RQGM cohort produced 12 finalized, receipt-linked rows with no
blockers in `~/.stockdata/rqgm-forward-evidence.sqlite`.

## Forward Context Capture

`forward-context-capture` records the current post-close Sina `hs_a` market
snapshot for the already-bound price cohort. It stores every raw response page,
the advertised universe count, one receipt hash, full daily membership, and the
cohort instrument-status projection in append-only SQLite tables. Capture rejects
another effective date, pre-close collection, future-dated receipts, incomplete
cohorts, duplicate symbols, count drift, or parsed rows that differ from the raw
pages.

```bash
stockdata-cli forward-context-capture \
  --database ~/.stockdata/rqgm-forward-evidence.sqlite \
  --date 2026-07-27
```

The live 2026-07-27 capture retained 5,532 universe rows and 12 cohort status
rows at `2026-07-27T17:10:45+08:00`. Receipt and row integrity pass, but this is
post-close evidence and cannot certify the same day's 09:25 decision cutoff.
The suspension value is a quote-activity proxy rather than an exchange status
announcement, and the universe publisher key is not enrolled. These remain
explicit blockers.

Use `full-execution-readiness` for the six-component gate. The older
`execution-readiness` command is price-only and must not be interpreted as full
snapshot readiness. The full report binds the exact price identity and canonical
panel digest and currently remains false for universe trust/timing, status
authority, corporate-action completeness, and the official rulebook bundle.

SQLite triggers prevent ordinary updates and deletes and their exact definitions
are checked. They do not protect against a database owner rebuilding the whole
file; cryptographic publisher enrollment and an external signed evidence root
are still required before execution-grade promotion.

Starting with the next uncaptured session, `forward-context-capture` accepts either the
strict `08:30 <= observed_at < 09:25` pre-open decision window or a post-close window at
or after 15:00. Captures during the trading session are rejected. Only the pre-open
observation can satisfy same-day decision availability; post-close rows remain useful
final evidence. The two phases have independent append-only keys, so collecting pre-open
evidence cannot suppress the same day's post-close capture. The database persists
`decision_available_at`, `outcome_observed_at`, and `finalized_at` on the phase record;
readiness requires both phases and never infers one phase from the other's timestamp.

## Forward Corporate Actions

`forward-corporate-actions-capture` queries every symbol in the immutable cohort during
the same pre-open decision window. It stores exact BaoStock fields and rows in one
receipt, plus an append-only coverage row for every symbol. Empty provider results are
retained as zero-event completeness evidence instead of being silently omitted.

```bash
stockdata-cli forward-corporate-actions-capture \
  --database ~/.stockdata/rqgm-forward-evidence.sqlite \
  --date 2026-07-28
```

This is explicitly a dividend-observation aid, not a complete corporate-action ledger.
It does not cover every allotment, merger, cancellation, code change, delisting, or
authority revision. Full readiness therefore keeps three blockers:
`dividend_observation_not_full_corporate_action_ledger`,
`corporate_action_revisions_not_supported`, and
`corporate_action_publisher_key_not_enrolled`. The exporter does not consume this table
as execution authority. Event announcement dates remain date-only source fields and are
not fabricated into publication timestamps.
