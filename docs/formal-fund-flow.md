# Formal Fund Flow Supplement

`stockdata.formal_fund_flow` supplies source-bound, signed raw fund-flow facts
for Trading's existing `MainNetFlow20` consumer. It does not compute a factor,
select a research window, fetch data, enroll a publisher or authorize a trade.
The source profile retains the existing Trading input: Westock `asfund`
provider-reported `MainNetFlow`, in CNY, with negative values permitted. It does
not define a large/superlarge-order decomposition, convert units, or substitute
turnover or fund subscriptions.

## Reader Contract

```python
from stockdata.formal_fund_flow import verify_formal_fund_flow

verified = verify_formal_fund_flow(
    payload,
    expected_registry_sha256=independent_registry_pin,
    provider_manifest_sha256=admitted_price_manifest_sha256,
    asof=formal_asof,
    decision_cutoff=formal_decision_cutoff,
    expected_symbols=configured_factor_proxies,
)
```

All expected values come from the caller's independently bound formal request.
The registry digest must not be learned from this supplement. The returned
`VerifiedFundFlowSupplement` contains `symbols`, `sessions`, `rows_by_symbol`
and `bindings`. Each `FundFlowRow` has `date` and `main_net_flow`; rows are ordered
by the complete verified session chain. Trading consumes these returned facts
in memory and retains the bindings with its factor/artifact. It does not reopen
a file, reconstruct a second schema or call a legacy data loader on failure.

The formal cutoff may use the existing timezone-aware `datetime.isoformat()`
form or Trading's UTC microsecond form, such as
`2026-08-20T08:10:00.000000Z`. The original caller string must exactly match the
wrapper, signed record context and returned binding. Calls into the existing
authority admission normalize only the cutoff argument to its existing format
at the same instant. Signed envelopes and source timestamps retain their existing
stock_data encoding; their bytes are not rewritten for this conversion.

Verification failure raises `ValueError`. Trading keeps the entire forward
fund-flow group unknown, with no partially admitted series. A successful
supplement supplies facts only; Trading's existing factor, risk and formal
decision rules remain its responsibility.

## Versioned Envelope

Top-level schema `stockdata-formal-fund-flow-supplement/1` has exactly:

| Field | Meaning |
| --- | --- |
| `schema_version` | This supplement version. |
| `provider_manifest_sha256` | Independently admitted price-manifest identity. |
| `asof`, `decision_cutoff` | Exact external formal decision context. |
| `symbols` | Canonical sorted unique caller-selected factor proxies. |
| `registry` | Existing enrolled registry, verified against the external pin. |
| `calendar` | Signed calendar `artifact`, `source_receipts`, `authority_envelope`. |
| `fund_flow` | Signed `artifact`, `source_receipts`, `authority_envelope`, plus retained `source_evidence`. |

The fund-flow component uses role `fund_flow` and schema `stockdata-fund-flow/1`.
It is an optional supplemental component. Existing provider REQUIRED_COMPONENTS,
old schema meanings and main-buy/global/liquidity components remain unchanged.
Trust uses the existing registry/enrollment and Ed25519 envelope verifier; no
new registry protocol or embedded self-authorizing pin is introduced.
The old `AUTHORITY_COMPONENT_ROLES` remains the registration/default-publisher
role set. `SUPPORTED_AUTHORITY_COMPONENT_ROLES` additionally permits explicit
fund-flow enrollments; it does not grant that role to existing or default
publishers or add it to old forward-registration prerequisites.

Every signed fund-flow record binds source/profile/field, code, session, value,
unit and the provider manifest/asof/cutoff context. The wrapper, caller and signed
record context must agree, so changing an unsigned top-level field cannot reuse
an old signature for another formal request. Availability and receipt identities
are also bound by the existing record and authority envelope.

## Source And Session Closure

Each symbol has one `stockdata-westock-asfund-capture/1` source capture:

```text
schema_version
source = westock
source_version = asfund/1
request = {code, start, end}
observed_at
response = {field: MainNetFlow, unit: CNY, rows: [{date, MainNetFlow}]}
```

