"""Pure verification for the trusted-local E1 research replay export."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo

from stockdata.rqgm_provider_contract import REQUIRED_COMPONENTS
from stockdata.ticker import normalize

RESEARCH_REPLAY_EXPORT_SCHEMA = "stockdata-rqgm-research-replay-export/1"
TRUSTED_LOCAL_REGISTRATION_SCHEMA = "rqgm-forward-panel-registration/5"
TRUSTED_LOCAL_AUTHORITY_MODE = "trusted_local_mechanical"
RESEARCH_SCOPE = "TRUSTED_LOCAL_RESEARCH_ONLY"
E1_RETROSPECTIVE_RESEARCH = "E1_RETROSPECTIVE_RESEARCH"
MARKET_RULE_COST_POLICY_BINDING_SCHEMA = "stockdata-market-rule-cost-policy-binding/1"

_EXPORT_FIELDS = frozenset(
    {
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
)
_REFERENCE_FIELDS = frozenset({"schema_version", "sha256"})
_REGISTRATION_FIELDS = _REFERENCE_FIELDS | frozenset(
    {"authority_mode", "registered_at", "outcome_feedback_used"}
)
_PANEL_FIELDS = frozenset(
    {"sha256", "ordered_cells", "symbol_count", "session_count", "panel_cell_count"}
)
_SCHEDULE_FIELDS = frozenset(
    {"sha256", "terminal_step_count", "completed_step_ordinals"}
)
_ADJUSTMENT_FIELDS = frozenset({"execution", "signal"})
_COMPONENT_FIELDS = frozenset(
    {"artifact_reference", "mechanically_complete", "blockers"}
)
_POLICY_FIELDS = frozenset(
    {
        "research_authorization_reference",
        "shared_cash_policy_reference",
        "market_rule_cost_policy_binding",
        "risk_policy_reference",
    }
)
_MARKET_RULE_COST_POLICY_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "policy_reference",
        "market_rule_artifact_reference",
        "sha256",
    }
)
_OUTCOME_CONTROL_FIELDS = frozenset(
    {"registration_outcome_feedback_used", "eligibility_inputs_outcome_free"}
)
_AUTHORITY_GRANTS = frozenset(
    {
        "component_readiness",
        "execution_readiness",
        "judge",
        "release",
        "production",
        "advice",
    }
)
_BINDING_FIELDS = frozenset(
    {
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
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchReplayExportError(ValueError):
    """The E1 trusted-local research replay export is malformed or unbound."""


def _fail(message: str) -> None:
    raise ResearchReplayExportError(message)


def _canonical_sha256(value: object, field: str) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ResearchReplayExportError(f"{field} is not canonical JSON data") from exc
    return hashlib.sha256(raw).hexdigest()


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(f"{field} must be a lowercase SHA-256 digest")
    return value


def _mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(f"{field} has an invalid field set")
    return value


def _reference(value: object, field: str) -> Mapping[str, object]:
    reference = _mapping(value, _REFERENCE_FIELDS, field)
    if (
        not isinstance(reference["schema_version"], str)
        or not reference["schema_version"]
    ):
        _fail(f"{field}.schema_version is invalid")
    _sha256(reference["sha256"], f"{field}.sha256")
    return reference


def _registration(value: object) -> datetime:
    registration = _mapping(value, _REGISTRATION_FIELDS, "registration_reference")
    if registration["schema_version"] != TRUSTED_LOCAL_REGISTRATION_SCHEMA:
        _fail("registration_reference schema is invalid")
    _sha256(registration["sha256"], "registration_reference.sha256")
    if registration["authority_mode"] != TRUSTED_LOCAL_AUTHORITY_MODE:
        _fail("registration_reference authority mode is invalid")
    if registration["outcome_feedback_used"] is not False:
        _fail("registration_reference must be outcome-free")
    registered_at = registration["registered_at"]
    if not isinstance(registered_at, str):
        _fail("registration_reference.registered_at is invalid")
    try:
        value_at = datetime.fromisoformat(registered_at)
    except ValueError as exc:
        raise ResearchReplayExportError(
            "registration_reference.registered_at is invalid"
        ) from exc
    if value_at.tzinfo is None or value_at.utcoffset() is None:
        _fail("registration_reference.registered_at must include an offset")
    return value_at


def _panel(value: object, registered_at: datetime) -> None:
    panel = _mapping(value, _PANEL_FIELDS, "panel_reference")
    cells = panel["ordered_cells"]
    if not isinstance(cells, list) or len(cells) != 36:
        _fail("panel_reference must contain exactly 36 ordered cells")
    if (
        type(panel["symbol_count"]) is not int
        or type(panel["session_count"]) is not int
        or type(panel["panel_cell_count"]) is not int
        or panel["symbol_count"] != 12
        or panel["session_count"] != 3
        or panel["panel_cell_count"] != 36
    ):
        _fail("panel_reference counts are invalid")
    _sha256(panel["sha256"], "panel_reference.sha256")
    if panel["sha256"] != _canonical_sha256(cells, "panel_reference.ordered_cells"):
        _fail("panel_reference ordered-cell identity has drifted")

    symbols: set[str] = set()
    sessions: set[str] = set()
    for cell in cells:
        if not isinstance(cell, str) or cell.count("@") != 1:
            _fail("panel_reference cell is invalid")
        symbol, session = cell.split("@")
        try:
            if normalize(symbol) != symbol:
                _fail("panel_reference symbol is not canonical")
            parsed_session = date.fromisoformat(session)
        except ValueError as exc:
            raise ResearchReplayExportError("panel_reference cell is invalid") from exc
        if session != parsed_session.isoformat():
            _fail("panel_reference session is not canonical")
        symbols.add(symbol)
        sessions.add(session)
    if len(symbols) != 12 or len(sessions) != 3:
        _fail("panel_reference symbols or sessions are incomplete")
    expected_cells = [
        f"{symbol}@{session}"
        for session in sorted(sessions)
        for symbol in sorted(symbols)
    ]
    if cells != expected_cells:
        _fail("panel_reference cells are not in canonical order")
    registered_date = registered_at.astimezone(_SHANGHAI).date()
    for session in sessions:
        if registered_date >= date.fromisoformat(session):
            _fail("registration must precede every panel session")


def _schedule(value: object) -> None:
    schedule = _mapping(value, _SCHEDULE_FIELDS, "collector_schedule_reference")
    ordinals = schedule["completed_step_ordinals"]
    if (
        type(schedule["terminal_step_count"]) is not int
        or schedule["terminal_step_count"] != 12
        or not isinstance(ordinals, list)
        or any(type(ordinal) is not int for ordinal in ordinals)
        or ordinals != list(range(12))
    ):
        _fail("collector_schedule_reference is incomplete or reordered")
    _sha256(schedule["sha256"], "collector_schedule_reference.sha256")
    if schedule["sha256"] != _canonical_sha256(
        ordinals, "collector_schedule_reference.completed_step_ordinals"
    ):
        _fail("collector_schedule_reference identity has drifted")


def _components(value: object) -> Mapping[str, object]:
    components = _mapping(value, frozenset(REQUIRED_COMPONENTS), "component_references")
    for component in REQUIRED_COMPONENTS:
        item = _mapping(
            components[component],
            _COMPONENT_FIELDS,
            f"component_references.{component}",
        )
        _reference(
            item["artifact_reference"],
            f"component_references.{component}.artifact_reference",
        )
        if item["mechanically_complete"] is not True or item["blockers"] != []:
            _fail(f"component_references.{component} is not mechanically complete")
    return components


def _receipts(value: object, field: str) -> None:
    if not isinstance(value, list) or not value:
        _fail(f"{field} is invalid")
    identities: list[tuple[str, str]] = []
    receipt_ids: list[str] = []
    for index, receipt in enumerate(value):
        reference = _reference(receipt, f"{field}[{index}]")
        identity = (reference["schema_version"], reference["sha256"])
        identities.append(identity)
        receipt_ids.append(identity[1])
    if (
        identities != sorted(identities)
        or len(receipt_ids) != len(set(receipt_ids))
    ):
        _fail(f"{field} must be sorted and contain unique receipt identities")


def _policies(
    value: object,
    market_rule_artifact_reference: object,
    field: str,
) -> Mapping[str, object]:
    policies = _mapping(value, _POLICY_FIELDS, field)
    for name in (
        "research_authorization_reference",
        "shared_cash_policy_reference",
        "risk_policy_reference",
    ):
        _reference(policies[name], f"{field}.{name}")
    binding = _mapping(
        policies["market_rule_cost_policy_binding"],
        _MARKET_RULE_COST_POLICY_BINDING_FIELDS,
        f"{field}.market_rule_cost_policy_binding",
    )
    if binding["schema_version"] != MARKET_RULE_COST_POLICY_BINDING_SCHEMA:
        _fail(f"{field}.market_rule_cost_policy_binding schema is invalid")
    _reference(
        binding["policy_reference"],
        f"{field}.market_rule_cost_policy_binding.policy_reference",
    )
    artifact = _reference(
        binding["market_rule_artifact_reference"],
        f"{field}.market_rule_cost_policy_binding.market_rule_artifact_reference",
    )
    _sha256(binding["sha256"], f"{field}.market_rule_cost_policy_binding.sha256")
    body = {name: item for name, item in binding.items() if name != "sha256"}
    if binding["sha256"] != _canonical_sha256(
        body, f"{field}.market_rule_cost_policy_binding"
    ):
        _fail(f"{field}.market_rule_cost_policy_binding identity has drifted")
    if artifact != market_rule_artifact_reference:
        _fail(f"{field}.market_rule_cost_policy_binding is detached from market rules")
    return policies


def _bindings(value: object) -> Mapping[str, object]:
    bindings = _mapping(value, _BINDING_FIELDS, "expected_bindings")
    registered_at = _registration(bindings["registration_reference"])
    for field in (
        "provider_checkout_reference",
        "provider_bundle_reference",
        "database_snapshot_reference",
        "ledger_snapshot_reference",
        "continuity_closure_reference",
    ):
        _reference(bindings[field], f"expected_bindings.{field}")
    _panel(bindings["panel_reference"], registered_at)
    _schedule(bindings["collector_schedule_reference"])
    _receipts(
        bindings["source_receipt_references"],
        "expected_bindings.source_receipt_references",
    )
    adjustments = _mapping(
        bindings["adjustment_references"],
        _ADJUSTMENT_FIELDS,
        "expected_bindings.adjustment_references",
    )
    _reference(
        adjustments["execution"], "expected_bindings.adjustment_references.execution"
    )
    _reference(adjustments["signal"], "expected_bindings.adjustment_references.signal")
    components = _components(bindings["component_references"])
    _policies(
        bindings["replay_policy_binding"],
        components["market_rules"]["artifact_reference"],
        "expected_bindings.replay_policy_binding",
    )
    return bindings


def build_trusted_local_research_replay_export(
    *, expected_bindings: object
) -> dict[str, object]:
    """Build one verified E1-only trusted-local research replay export."""

    bindings = _bindings(expected_bindings)
    export = {
        "schema_version": RESEARCH_REPLAY_EXPORT_SCHEMA,
        "research_replay_eligible": True,
        "scope": RESEARCH_SCOPE,
        "max_evidence_grade": E1_RETROSPECTIVE_RESEARCH,
        **{field: bindings[field] for field in _BINDING_FIELDS},
        "outcome_control": {
            "registration_outcome_feedback_used": False,
            "eligibility_inputs_outcome_free": True,
        },
        "blockers": [],
        "task_8_6b_credit": False,
        "authority_grants": {grant: False for grant in _AUTHORITY_GRANTS},
    }
    return verify_trusted_local_research_replay_export(
        export, expected_bindings=expected_bindings
    )


def verify_trusted_local_research_replay_export(
    payload: object, *, expected_bindings: object
) -> dict[str, object]:
    """Verify one immutable E1-only trusted-local research replay export.

    ``expected_bindings`` is the required independent provider-side closure. Every
    registration, artifact, panel, schedule, receipt, adjustment, component, and
    policy identity must match it exactly.
    """

    export = _mapping(payload, _EXPORT_FIELDS, "research replay export")
    if (
        export["schema_version"] != RESEARCH_REPLAY_EXPORT_SCHEMA
        or export["research_replay_eligible"] is not True
        or export["scope"] != RESEARCH_SCOPE
        or export["max_evidence_grade"] != E1_RETROSPECTIVE_RESEARCH
        or export["task_8_6b_credit"] is not False
        or export["blockers"] != []
    ):
        _fail("research replay export crosses its E1-only boundary")

    registered_at = _registration(export["registration_reference"])
    for field in (
        "provider_checkout_reference",
        "provider_bundle_reference",
        "database_snapshot_reference",
        "ledger_snapshot_reference",
        "continuity_closure_reference",
    ):
        _reference(export[field], field)
    _panel(export["panel_reference"], registered_at)
    _schedule(export["collector_schedule_reference"])

    _receipts(export["source_receipt_references"], "source_receipt_references")
    adjustments = _mapping(
        export["adjustment_references"], _ADJUSTMENT_FIELDS, "adjustment_references"
    )
    execution = _reference(adjustments["execution"], "adjustment_references.execution")
    signal = _reference(adjustments["signal"], "adjustment_references.signal")
    if execution == signal:
        _fail("execution and signal adjustments must remain separate")
    components = _components(export["component_references"])
    _policies(
        export["replay_policy_binding"],
        components["market_rules"]["artifact_reference"],
        "replay_policy_binding",
    )
    outcome_control = _mapping(
        export["outcome_control"], _OUTCOME_CONTROL_FIELDS, "outcome_control"
    )
    if (
        outcome_control["registration_outcome_feedback_used"] is not False
        or outcome_control["eligibility_inputs_outcome_free"] is not True
    ):
        _fail("research replay export is not outcome-free")
    grants = _mapping(export["authority_grants"], _AUTHORITY_GRANTS, "authority_grants")
    if any(value is not False for value in grants.values()):
        _fail("research replay export grants authority")

    bindings = _bindings(expected_bindings)
    for field in _BINDING_FIELDS:
        if export[field] != bindings[field]:
            _fail(f"{field} does not match the independent binding")

    try:
        return json.loads(
            json.dumps(
                export,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ResearchReplayExportError(
            "research replay export is not canonical JSON data"
        ) from exc


__all__ = [
    "ResearchReplayExportError",
    "build_trusted_local_research_replay_export",
    "verify_trusted_local_research_replay_export",
]
