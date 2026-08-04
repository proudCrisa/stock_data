## Why

RQGM execution replay is blocked on historical universe and instrument-status evidence.
The user has a time-limited JQData trial, but credentials must never be persisted and
vendor data must not be relabeled as official or point-in-time publisher authority.

## What Changes

- Add an optional JQData bootstrap adapter for bounded historical universe and status
  collection.
- Read the account and secret interactively at runtime and keep them only in memory.
- Enforce exact requested dates, bounded free-trial query budgets, deterministic output,
  and redacted failures.
- Mark every JQData artifact as vendor bootstrap evidence that cannot unlock signed
  authority or release readiness by itself.

## Non-Goals

- Persist credentials or include them in requests, receipts, logs, or artifacts.
- Consume ETF trial access during automated tests or unauthenticated smoke checks.
- Treat JQData as an official exchange publisher or infer historical availability times.
- Change RQGM readiness gates or enroll a trust key.

## Impact

The change is limited to an optional dependency, one provider adapter, one CLI command,
tests, and the existing provider-authority capability delta.
