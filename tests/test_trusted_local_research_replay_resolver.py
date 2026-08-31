from __future__ import annotations

import hashlib
import inspect
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from test_collector_recovery import _tencent_capture, _writer_cache
from test_provider_materializer import _inputs
from test_trusted_local_forward_registration import _local_inputs, _register
from test_trusted_local_research_replay_materialization import _export_inputs

import stockdata.cache as cache_module
import stockdata.collector_continuity as continuity
import stockdata.sync as sync_module
from stockdata import component_availability, provider_export, provider_materializer
from stockdata.adjustment_identity import verify_adjustment_identity
from stockdata.collector_continuity import (
    open_registered_collector_read_connection,
    parse_collector_ledger,
    verify_collector_raw_postcondition,
)
from stockdata.forward_context import CapturedMarketRows, capture_forward_context
from stockdata.forward_corporate_actions import SOURCE as CORPORATE_ACTION_SOURCE
from stockdata.forward_corporate_actions import (
    CapturedCorporateActions,
    capture_forward_corporate_actions,
)
from stockdata.provider_export import resolve_trusted_local_research_replay_inputs
from stockdata.provider_intrinsic import (
    reconstruct_forward_component_evidence,
    reconstruct_intrinsic_evidence,
)
from stockdata.provider_materializer import materialize_provider_bundle
from stockdata.rqgm_provider_contract import (
    COMPONENT_SCHEMAS,
    REQUIRED_COMPONENTS,
    ProviderArtifactReference,
)
from stockdata.trusted_local_research_replay_export import (
    build_trusted_local_research_replay_export,
)
from stockdata.trusted_local_research_replay_materialization import (
    build_trusted_local_research_replay_materialization,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _availability_artifact(
    panel: list[str], components: dict[str, dict[str, object]]
) -> dict[str, object]:
    calendar_phases = {
        record["panel_entry"]: record["payload"]
        for record in components["trading_calendar"]["records"]
    }
    records = []
    for component, component_value in components.items():
        if component == "availability_records":
            continue
        for record in component_value["records"]:
            is_price = component in {"execution_prices", "signal_prices"}
            records.append(
                {
                    "component": component,
                    "panel_entry": record["panel_entry"],
                    "record_sha256": record["record_sha256"],
                    "source_receipt_ids": record["source_receipt_ids"],
                    "event_at": record["effective_at"],
                    "available_at": record["available_at"],
                    "cutoff_kind": (
                        "next_session_decision_cutoff_at"
                        if is_price
                        else "decision_cutoff_at"
                    ),
                    "applicable_cutoff_at": (
                        calendar_phases[record["panel_entry"]][
                            "next_session_decision_cutoff_at"
                            if is_price
                            else "decision_cutoff_at"
                        ]
                    ),
                }
            )
    return {
        "schema_version": COMPONENT_SCHEMAS["availability_records"],
        "panel": panel,
        "records": sorted(
            records, key=lambda row: (row["component"], row["panel_entry"])
        ),
    }


def _intrinsic_from_bundle(bundle: dict[str, object]) -> object:
    panel = json.loads(Path(bundle["exact_panel"]["path"]).read_bytes())
    calendar = json.loads(
        Path(bundle["components"]["trading_calendar"]["path"]).read_bytes()
    )
    execution_value = json.loads(
        Path(bundle["execution_adjustment_identity"]["path"]).read_bytes()
    )
    signal_value = json.loads(
        Path(bundle["signal_adjustment_identity"]["path"]).read_bytes()
    )
    return reconstruct_intrinsic_evidence(
        bundle["database"]["path"],
        panel=panel,
        execution_adjustment=verify_adjustment_identity(
            execution_value, expected_price_role="execution"
        ),
        signal_adjustment=verify_adjustment_identity(
            signal_value, expected_price_role="signal"
        ),
        decision_cutoffs={
            record["panel_entry"]: record["payload"]["decision_cutoff_at"]
            for record in calendar["records"]
        },
    )


def _context_rows(symbols: list[str]) -> list[dict[str, object]]:
    return [
        {
            "symbol": f"{'sz' if symbol.endswith('.SZ') else 'sh'}{symbol[:6]}",
            "name": f"Fixture {symbol}",
            "trade": "10.0",
            "volume": 1000,
        }
        for symbol in symbols
    ]


def _append_semantic_attempt(
    prepared: dict[str, object],
    lease: object,
    spec: object,
    symbols: list[str],
    first_session: str,
) -> None:
    started_at = (
        f"{spec.session}T08:35:00+08:00"
        if spec.phase == "pre_open"
        else f"{spec.session}T15:01:00+08:00"
    )
    finished_at = (
        f"{spec.session}T09:20:00+08:00"
        if spec.phase == "pre_open"
        else f"{spec.session}T16:20:00+08:00"
    )
    launch = continuity._begin_collector_step_attempt(
        lease, spec, now=lambda: started_at
    )
    with pytest.MonkeyPatch.context() as writer_patch, _writer_cache(
        prepared, spec, lease, launch
    ) as cache:
        if spec.step_id == "pre_open_corporate_actions":
            observed_at = f"{spec.session}T08:40:00+08:00"
            years = [int(spec.session[:4]) - 1, int(spec.session[:4])]
            receipt = {
                "observed_at": observed_at,
                "source": CORPORATE_ACTION_SOURCE,
                "request": {
                    "symbols": symbols,
                    "observation_date": spec.session,
                    "years": years,
                },
                "response": {
                    "symbols": {
                        symbol: [
                            {
                                "year": year,
                                "fields": ["dividOperateDate"],
                                "rows": [],
                            }
                            for year in years
                        ]
                        for symbol in symbols
                    }
                },
            }
            writer_patch.setattr(cache_module, "_utc_now", lambda: observed_at)
            capture_forward_corporate_actions(
                cache,
                spec.session,
                fetcher=lambda requested_symbols, observation_date: CapturedCorporateActions(
                    {symbol: [] for symbol in requested_symbols}, receipt
                ),
                now=datetime.fromisoformat(observed_at),
            )
        elif spec.step_id in {"pre_open_context", "post_close_context"}:
            observed_at = (
                f"{spec.session}T08:40:00+08:00"
                if spec.phase == "pre_open"
                else f"{spec.session}T15:05:00+08:00"
            )
            rows = _context_rows(symbols)
            receipt = {
                "observed_at": observed_at,
                "source": "sina-market-center-hs-a-v1",
                "request": {
                    "count_url": (
                        "http://vip.stock.finance.sina.com.cn/quotes_service/"
                        "api/json_v2.php/Market_Center.getHQNodeStockCount"
                    ),
                    "page_url": (
                        "http://vip.stock.finance.sina.com.cn/quotes_service/"
                        "api/json_v2.php/Market_Center.getHQNodeData"
                    ),
                    "node": "hs_a",
                    "page_size": 80,
                },
                "response": {
                    "advertised_count": len(rows),
                    "count_raw": str(len(rows)),
                    "raw_pages": [json.dumps(rows)],
                },
            }
            writer_patch.setattr(cache_module, "_utc_now", lambda: observed_at)
            capture_forward_context(
                cache,
                spec.session,
                fetcher=lambda: CapturedMarketRows(rows, receipt),
                now=datetime.fromisoformat(observed_at),
            )
        else:
            observed_at = f"{spec.session}T16:00:00+08:00"
            writer_patch.setattr(cache_module, "_utc_now", lambda: observed_at)
            writer_patch.setattr(
                sync_module, "default_final_date", lambda: spec.session
            )
            writer_patch.setattr(
                sync_module, "latest_finalized_date", lambda: spec.session
            )
            sync_module.sync_symbols(
                cache,
                symbols,
                first_session,
                spec.session,
                source="tencent",
                adjustment_mode="raw",
                adjustment_version="tencent-qt-daily-v1",
                fetcher=lambda code, start, end: _tencent_capture(
                    code,
                    end,
                    observed_at=observed_at,
                    start_date=start,
                ),
            )
    with open_registered_collector_read_connection(spec) as token:
        raw = verify_collector_raw_postcondition(
            token,
            spec,
            launch.baseline,
            attempt_started_at=started_at,
            attempt_finished_at=finished_at,
        )
    assert raw.raw_class == "complete"
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    process = continuity._CollectorProcessResult(
        True, 0, empty_sha256, 0, empty_sha256, 0, False
    )
    event_type, detail = continuity._terminal_attempt_event(
        launch,
        raw,
        process,
        process_launch_state="handle_obtained",
        finished_at=finished_at,
        failure_classification=None,
    )
    continuity._append_terminal_once(lease, launch, event_type=event_type, event=detail)


def _bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    component_override: str | None = None,
) -> Path:
    registration_root = tmp_path / "trusted-local-registration"
    registration_root.mkdir()
    registration_inputs = _local_inputs(registration_root, monkeypatch)
    _register(registration_inputs)
    specs = continuity.freeze_collector_step_schedule(
        registration_file=registration_inputs["output_file"]
    )
    ledger = Path(
        continuity.default_collector_ledger_path(registration_inputs["database_file"])
    )
    symbols = list(json.loads(Path(registration_inputs["panel_file"]).read_bytes()))
    symbols = sorted({entry.split("@", 1)[0] for entry in symbols})
    sessions = sorted(
        {entry.split("@", 1)[1] for entry in json.loads(Path(registration_inputs["panel_file"]).read_bytes())}
    )
    prepared = {
        "database": registration_inputs["database_file"],
        "ledger": ledger,
        "registration": registration_inputs["output_file"],
    }
    with continuity.acquire_collector_phase_lease(ledger) as lease:
        for spec in specs:
            _append_semantic_attempt(prepared, lease, spec, symbols, sessions[0])

    values = _inputs(tmp_path / "inputs")
    values.update(
        database_file=registration_inputs["database_file"],
        registration_file=registration_inputs["output_file"],
        panel_file=registration_inputs["panel_file"],
        source_receipt_files=list(registration_inputs["source_receipt_files"]),
        snapshot_staging_directory=tmp_path / "snapshot-staging",
        source="tencent",
    )
    values["snapshot_staging_directory"].mkdir()
    values["component_files"]["trading_calendar"] = registration_inputs[
        "calendar_file"
    ]
    values["component_files"]["market_rules"] = registration_inputs[
        "market_rules_file"
    ]
    execution_value = {
        "schema_version": "stockdata-execution-adjustment-identity/1",
        "price_role": "execution",
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
    }
    signal_value = {
        "schema_version": "stockdata-signal-adjustment-identity/1",
        "price_role": "signal",
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
    }
    values["execution_adjustment_file"].write_bytes(_canonical(execution_value))
    values["signal_adjustment_file"].write_bytes(_canonical(signal_value))
    execution_adjustment = verify_adjustment_identity(
        execution_value, expected_price_role="execution"
    )
    signal_adjustment = verify_adjustment_identity(
        signal_value, expected_price_role="signal"
    )
    panel = json.loads(Path(values["panel_file"]).read_bytes())
    calendar = json.loads(Path(values["component_files"]["trading_calendar"]).read_bytes())
    decision_cutoffs = {
        record["panel_entry"]: record["payload"]["decision_cutoff_at"]
        for record in calendar["records"]
    }
    intrinsic = reconstruct_intrinsic_evidence(
        values["database_file"],
        panel=panel,
        execution_adjustment=execution_adjustment,
        signal_adjustment=signal_adjustment,
        decision_cutoffs=decision_cutoffs,
    )
    panel = list(panel)
    _, _, component_payloads, _, _ = _export_inputs()
    forward = reconstruct_forward_component_evidence(
        Path(values["database_file"]).read_bytes(),
        panel=panel,
        decision_cutoffs=decision_cutoffs,
    )
    component_values = {
        component: dict(value) for component, value in intrinsic.components.items()
    }
    component_values.update(
        {component: dict(value) for component, value in forward.components.items()}
    )
    for component in REQUIRED_COMPONENTS:
        assert component_payloads[component]["schema_version"] == COMPONENT_SCHEMAS[
            component
        ]
        if component in intrinsic.components:
            values["component_files"][component].write_bytes(
                _canonical(intrinsic.components[component])
            )
        elif component in forward.components:
            values["component_files"][component].write_bytes(
                _canonical(forward.components[component])
            )
        elif component == "trading_calendar":
            component_values[component] = calendar
        elif component == "market_rules":
            component_values[component] = json.loads(
                Path(values["component_files"][component]).read_bytes()
            )
        elif component not in {"trading_calendar", "market_rules"}:
            values["component_files"][component].write_bytes(
                _canonical(component_payloads[component])
            )
            component_values[component] = component_payloads[component]
    values["component_files"]["availability_records"].write_bytes(
        _canonical(_availability_artifact(panel, component_values))
    )
    if component_override is not None:
        values["component_files"][component_override].write_bytes(
            _canonical({"schema_version": COMPONENT_SCHEMAS[component_override]})
        )
    registration = json.loads(Path(values["registration_file"]).read_bytes())
    receipt_ids = sorted(
        hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in values["source_receipt_files"]
    )
    assert registration["schema_version"] == "rqgm-forward-panel-registration/5"
    assert len(receipt_ids) == 2
    assert len(set(receipt_ids)) == 2
    assert registration["prerequisites"]["source_receipt_ids"] == receipt_ids
    result = materialize_provider_bundle(output_dir=tmp_path / "bundle", **values)
    bundle_file = Path(result["bundle_file"])
    assert bundle_file.name == "bundle.json"
    return bundle_file


