from __future__ import annotations

import hashlib
import inspect
import json
from copy import deepcopy
from typing import Callable

import pytest

from stockdata.provider_export import (
    EXPORT_SCHEMA,
    TRUSTED_LOCAL_READINESS_BLOCKER,
    TRUSTED_LOCAL_REGISTRATION_SCHEMA,
)
from stockdata.rqgm_provider_contract import REQUIRED_COMPONENTS
from stockdata.trusted_local_research_replay_export import (
    ResearchReplayExportError,
    build_trusted_local_research_replay_export,
    verify_trusted_local_research_replay_export,
)

RESEARCH_EXPORT_SCHEMA = "stockdata-rqgm-research-replay-export/1"
RESEARCH_SCOPE = "TRUSTED_LOCAL_RESEARCH_ONLY"
E1 = "E1_RETROSPECTIVE_RESEARCH"
EXPORT_FIELDS = {
    "schema_version",
    "research_replay_eligible",
    "scope",
    "max_evidence_grade",
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
    "outcome_control",
    "blockers",
    "task_8_6b_credit",
    "authority_grants",
}
AUTHORITY_GRANTS = (
    "component_readiness",
    "execution_readiness",
    "judge",
    "release",
    "production",
    "advice",
)
PANEL_SYMBOLS = tuple(f"{index:06d}.SZ" for index in range(1, 13))
PANEL_SESSIONS = ("2026-09-07", "2026-09-08", "2026-09-09")
SOURCE_RECEIPT_NAMES = ("source-receipt-0", "source-receipt-1")
PANEL_CELLS = [
    f"{symbol}@{session}"
    for session in PANEL_SESSIONS
    for symbol in PANEL_SYMBOLS
]


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


def _reference(name: str) -> dict[str, str]:
    return {
        "schema_version": f"stockdata-test-{name}/1",
        "sha256": _sha(name),
    }


def _source_receipts() -> list[dict[str, str]]:
    return [_reference(name) for name in SOURCE_RECEIPT_NAMES]


def _market_rule_cost_policy_binding() -> dict[str, object]:
    binding: dict[str, object] = {
        "schema_version": "stockdata-market-rule-cost-policy-binding/1",
        "policy_reference": _reference("market-rule-cost-policy"),
        "market_rule_artifact_reference": _reference("component-market_rules"),
    }
    binding["sha256"] = _sha(binding)
    return binding


def _expected_bindings() -> dict[str, object]:
    """Independent provider-side closure expected by the read-only verifier."""
    ordered_cells = [
        f"{symbol}@{session}"
        for session in PANEL_SESSIONS
        for symbol in PANEL_SYMBOLS
    ]
    completed_step_ordinals = list(range(12))
    return {
        "registration_reference": {
            "schema_version": TRUSTED_LOCAL_REGISTRATION_SCHEMA,
            "sha256": _sha("registration"),
            "authority_mode": "trusted_local_mechanical",
            "registered_at": "2026-08-27T09:00:00+08:00",
            "outcome_feedback_used": False,
        },
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
            "sha256": _sha(completed_step_ordinals),
            "terminal_step_count": 12,
            "completed_step_ordinals": completed_step_ordinals,
        },
        "source_receipt_references": _source_receipts(),
        "adjustment_references": {
            "execution": _reference("execution-adjustment"),
            "signal": _reference("signal-adjustment"),
        },
        "component_references": {
            component: {
                "artifact_reference": _reference(f"component-{component}"),
                "mechanically_complete": True,
                "blockers": [],
            }
            for component in REQUIRED_COMPONENTS
        },
        "replay_policy_binding": {
            "research_authorization_reference": _reference("research-authorization"),
            "shared_cash_policy_reference": _reference("shared-cash-policy"),
            "market_rule_cost_policy_binding": _market_rule_cost_policy_binding(),
            "risk_policy_reference": _reference("risk-policy"),
        },
    }


