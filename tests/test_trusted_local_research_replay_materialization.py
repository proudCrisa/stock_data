from __future__ import annotations

import hashlib
import inspect
import json
from copy import deepcopy
from typing import Callable

import pytest

from stockdata.rqgm_provider_contract import COMPONENT_SCHEMAS, REQUIRED_COMPONENTS
from stockdata.trusted_local_research_replay_export import (
    build_trusted_local_research_replay_export,
    verify_trusted_local_research_replay_export,
)
from stockdata.trusted_local_research_replay_materialization import (
    build_trusted_local_research_replay_materialization,
    verify_trusted_local_research_replay_materialization,
)

MATERIALIZATION_SCHEMA = "stockdata-rqgm-research-replay-materialization/1"
EXPORT_SCHEMA = "stockdata-rqgm-research-replay-export/1"
EXPECTED_BINDINGS_SCHEMA = "stockdata-rqgm-research-replay-expected-bindings/1"
FIELDS = {
    "schema_version",
    "provider_export_reference",
    "provider_expected_bindings_reference",
    "component_payloads",
    "shared_cash_policy_body",
    "risk_policy_body",
    "materialization_sha256",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _reference(name: str, schema: str = "stockdata-test-reference/1") -> dict[str, str]:
    return {"schema_version": schema, "sha256": _sha(name)}


def _export_inputs() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, dict[str, object]],
    dict[str, object],
    dict[str, object],
]:
    symbols = [f"{index:06d}.SH" for index in range(1, 7)] + [
        f"{index:06d}.SZ" for index in range(7, 13)
    ]
    sessions = ["2026-09-07", "2026-09-08", "2026-09-09"]
    ordered_cells = [
        f"{symbol}@{session}"
        for session in sessions
        for symbol in symbols
    ]
    registration = {
        "schema_version": "rqgm-forward-panel-registration/5",
        "sha256": _sha("registration"),
        "authority_mode": "trusted_local_mechanical",
        "registered_at": "2026-08-27T09:00:00+08:00",
        "outcome_feedback_used": False,
    }
    component_payloads = {
        component: {
            "schema_version": COMPONENT_SCHEMAS[component],
            "component": component,
            "records": [{"symbol": symbols[0], "session": sessions[0]}],
        }
        for component in REQUIRED_COMPONENTS
    }
    component_references = {
        component: {
            "artifact_reference": {
                "schema_version": COMPONENT_SCHEMAS[component],
                "sha256": _sha(component_payloads[component]),
            },
            "mechanically_complete": True,
            "blockers": [],
        }
        for component in REQUIRED_COMPONENTS
    }
    shared_cash_policy_body = {
        "schema_version": "rqgm-trusted-local-shared-cash-policy/1",
        "initial_capital": 100000.0,
        "allocation_policy": "pro_rata_then_ticker",
        "order_priority": "sells_then_buys_then_ticker",
        "single_cash_pool": True,
        "per_symbol_sleeves": False,
    }
    risk_policy_body = {
        "schema_version": "rqgm-trusted-local-risk-policy/1",
        "long_only": True,
        "leverage_allowed": False,
        "target_weight_min": 0.0,
        "target_weight_max": 0.25,
        "gross_target_weight_limit": 1.0,
    }
    replay_policy_binding = {
        "research_authorization_reference": _reference("research-authorization"),
        "shared_cash_policy_reference": {
            "schema_version": shared_cash_policy_body["schema_version"],
            "sha256": _sha(shared_cash_policy_body),
        },
        "market_rule_cost_policy_binding": {
            "schema_version": "stockdata-market-rule-cost-policy-binding/1",
            "policy_reference": _reference("market-rule-cost-policy"),
            "market_rule_artifact_reference": component_references["market_rules"][
                "artifact_reference"
            ],
        },
        "risk_policy_reference": {
            "schema_version": risk_policy_body["schema_version"],
            "sha256": _sha(risk_policy_body),
        },
    }
    replay_policy_binding["market_rule_cost_policy_binding"]["sha256"] = _sha(
        {
            key: value
            for key, value in replay_policy_binding[
                "market_rule_cost_policy_binding"
            ].items()
            if key != "sha256"
        }
    )
    expected_source = {
        "schema_version": EXPORT_SCHEMA,
        "research_replay_eligible": True,
        "scope": "TRUSTED_LOCAL_RESEARCH_ONLY",
        "max_evidence_grade": "E1_RETROSPECTIVE_RESEARCH",
        "registration_reference": registration,
        "provider_checkout_reference": _reference("checkout"),
        "provider_bundle_reference": _reference("bundle"),
        "database_snapshot_reference": _reference("database"),
        "ledger_snapshot_reference": _reference("ledger"),
        "continuity_closure_reference": _reference("continuity"),
        "panel_reference": {
            "sha256": _sha(ordered_cells),
            "ordered_cells": ordered_cells,
            "symbol_count": 12,
            "session_count": 3,
            "panel_cell_count": 36,
        },
        "collector_schedule_reference": {
            "sha256": _sha(list(range(12))),
            "terminal_step_count": 12,
            "completed_step_ordinals": list(range(12)),
        },
        "source_receipt_references": [_reference("receipt-0"), _reference("receipt-1")],
        "adjustment_references": {
            "execution": _reference("execution-adjustment"),
            "signal": _reference("signal-adjustment"),
        },
        "component_references": component_references,
        "replay_policy_binding": replay_policy_binding,
        "outcome_control": {
            "registration_outcome_feedback_used": False,
            "eligibility_inputs_outcome_free": True,
        },
        "blockers": [],
        "task_8_6b_credit": False,
        "authority_grants": {
            grant: False
            for grant in (
                "component_readiness",
                "execution_readiness",
                "judge",
                "release",
                "production",
                "advice",
            )
        },
    }
    expected_bindings = {
        field: deepcopy(expected_source[field])
        for field in (
            "registration_reference",
            "provider_checkout_reference",
            "provider_bundle_reference",
            "database_snapshot_reference",
            "ledger_snapshot_reference",
            "continuity_closure_reference",
            "panel_reference",
            "collector_schedule_reference",
            "source_receipt_references",
            "adjustment_references",
            "component_references",
            "replay_policy_binding",
        )
    }
    export = build_trusted_local_research_replay_export(
        expected_bindings=expected_bindings,
    )
    verify_trusted_local_research_replay_export(
        export,
        expected_bindings=expected_bindings,
    )
    return export, expected_bindings, component_payloads, shared_cash_policy_body, risk_policy_body


