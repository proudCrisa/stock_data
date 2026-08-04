## 1. Contract

- [x] 1.1 Freeze the JQData bootstrap boundary, credential policy, and evidence grade.

## 2. Tests and Implementation

- [x] 2.1 Add failing tests for interactive credentials, redaction, exact dates, quota limits, deterministic rows, and non-authority grading.
- [x] 2.2 Implement the optional JQData adapter and bounded stock-universe bootstrap.
- [x] 2.3 Add a CLI command with no account or secret arguments.

## 3. Verification

- [x] 3.1 Run targeted and full `stock_data` tests, lint, and compile checks.
- [x] 3.2 Run RQGM provider-consumer compatibility tests and record remaining blockers.

Verification: 267 `stock_data` tests and 49 RQGM consumer tests passed; changed-file
Ruff and compile checks passed. The 12-symbol readiness panel remains fail-closed with
only execution and signal prices ready; JQData bootstrap grants no component authority.