def _eligible_export() -> dict[str, object]:
    ordered_cells = list(PANEL_CELLS)
    return {
        "schema_version": RESEARCH_EXPORT_SCHEMA,
        "research_replay_eligible": True,
        "scope": RESEARCH_SCOPE,
        "max_evidence_grade": E1,
        "registration_reference": {
            "schema_version": TRUSTED_LOCAL_REGISTRATION_SCHEMA,
            "sha256": _sha("registration"),
            "authority_mode": "trusted_local_mechanical",
            "registered_at": "2026-08-27T09:00:00+08:00",
            "outcome_feedback_used": False,
        },
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
        "source_receipt_references": _source_receipts(),
        "adjustment_references": {
            "execution": _reference("execution-adjustment"),
            "signal": _reference("signal-adjustment"),
        },
        "component_references": {
            component: {
                "artifact_reference": _reference(f"component-{component}"),
                "mechanically_complete": True,
                "blockers": [],
            }
            for component in REQUIRED_COMPONENTS
        },
        "replay_policy_binding": {
            "research_authorization_reference": _reference("research-authorization"),
            "shared_cash_policy_reference": _reference("shared-cash-policy"),
            "market_rule_cost_policy_binding": _market_rule_cost_policy_binding(),
            "risk_policy_reference": _reference("risk-policy"),
        },
        "outcome_control": {
            "registration_outcome_feedback_used": False,
            "eligibility_inputs_outcome_free": True,
        },
        "blockers": [],
        "task_8_6b_credit": False,
        "authority_grants": {grant: False for grant in AUTHORITY_GRANTS},
    }


def _verify(
    payload: dict[str, object],
    expected_bindings: dict[str, object] | None = None,
) -> dict[str, object]:
    verified = verify_trusted_local_research_replay_export(
        payload,
        expected_bindings=(
            expected_bindings
            if expected_bindings is not None
            else _expected_bindings()
        ),
    )
    assert isinstance(verified, dict)
    return verified


def test_export_builder_has_only_one_keyword_only_required_argument() -> None:
    parameters = inspect.signature(
        build_trusted_local_research_replay_export
    ).parameters

    assert list(parameters) == ["expected_bindings"]
    assert parameters["expected_bindings"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["expected_bindings"].default is inspect.Parameter.empty
    assert not {
        "path",
        "current",
        "database",
        "cache",
        "callback",
        "result",
        "authority",
        "signature",
    } & set(parameters)


def test_export_builder_matches_independent_eligible_fixture_and_does_not_alias() -> None:
    expected = _expected_bindings()
    before = deepcopy(expected)

    payload = build_trusted_local_research_replay_export(
        expected_bindings=expected,
    )
    assert payload == _eligible_export()
    assert _verify(payload, expected) == payload
    assert expected == before

    payload["panel_reference"]["ordered_cells"][0] = "000002.SZ@2026-09-07"
    assert expected == before


@pytest.mark.parametrize(
    "expected_bindings",
    [
        None,
        [],
        {},
        {**_expected_bindings(), "extra": True},
        {
            key: value
            for key, value in _expected_bindings().items()
            if key != "panel_reference"
        },
    ],
)
def test_export_builder_rejects_missing_extra_or_non_object_closure(
    expected_bindings: object,
) -> None:
    with pytest.raises(ResearchReplayExportError):
        build_trusted_local_research_replay_export(
            expected_bindings=expected_bindings,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["panel_reference"]["ordered_cells"].__setitem__(
            0, "000002.SZ@2026-09-07"
        ),
        lambda value: value["collector_schedule_reference"][
            "completed_step_ordinals"
        ].pop(),
        lambda value: value["source_receipt_references"].__setitem__(
            1, value["source_receipt_references"][0]
        ),
        lambda value: value["component_references"]["market_rules"].update(
            {"mechanically_complete": False}
        ),
        lambda value: value["replay_policy_binding"][
            "market_rule_cost_policy_binding"
        ].update({"sha256": "f" * 64}),
    ],
)
def test_export_builder_rejects_panel_schedule_receipt_component_and_cost_drift(
    mutation: Callable[[dict[str, object]], object],
) -> None:
    expected = _expected_bindings()
    mutation(expected)
    before = deepcopy(expected)

    with pytest.raises(ResearchReplayExportError):
        build_trusted_local_research_replay_export(
            expected_bindings=expected,
        )
    assert expected == before