def _build() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    export, expected, components, shared_cash, risk = _export_inputs()
    materialization = build_trusted_local_research_replay_materialization(
        provider_export=export,
        expected_bindings=expected,
        component_payloads=components,
        shared_cash_policy_body=shared_cash,
        risk_policy_body=risk,
    )
    return materialization, export, expected


def _body(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "materialization_sha256"}


def _rehash(payload: dict[str, object]) -> None:
    payload["materialization_sha256"] = _sha(_body(payload))


def test_materialization_has_exact_shape_and_reproduces_all_explicit_bytes() -> None:
    materialization, export, expected = _build()

    assert set(materialization) == FIELDS
    assert materialization["schema_version"] == MATERIALIZATION_SCHEMA
    assert materialization["provider_export_reference"] == {
        "schema_version": EXPORT_SCHEMA,
        "sha256": _sha(export),
    }
    assert materialization["provider_expected_bindings_reference"] == {
        "schema_version": EXPECTED_BINDINGS_SCHEMA,
        "sha256": _sha(expected),
    }
    assert set(materialization["component_payloads"]) == set(REQUIRED_COMPONENTS)
    assert materialization["component_payloads"] == _export_inputs()[2]
    assert materialization["shared_cash_policy_body"] == _export_inputs()[3]
    assert materialization["risk_policy_body"] == _export_inputs()[4]
    assert set(materialization["shared_cash_policy_body"]) == {
        "schema_version",
        "initial_capital",
        "allocation_policy",
        "order_priority",
        "single_cash_pool",
        "per_symbol_sleeves",
    }
    assert materialization["shared_cash_policy_body"] == {
        "schema_version": "rqgm-trusted-local-shared-cash-policy/1",
        "initial_capital": 100000.0,
        "allocation_policy": "pro_rata_then_ticker",
        "order_priority": "sells_then_buys_then_ticker",
        "single_cash_pool": True,
        "per_symbol_sleeves": False,
    }
    assert set(materialization["risk_policy_body"]) == {
        "schema_version",
        "long_only",
        "leverage_allowed",
        "target_weight_min",
        "target_weight_max",
        "gross_target_weight_limit",
    }
    assert materialization["risk_policy_body"] == {
        "schema_version": "rqgm-trusted-local-risk-policy/1",
        "long_only": True,
        "leverage_allowed": False,
        "target_weight_min": 0.0,
        "target_weight_max": 0.25,
        "gross_target_weight_limit": 1.0,
    }
    assert materialization["materialization_sha256"] == _sha(_body(materialization))
    assert not any(
        field in materialization
        for field in (
            "path",
            "latest",
            "current_database",
            "cache",
            "callback",
            "candidate_result",
            "plan",
            "readiness",
            "authority_grants",
        )
    )