`asfund/1` identifies this contract's source profile, not an assertion about a
vendor's official API version. Values must be finite non-boolean numbers with
the declared CNY semantics. The source response, request identity, symbol,
session and value are reconstructed and compared to the signed records and
receipts. A matching response or artifact hash alone is not source authority;
the externally pinned enrolled signer must hold the `fund_flow` role and its
signature must verify.

All symbols share the same declared signed calendar window, containing at least
39 finalized sessions and ending exactly at `asof`. Every symbol must have
exactly one value for every session. Signed next-session links prove adjacency;
weekdays are not a substitute for a trading calendar. No row is filled, dropped
or silently cropped. Thirty-nine observations are only the mathematical minimum
for Trading's rolling-20 sum followed by its minimum-20 signal calculation.
Longer declared windows are supported, and the producer returns all rows.

Observation/availability must be at or after the corresponding signed session
close and strictly before the formal cutoff. The asof session close is strictly
before that cutoff, which is strictly before the signed next-session cutoff.
Stale asof, missing or future rows, calendar gaps, incomplete symbol coverage,
invalid numeric types, unit/source drift and signature/content tampering reject.
This binds facts for the current formal decision; it does not retroactively
establish historical point-in-time availability for earlier decisions.

## Producer And Offline Acceptance

`build_fund_flow_authority_inputs(captures, *, expected_symbols,
calendar_authority, provider_manifest_sha256, asof, decision_cutoff)` constructs
unsigned component inputs from retained source evidence and an admitted signed
calendar. The existing authority publisher signs those inputs under an enrolled
fund-flow role. The reader replays source closure and verifies both calendar and
fund-flow authority. No fetcher, registry enrollment command or production
activation is part of this delivery.

Cross-project acceptance uses a shared test-only builder with temporary enrolled
keys, signed synthetic calendar and source captures. Trading passes its raw
series into that same builder and consumes the actual public verifier result.
The fixture must not mock the verifier or present a self-hash as trust. Longer
synthetic series support Trading's unchanged previous/current signal edge test.
No fixture is actual provider coverage, production enrollment or trading
eligibility. The checked-in registry and frozen RQGM research inputs are not
modified.

The shared helper is `tests/fund_flow_fixture.py`:

```python
fixture = make_signed_fund_flow_fixture(
    tmp_path,
    monkeypatch,
    symbols=configured_factor_proxies,
    session_count=80,
    values_by_symbol=synthetic_raw_values,
    asof="2026-08-20",
    decision_cutoff="2026-08-20T08:10:00.000000Z",
    provider_manifest_sha256=fixture_price_manifest_sha256,
)
```

Load that exact helper file with `importlib` or add this checkout's `tests/`
directory to the test import path; include the checkout itself for `stockdata`.
Use an isolated temporary directory and pytest's `monkeypatch`. Each supplied
series must have exactly `session_count` values. The helper's generated weekdays
are a declared synthetic signed calendar, not a claim about real holiday dates.
It calls the actual `build_fund_flow_authority_inputs`,
`publish_authority_envelope` and `verify_formal_fund_flow` functions.

Return keys are `payload`, `expected_registry_sha256`,
`provider_manifest_sha256`, `asof`, `decision_cutoff`, `expected_symbols`,
`calendar_authority`, `verified`, and `external_io_attempts`. The shared helper
denies DNS, socket and child-process calls and records attempted calls; acceptance
must require `external_io_attempts == []`, including after downstream consumption.
The fixture constructs a temporary test
root, root-signed role enrollment and publisher signatures, then computes its
registry pin from that independently constructed test trust configuration.
Pass the returned pin to Trading's independent test configuration; never obtain
a runtime expectation from an incoming untrusted payload. Test private keys are
public synthetic constants and confer no production trust. No checked-in
registry is enrolled or modified.

Trading supplies its existing synthetic price manifest and factor proxy values.
The helper does not create price authority or choose account holdings. The
bindings are a frozen `FundFlowBindings` dataclass; `dataclasses.asdict` converts
them for a JSON artifact without reopening or reparsing the source package.

Run the producer's focused acceptance with
`python3 -m pytest tests/test_formal_fund_flow.py`. Trading's actual formal
CLI/daily/artifact/Bundle test must use this same helper and the actual imported
reader; that downstream result is separate from these producer tests.