def test_eligible_export_has_exact_shape_and_never_claims_readiness() -> None:
    payload = _eligible_export()
    verified = _verify(payload)

    assert set(verified) == EXPORT_FIELDS
    assert "ready" not in verified
    assert verified["schema_version"] == RESEARCH_EXPORT_SCHEMA
    assert verified["research_replay_eligible"] is True
    assert verified["scope"] == RESEARCH_SCOPE
    assert verified["max_evidence_grade"] == E1
    assert verified["task_8_6b_credit"] is False
    assert verified["authority_grants"] == {grant: False for grant in AUTHORITY_GRANTS}
    assert set(verified["registration_reference"]) == {
        "schema_version",
        "sha256",
        "authority_mode",
        "registered_at",
        "outcome_feedback_used",
    }
    assert set(verified["panel_reference"]) == {
        "sha256",
        "ordered_cells",
        "symbol_count",
        "session_count",
        "panel_cell_count",
    }
    assert verified["panel_reference"]["ordered_cells"] == PANEL_CELLS
    assert verified["panel_reference"]["panel_cell_count"] == 36
    assert verified["panel_reference"]["sha256"] == _sha(
        verified["panel_reference"]["ordered_cells"]
    )
    assert set(verified["collector_schedule_reference"]) == {
        "sha256",
        "terminal_step_count",
        "completed_step_ordinals",
    }
    assert verified["collector_schedule_reference"]["completed_step_ordinals"] == list(
        range(12)
    )
    assert verified["collector_schedule_reference"]["sha256"] == _sha(
        verified["collector_schedule_reference"]["completed_step_ordinals"]
    )
    assert set(verified["adjustment_references"]) == {"execution", "signal"}
    assert set(verified["component_references"]) == set(REQUIRED_COMPONENTS)
    assert all(
        set(value) == {"artifact_reference", "mechanically_complete", "blockers"}
        and value["mechanically_complete"] is True
        and value["blockers"] == []
        for value in verified["component_references"].values()
    )
    assert set(verified["replay_policy_binding"]) == {
        "research_authorization_reference",
        "shared_cash_policy_reference",
        "market_rule_cost_policy_binding",
        "risk_policy_reference",
    }
    cost_binding = verified["replay_policy_binding"][
        "market_rule_cost_policy_binding"
    ]
    assert set(cost_binding) == {
        "schema_version",
        "policy_reference",
        "market_rule_artifact_reference",
        "sha256",
    }
    assert cost_binding["schema_version"] == (
        "stockdata-market-rule-cost-policy-binding/1"
    )
    assert cost_binding["sha256"] == _sha(
        {key: value for key, value in cost_binding.items() if key != "sha256"}
    )
    assert cost_binding["market_rule_artifact_reference"] == verified[
        "component_references"
    ]["market_rules"]["artifact_reference"]
    assert verified["outcome_control"] == {
        "registration_outcome_feedback_used": False,
        "eligibility_inputs_outcome_free": True,
    }


def test_verifier_requires_independent_expected_bindings_keyword() -> None:
    payload = _eligible_export()

    with pytest.raises(TypeError):
        verify_trusted_local_research_replay_export(payload)
    with pytest.raises(ResearchReplayExportError):
        verify_trusted_local_research_replay_export(
            payload,
            expected_bindings=None,
        )