def test_builder_and_verifier_do_not_mutate_inputs() -> None:
    export, expected, components, shared_cash, risk = _export_inputs()
    original = deepcopy((export, expected, components, shared_cash, risk))

    materialization = build_trusted_local_research_replay_materialization(
        provider_export=export,
        expected_bindings=expected,
        component_payloads=components,
        shared_cash_policy_body=shared_cash,
        risk_policy_body=risk,
    )
    verify_trusted_local_research_replay_materialization(
        materialization,
        provider_export=export,
        expected_bindings=expected,
    )

    assert (export, expected, components, shared_cash, risk) == original


def test_public_api_has_only_the_explicit_keyword_contract() -> None:
    build_parameters = inspect.signature(
        build_trusted_local_research_replay_materialization
    ).parameters
    verify_parameters = inspect.signature(
        verify_trusted_local_research_replay_materialization
    ).parameters

    assert list(build_parameters) == [
        "provider_export",
        "expected_bindings",
        "component_payloads",
        "shared_cash_policy_body",
        "risk_policy_body",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in build_parameters.values()
    )
    assert list(verify_parameters) == [
        "payload",
        "provider_export",
        "expected_bindings",
    ]
    assert verify_parameters["payload"].kind is not inspect.Parameter.KEYWORD_ONLY
    assert all(
        verify_parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("provider_export", "expected_bindings")
    )
    for forbidden in ("path", "default", "writer", "callback"):
        assert forbidden not in build_parameters
        assert forbidden not in verify_parameters


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("materialization_sha256"),
        lambda payload: payload.update({"extra": True}),
        lambda payload: payload["component_payloads"].pop("market_rules"),
        lambda payload: payload["component_payloads"].update({"extra": {}}),
    ],
)
def test_verifier_rejects_missing_or_extra_fields(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    materialization, export, expected = _build()
    mutation(materialization)

    with pytest.raises(ValueError):
        verify_trusted_local_research_replay_materialization(
            materialization,
            provider_export=export,
            expected_bindings=expected,
        )


def test_verifier_rejects_component_payload_drift_after_aggregate_rehash() -> None:
    materialization, export, expected = _build()
    materialization["component_payloads"]["market_rules"]["records"][0][
        "symbol"
    ] = "000002.SH"
    _rehash(materialization)

    with pytest.raises(ValueError):
        verify_trusted_local_research_replay_materialization(
            materialization,
            provider_export=export,
            expected_bindings=expected,
        )


def test_verifier_rejects_policy_body_drift_after_aggregate_rehash() -> None:
    materialization, export, expected = _build()
    materialization["risk_policy_body"]["max_position_weight"] = "0.30"
    _rehash(materialization)

    with pytest.raises(ValueError):
        verify_trusted_local_research_replay_materialization(
            materialization,
            provider_export=export,
            expected_bindings=expected,
        )


def test_verifier_rejects_export_and_expected_closure_drift() -> None:
    materialization, export, expected = _build()
    export["scope"] = "PIT_EXECUTION"
    with pytest.raises(ValueError):
        verify_trusted_local_research_replay_materialization(
            materialization,
            provider_export=export,
            expected_bindings=expected,
        )

    materialization, export, expected = _build()
    expected["panel_reference"]["ordered_cells"][0] = "000002.SH@2026-09-07"
    with pytest.raises(ValueError):
        verify_trusted_local_research_replay_materialization(
            materialization,
            provider_export=export,
            expected_bindings=expected,
        )


def test_verifier_rejects_materialization_hash_drift() -> None:
    materialization, export, expected = _build()
    materialization["materialization_sha256"] = "f" * 64

    with pytest.raises(ValueError):
        verify_trusted_local_research_replay_materialization(
            materialization,
            provider_export=export,
            expected_bindings=expected,
        )


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "path",
        "latest",
        "current_database",
        "cache",
        "callback",
        "candidate_result",
        "result",
        "plan",
        "readiness",
        "authority_grants",
    ],
)
def test_builder_rejects_mutable_lookup_and_result_shaped_provider_inputs(
    forbidden_field: str,
) -> None:
    export, expected, components, shared_cash, risk = _export_inputs()
    export[forbidden_field] = True

    with pytest.raises(ValueError):
        build_trusted_local_research_replay_materialization(
            provider_export=export,
            expected_bindings=expected,
            component_payloads=components,
            shared_cash_policy_body=shared_cash,
            risk_policy_body=risk,
        )