def _policy_binding(bundle: dict[str, object]) -> dict[str, object]:
    shared_cash = _shared_cash_policy()
    risk = _risk_policy()
    provider_market_rule_reference = bundle["components"]["market_rules"][
        "reference"
    ]
    cost = {
        "schema_version": "stockdata-market-rule-cost-policy-binding/1",
        "policy_reference": {
            "schema_version": "stockdata-test-cost-policy/1",
            "sha256": "c" * 64,
        },
        "market_rule_artifact_reference": {
            "schema_version": provider_market_rule_reference["schema_version"],
            "sha256": provider_market_rule_reference["identifier"],
        },
    }
    cost["sha256"] = _sha(cost)
    return {
        "research_authorization_reference": {
            "schema_version": "stockdata-test-research-authorization/1",
            "sha256": "a" * 64,
        },
        "shared_cash_policy_reference": {
            "schema_version": shared_cash["schema_version"],
            "sha256": _sha(shared_cash),
        },
        "market_rule_cost_policy_binding": cost,
        "risk_policy_reference": {
            "schema_version": risk["schema_version"],
            "sha256": _sha(risk),
        },
    }


def _shared_cash_policy() -> dict[str, object]:
    return {
        "schema_version": "rqgm-trusted-local-shared-cash-policy/1",
        "initial_capital": 1_000_000.0,
        "allocation_policy": "pro_rata_then_ticker",
        "order_priority": "sells_then_buys_then_ticker",
        "single_cash_pool": True,
        "per_symbol_sleeves": False,
    }