def test_expected_bindings_cover_the_complete_ordered_closure() -> None:
    assert set(_expected_bindings()) == {
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
    }
    assert _expected_bindings()["source_receipt_references"] == _source_receipts()
    expected_cost_binding = _expected_bindings()["replay_policy_binding"][
        "market_rule_cost_policy_binding"
    ]
    assert set(expected_cost_binding) == {
        "schema_version",
        "policy_reference",
        "market_rule_artifact_reference",
        "sha256",
    }
    assert expected_cost_binding["sha256"] == _sha(
        {key: value for key, value in expected_cost_binding.items() if key != "sha256"}
    )


def test_export_rejects_cost_binding_body_hash_drift() -> None:
    payload = _eligible_export()
    binding = payload["replay_policy_binding"]["market_rule_cost_policy_binding"]
    binding["sha256"] = "f" * 64

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


def test_export_rejects_detached_cost_binding_even_when_expected_is_changed() -> None:
    payload = _eligible_export()
    expected = _expected_bindings()
    detached = _market_rule_cost_policy_binding()
    detached["policy_reference"] = _reference("detached-cost-policy")
    detached["market_rule_artifact_reference"] = _reference("detached-market-rules")
    detached["sha256"] = _sha(
        {key: value for key, value in detached.items() if key != "sha256"}
    )
    payload["replay_policy_binding"]["market_rule_cost_policy_binding"] = deepcopy(
        detached
    )
    expected["replay_policy_binding"]["market_rule_cost_policy_binding"] = deepcopy(
        detached
    )

    with pytest.raises(ResearchReplayExportError):
        _verify(payload, expected)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda binding: binding.pop("policy_reference"),
        lambda binding: binding.update({"extra": True}),
    ],
)
def test_export_rejects_missing_or_extra_cost_binding_fields(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    payload = _eligible_export()
    mutation(payload["replay_policy_binding"]["market_rule_cost_policy_binding"])

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


def test_export_rejects_rehashed_panel_when_expected_panel_differs() -> None:
    payload = _eligible_export()
    panel = payload["panel_reference"]
    cells = panel["ordered_cells"]
    assert isinstance(cells, list)
    cells[0] = "000001.SH@2026-09-07"
    panel["sha256"] = _sha(cells)

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


def test_export_rejects_rehashed_schedule_when_expected_schedule_differs() -> None:
    payload = _eligible_export()
    schedule = payload["collector_schedule_reference"]
    steps = schedule["completed_step_ordinals"]
    assert isinstance(steps, list)
    steps[0] = 12
    schedule["sha256"] = _sha(steps)

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


def test_existing_provider_export_remains_separate_and_v5_readiness_negative() -> None:
    assert EXPORT_SCHEMA == "stockdata-rqgm-provider-export/1"
    assert TRUSTED_LOCAL_REGISTRATION_SCHEMA == "rqgm-forward-panel-registration/5"
    assert TRUSTED_LOCAL_READINESS_BLOCKER == (
        "trusted_local_mechanical_has_no_readiness_authority"
    )

    regular_provider_export = {
        "schema_version": EXPORT_SCHEMA,
        "ready": False,
        "readiness_report": {
            "ready": False,
            "blockers": [{"code": TRUSTED_LOCAL_READINESS_BLOCKER}],
        },
    }
    assert regular_provider_export["ready"] is False
    with pytest.raises(ResearchReplayExportError):
        _verify(regular_provider_export)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("schema_version"),
        lambda value: value.update({"extra": True}),
        lambda value: value["registration_reference"].pop("sha256"),
        lambda value: value["panel_reference"].pop("ordered_cells"),
        lambda value: value["outcome_control"].update(
            {"eligibility_inputs_outcome_free": False}
        ),
        lambda value: value["authority_grants"].update({"judge": True}),
    ],
)
def test_export_rejects_missing_extra_or_forbidden_nested_fields(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    payload = _eligible_export()
    mutation(payload)

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope", "PIT_EXECUTION"),
        ("max_evidence_grade", "E2_PIT_SEARCH"),
        ("task_8_6b_credit", True),
        ("research_replay_eligible", False),
    ],
)
def test_export_fixes_e1_boundary_and_8_6b_credit(
    field: str, value: object
) -> None:
    payload = _eligible_export()
    payload[field] = value

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda panel: panel.pop(),
        lambda panel: panel.append("999999.SZ@2026-09-09"),
        lambda panel: panel.__setitem__(0, panel[1]),
        lambda panel: panel.__setitem__(0, panel[0].replace("000001", "000002")),
    ],
)
def test_export_rejects_missing_extra_duplicate_or_reordered_panel_cells(
    mutation: Callable[[list[str]], None],
) -> None:
    payload = _eligible_export()
    panel = payload["panel_reference"]["ordered_cells"]
    assert isinstance(panel, list)
    mutation(panel)

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda steps: steps.pop(),
        lambda steps: steps.append(12),
        lambda steps: steps.__setitem__(0, steps[1]),
        lambda steps: steps.reverse(),
    ],
)
def test_export_rejects_missing_extra_duplicate_or_reordered_terminal_steps(
    mutation: Callable[[list[int]], None],
) -> None:
    payload = _eligible_export()
    steps = payload["collector_schedule_reference"]["completed_step_ordinals"]
    assert isinstance(steps, list)
    mutation(steps)

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("provider_checkout_reference",),
        ("provider_bundle_reference",),
        ("database_snapshot_reference",),
        ("ledger_snapshot_reference",),
        ("continuity_closure_reference",),
        ("registration_reference",),
        ("source_receipt_references", 0),
        ("adjustment_references", "execution"),
        ("replay_policy_binding", "market_rule_cost_policy_binding", "sha256"),
        ("component_references", "market_rules", "artifact_reference"),
    ],
)
def test_export_rejects_artifact_receipt_component_and_adjustment_hash_drift(
    path: tuple[object, ...],
) -> None:
    payload = _eligible_export()
    current: object = payload
    for part in path[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            assert isinstance(current, dict)
            current = current[part]
    last = path[-1]
    if isinstance(current, list):
        current[int(last)] = {"schema_version": "drift/1", "sha256": "f" * 64}
    else:
        assert isinstance(current, dict)
        if "sha256" in current:
            current["sha256"] = "f" * 64
        elif last in current:
            value = current[last]
            assert isinstance(value, dict)
            value["sha256"] = "f" * 64
        else:
            raise AssertionError(f"unexpected test path: {path}")

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["source_receipt_references"].pop(),
        lambda value: value["source_receipt_references"].append(_reference("unused")),
        lambda value: value["source_receipt_references"].reverse(),
        lambda value: value["component_references"]["market_rules"].update(
            {"mechanically_complete": False}
        ),
        lambda value: value["component_references"]["market_rules"]["blockers"].append(
            {"code": "incomplete"}
        ),
        lambda value: value["adjustment_references"].pop("signal"),
    ],
)
def test_export_requires_exact_receipt_component_and_adjustment_closure(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    payload = _eligible_export()
    mutation(payload)

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["registration_reference"].update(
            {"registered_at": "2026-09-08T09:00:00+08:00"}
        ),
        lambda value: value["registration_reference"].update(
            {"authority_mode": "ambiguous"}
        ),
        lambda value: value["outcome_control"].update(
            {"registration_outcome_feedback_used": True}
        ),
        lambda value: value["blockers"].append({"code": "historical_backfill"}),
        lambda value: value["blockers"].append({"code": "late_capture"}),
        lambda value: value.update({"result_feedback": {"used": True}}),
    ],
)
def test_export_rejects_stale_ambiguous_late_backfilled_or_result_influenced_inputs(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    payload = _eligible_export()
    mutation(payload)

    with pytest.raises(ResearchReplayExportError):
        _verify(payload)


def test_export_fixture_is_not_mutated_by_verification() -> None:
    payload = _eligible_export()
    before = deepcopy(payload)

    _verify(payload)

    assert payload == before
