## 1. Artifact Contract

- [x] 1.1 Implement immutable feature/source record and manifest schemas with canonical content identities and fixed research-only authority labels.
- [x] 1.2 Implement standalone artifact verification that rejects duplicate JSON keys, file drift, identity drift, non-canonical ordering, and unsafe paths.

## 2. Local Receipt Export

- [x] 2.1 Implement immutable SQLite receipt loading, response-hash verification, exact symbol selection, field mapping, availability validation, and deterministic revision identities.
- [x] 2.2 Implement atomic idempotent export beneath a caller-selected output root and a standalone `python -m` CLI with no collection path.

## 3. Verification

- [x] 3.1 Add tests for valid export/verification, incomplete panels, duplicate rows, receipt tampering, backdated availability, non-finite values, byte drift, identity collision, and unchanged source database bytes.
- [x] 3.2 Export and verify one real local Sina receipt artifact for the frozen RQGM symbol panel without network access, recording only non-sensitive identities and evidence limitations.
- [x] 3.3 Run focused and full tests, formatting/lint checks, `git diff --check`, and strict OpenSpec validation.

## Evidence

- Frozen feature snapshot: `0fbb8bd9e7e4b34779f0a8f91bcd4570bc2b0388176ee2312a059549d0db0e83`.
- Source availability time: `2026-07-27T17:10:45+08:00`; panel size: 12 symbols and 12 feature records.
- Authority boundary: `FORWARD_PIT_RESEARCH_ONLY`, `execution_grade=false`, and `authoritative_for_execution=false`.
- Verification: 17 focused tests and 497 full-suite tests passed; ruff, `git diff --check`, and strict OpenSpec validation passed.