def _risk_policy() -> dict[str, object]:
    return {
        "schema_version": "rqgm-trusted-local-risk-policy/1",
        "long_only": True,
        "leverage_allowed": False,
        "target_weight_min": 0.0,
        "target_weight_max": 0.2,
        "gross_target_weight_limit": 0.24,
    }


def test_resolver_signature_is_one_positional_bundle_and_one_required_policy() -> None:
    parameters = inspect.signature(
        resolve_trusted_local_research_replay_inputs
    ).parameters
    assert list(parameters) == ["bundle_file", "replay_policy_binding"]
    assert parameters["bundle_file"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["bundle_file"].default is inspect.Parameter.empty
    assert parameters["replay_policy_binding"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["replay_policy_binding"].default is inspect.Parameter.empty
    assert not {
        "candidate_export",
        "current_db",
        "database",
        "latest",
        "result",
        "completeness",
        "readiness",
        "authority",
        "cache",
        "callback",
        "signature",
    } & set(parameters)


def test_resolver_handoff_is_exact_and_covers_twelve_steps_and_thirty_six_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_file = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_file.read_bytes())
    panel = json.loads(Path(bundle["exact_panel"]["path"]).read_bytes())
    history = parse_collector_ledger(
        Path(bundle["ledger_snapshot"]["path"]).read_bytes()
    )
    completed = [
        event["event"]["step_ordinal"]
        for event in history
        if event["event_type"] == "ATTEMPT_COMPLETED"
    ]
    assert len(panel) == 36
    assert completed == list(range(12))

    resolved = resolve_trusted_local_research_replay_inputs(
        bundle_file,
        replay_policy_binding=_policy_binding(bundle),
    )
    assert set(resolved) == {
        "schema_version",
        "expected_bindings",
        "component_payloads",
    }
    assert resolved["schema_version"] == (
        "stockdata-rqgm-research-replay-resolved-inputs/1"
    )
    assert set(resolved["component_payloads"]) == set(REQUIRED_COMPONENTS)

    export = build_trusted_local_research_replay_export(
        expected_bindings=resolved["expected_bindings"]
    )
    shared_cash = _shared_cash_policy()
    risk = _risk_policy()
    materialization = build_trusted_local_research_replay_materialization(
        provider_export=export,
        expected_bindings=resolved["expected_bindings"],
        component_payloads=resolved["component_payloads"],
        shared_cash_policy_body=shared_cash,
        risk_policy_body=risk,
    )
    assert set(materialization["component_payloads"]) == set(REQUIRED_COMPONENTS)


@pytest.mark.parametrize(
    "mutation",
    [
        "unsigned_registration_receipt",
        "missing_receipt",
        "extra_receipt",
        "duplicate_receipt",
        "receipt_id_body_collision",
        "unsigned_payload_drift_rehashed",
    ],
)
def test_resolver_rejects_receipt_domain_mutations_without_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    bundle_file = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_file.read_bytes())
    bundle_before = _canonical(bundle)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    if mutation == "unsigned_registration_receipt":
        intrinsic = _intrinsic_from_bundle(bundle)
        collector_receipt_id, collector_receipt = next(
            iter(intrinsic.source_receipts.items())
        )
        replacement = tmp_path / "collector-receipt-as-registration.json"
        replacement.write_bytes(_canonical(collector_receipt))
        locator = bundle["source_receipts"][0]
        locator["reference"]["identifier"] = collector_receipt_id
        locator["path"] = str(replacement.resolve())
    elif mutation == "missing_receipt":
        bundle["source_receipts"].pop()
    elif mutation == "extra_receipt":
        original = bundle["source_receipts"][0]
        original_path = Path(original["path"])
        value = json.loads(original_path.read_bytes())
        value["observed_at"] = "2026-08-13T08:01:00+08:00"
        extra_path = tmp_path / "extra-source-receipt.json"
        extra_path.write_bytes(_canonical(value))
        bundle["source_receipts"].append(
            {
                "reference": {
                    **original["reference"],
                    "identifier": _sha(value),
                },
                "path": str(extra_path.resolve()),
            }
        )
    elif mutation == "duplicate_receipt":
        bundle["source_receipts"].append(bundle["source_receipts"][0])
    elif mutation == "receipt_id_body_collision":
        original = bundle["source_receipts"][0]
        replacement = tmp_path / "receipt-id-body-collision.json"
        replacement.write_bytes(Path(original["path"]).read_bytes() + b"\n")
        original["path"] = str(replacement.resolve())
    elif mutation == "unsigned_payload_drift_rehashed":
        locator = bundle["components"]["universe"]
        artifact = json.loads(Path(locator["path"]).read_bytes())
        artifact["records"][0]["payload"]["universe_id"] = "d" * 64
        artifact["records"][0]["record_sha256"] = _sha(
            artifact["records"][0]["payload"]
        )
        replacement = tmp_path / "universe-rehashed-drift.json"
        replacement.write_bytes(_canonical(artifact))
        locator["path"] = str(replacement.resolve())
        locator["reference"]["identifier"] = _sha(artifact)

    assert _canonical(bundle) != bundle_before
    bundle_file.write_bytes(_canonical(bundle))
    with pytest.raises((TypeError, ValueError)):
        resolve_trusted_local_research_replay_inputs(
            bundle_file,
            replay_policy_binding=_policy_binding(bundle),
        )
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert after == before or mutation in {
        "extra_receipt",
        "receipt_id_body_collision",
        "unsigned_payload_drift_rehashed",
        "unsigned_registration_receipt",
    }


