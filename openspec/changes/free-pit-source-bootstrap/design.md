## Context

`stock_data` already separates collection receipts from signed component authority.
JQData can bootstrap structured history, but its three-month trial, vendor license, and
incomplete historical publication timestamps make it unsuitable as sole PIT authority.

## Decisions

### Credentials are interactive and ephemeral

The CLI prompts for both account and secret with `getpass`. They are passed directly to
the SDK authentication call, never returned by the adapter, and never written to disk.
Authentication errors are replaced with a fixed redacted error.

### Bootstrap artifacts are deterministic but non-authoritative

Normalized rows bind requested/effective dates, symbol, membership/status, retrieval
time, provider, and a vendor-bootstrap evidence grade. They may support comparison and
official-source reconciliation, but cannot satisfy signed component readiness.

### Exact-date and quota gates fail closed

The adapter rejects rows whose provider date differs from the requested date, rejects
negative or insufficient remaining quota, and caps each command to an explicit maximum.
It does not call nearest-date index-weight APIs.

### ETF trial access is opt-in outside this slice

The first implementation collects stock universe/status only. This prevents tests or a
smoke command from starting the separate 14-day field-fund trial window.

## Verification

- Unit tests prove credentials do not appear in output or errors.
- Unit tests prove exact-date, quota, deterministic ordering, and evidence-grade gates.
- CLI tests prove no credential flags exist.
- Existing provider and RQGM compatibility suites remain green.