def test_builder_rejects_noncanonical_nan_component_bytes() -> None:
    export, expected, components, shared_cash, risk = _export_inputs()
    components["market_rules"]["records"][0]["value"] = float("nan")

    with pytest.raises(ValueError):
        build_trusted_local_research_replay_materialization(
            provider_export=export,
            expected_bindings=expected,
            component_payloads=components,
            shared_cash_policy_body=shared_cash,
            risk_policy_body=risk,
        )


@pytest.mark.parametrize(
    "mutate_shared, mutate_risk",
    [
        (lambda body: body.pop("allocation_policy"), lambda body: None),
        (lambda body: body.update({"extra": True}), lambda body: None),
        (lambda body: body.__setitem__("initial_capital", 0), lambda body: None),
        (lambda body: body.__setitem__("single_cash_pool", False), lambda body: None),
        (lambda body: None, lambda body: body.pop("long_only")),
        (lambda body: None, lambda body: body.update({"extra": True})),
        (lambda body: None, lambda body: body.__setitem__("target_weight_min", 0.5)),
        (lambda body: None, lambda body: body.__setitem__("gross_target_weight_limit", 0)),
    ],
)
def test_builder_rejects_non_exact_shared_cash_or_risk_policy_bodies(
    mutate_shared: Callable[[dict[str, object]], None],
    mutate_risk: Callable[[dict[str, object]], None],
) -> None:
    export, expected, components, shared_cash, risk = _export_inputs()
    mutate_shared(shared_cash)
    mutate_risk(risk)

    with pytest.raises(ValueError):
        build_trusted_local_research_replay_materialization(
            provider_export=export,
            expected_bindings=expected,
            component_payloads=components,
            shared_cash_policy_body=shared_cash,
            risk_policy_body=risk,
        )


def test_materialization_is_e1_research_only_and_grants_no_authority() -> None:
    materialization, export, _ = _build()

    assert export["max_evidence_grade"] == "E1_RETROSPECTIVE_RESEARCH"
    assert export["task_8_6b_credit"] is False
    assert all(value is False for value in export["authority_grants"].values())
    assert materialization["schema_version"] == MATERIALIZATION_SCHEMA
    assert set(materialization) == FIELDS