def test_registration_source_receipts_reject_a_retained_collector_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_file = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_file.read_bytes())
    intrinsic = _intrinsic_from_bundle(bundle)
    collector_receipt_id = next(iter(intrinsic.source_receipts))
    source_receipts = [
        ProviderArtifactReference.from_dict(locator["reference"])
        for locator in bundle["source_receipts"]
    ]
    source_receipts.append(
        ProviderArtifactReference(
            "stock-data-source-receipt",
            collector_receipt_id,
            "stockdata-source-receipt/1",
        )
    )
    components = {
        component: ProviderArtifactReference.from_dict(locator["reference"])
        for component, locator in bundle["components"].items()
    }
    with pytest.raises(ValueError, match="differ from registration"):
        provider_materializer._require_trusted_local_materialization_inputs(
            registration_raw=Path(bundle["registration"]["path"]).read_bytes(),
            source_receipts=source_receipts,
            components=components,
        )


def test_availability_cannot_use_only_static_registration_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_file = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_file.read_bytes())
    panel = json.loads(Path(bundle["exact_panel"]["path"]).read_bytes())
    component_values = {
        component: json.loads(Path(locator["path"]).read_bytes())
        for component, locator in bundle["components"].items()
    }
    calendar = component_values["trading_calendar"]
    decision_cutoffs = {
        record["panel_entry"]: record["payload"]["decision_cutoff_at"]
        for record in calendar["records"]
    }
    calendar_phases = {
        record["panel_entry"]: {
            field: record["payload"][field]
            for field in (
                "decision_cutoff_at",
                "session_close_at",
                "next_session_decision_cutoff_at",
            )
        }
        for record in calendar["records"]
    }
    static_receipt_ids = sorted(
        hashlib.sha256(Path(locator["path"]).read_bytes()).hexdigest()
        for locator in bundle["source_receipts"]
    )
    with pytest.raises(ValueError, match="unbound"):
        component_availability.verify_component_availability_records(
            component_values["availability_records"],
            expected_panel_sha256=_sha(panel),
            expected_panel_size=len(panel),
            expected_decision_cutoffs=decision_cutoffs,
            bound_source_receipt_ids=static_receipt_ids,
            component_records={
                component: value["records"]
                for component, value in component_values.items()
                if component != "availability_records"
            },
            expected_signed_calendar_phases=calendar_phases,
        )


def test_resolver_does_not_accept_candidate_export_or_authority_overrides() -> None:
    parameters = inspect.signature(
        resolve_trusted_local_research_replay_inputs
    ).parameters
    for forbidden in (
        "candidate_export",
        "result",
        "readiness",
        "authority",
        "completeness",
    ):
        assert forbidden not in parameters


@pytest.mark.parametrize("field", ["database", "ledger_snapshot", "continuity_closure"])
def test_resolver_reuses_retained_identity_checks_for_aba_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    bundle_file = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_file.read_bytes())
    policy = _policy_binding(bundle)
    locator = bundle[field]
    artifact = Path(locator["path"])
    replacement = tmp_path / f"{field}-replacement"
    replacement.write_bytes(artifact.read_bytes() + b"\n")
    locator["path"] = str(replacement)
    locator["reference"]["identifier"] = hashlib.sha256(
        replacement.read_bytes()
    ).hexdigest()
    bundle_file.write_bytes(_canonical(bundle))
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    with pytest.raises((TypeError, ValueError)):
        resolve_trusted_local_research_replay_inputs(
            bundle_file, replay_policy_binding=policy
        )
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_resolver_rejects_inner_locator_aba_after_research_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_file = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_file.read_bytes())
    policy = _policy_binding(bundle)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    replaced: dict[str, Path] = {}
    original = provider_export._resolve_research_inputs

    def resolve_with_inner_aba(*args: object, **kwargs: object) -> dict[str, object]:
        result = original(*args, **kwargs)
        paths = kwargs["paths"]
        components = kwargs["components"]
        retained = paths[components["universe"]]
        inner_path = Path(retained.path)
        replacement = tmp_path / "universe-aba-replacement.json"
        replacement.write_bytes(inner_path.read_bytes())
        os.replace(replacement, inner_path)
        replaced["path"] = inner_path
        return result

    monkeypatch.setattr(
        provider_export, "_resolve_research_inputs", resolve_with_inner_aba
    )
    with pytest.raises((TypeError, ValueError)):
        resolve_trusted_local_research_replay_inputs(
            bundle_file, replay_policy_binding=policy
        )
    assert replaced["path"].is_file()
    assert replaced["path"].read_bytes() == json.dumps(
        json.loads(replaced["path"].read_bytes()),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_resolver_requires_formal_absolute_bundle_and_preserves_regular_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_file = _bundle(tmp_path, monkeypatch)
    bundle = json.loads(bundle_file.read_bytes())
    policy = _policy_binding(bundle)
    assert bundle_file.is_absolute()

    with pytest.raises((TypeError, ValueError)):
        resolve_trusted_local_research_replay_inputs(
            str(bundle_file.relative_to(tmp_path)), replay_policy_binding=policy
        )

    regular = provider_export.export_verified_provider_receipt(bundle_file)
    assert regular["ready"] is False


@pytest.mark.parametrize(
    "component",
    [
        "execution_prices",
        "signal_prices",
        "decision_context",
        "universe",
        "instrument_status",
        "corporate_actions",
        "availability_records",
    ],
)
def test_resolver_rejects_incomplete_non_prerequisite_component_without_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    bundle_file = _bundle(
        tmp_path,
        monkeypatch,
        component_override=component,
    )
    bundle = json.loads(bundle_file.read_bytes())
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    with pytest.raises((TypeError, ValueError)):
        resolve_trusted_local_research_replay_inputs(
            bundle_file,
            replay_policy_binding=_policy_binding(bundle),
        )

    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
