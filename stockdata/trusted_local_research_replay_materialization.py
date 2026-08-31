"""Pure materialization for verified trusted-local E1 research replay inputs."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

from stockdata.rqgm_provider_contract import REQUIRED_COMPONENTS
from stockdata.trusted_local_research_replay_export import (
    RESEARCH_REPLAY_EXPORT_SCHEMA,
    verify_trusted_local_research_replay_export,
)

MATERIALIZATION_SCHEMA = "stockdata-rqgm-research-replay-materialization/1"
EXPECTED_BINDINGS_SCHEMA = "stockdata-rqgm-research-replay-expected-bindings/1"
SHARED_CASH_POLICY_SCHEMA = "rqgm-trusted-local-shared-cash-policy/1"
RISK_POLICY_SCHEMA = "rqgm-trusted-local-risk-policy/1"

_FIELDS = frozenset(
    {
        "schema_version",
        "provider_export_reference",
        "provider_expected_bindings_reference",
        "component_payloads",
        "shared_cash_policy_body",
        "risk_policy_body",
        "materialization_sha256",
    }
)
_REFERENCE_FIELDS = frozenset({"schema_version", "sha256"})
_SHARED_CASH_FIELDS = frozenset(
    {
        "schema_version",
        "initial_capital",
        "allocation_policy",
        "order_priority",
        "single_cash_pool",
        "per_symbol_sleeves",
    }
)
_RISK_FIELDS = frozenset(
    {
        "schema_version",
        "long_only",
        "leverage_allowed",
        "target_weight_min",
        "target_weight_max",
        "gross_target_weight_limit",
    }
)
_FORBIDDEN_CONTENT_FIELDS = frozenset(
    {
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
    }
)


def _canonical_bytes(value: object, field: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not canonical JSON data") from exc


def _sha256(value: object, field: str) -> str:
    return hashlib.sha256(_canonical_bytes(value, field)).hexdigest()


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{field} has an invalid field set")
    return value


def _reference(value: object, field: str) -> Mapping[str, object]:
    reference = _mapping(value, _REFERENCE_FIELDS, field)
    if (
        not isinstance(reference["schema_version"], str)
        or not reference["schema_version"]
    ):
        raise ValueError(f"{field}.schema_version is invalid")
    _digest(reference["sha256"], f"{field}.sha256")
    return reference


def _reject_forbidden_content(value: object, field: str) -> None:
    if isinstance(value, Mapping):
        for name, item in value.items():
            if name in _FORBIDDEN_CONTENT_FIELDS:
                raise ValueError(f"{field} contains forbidden field {name}")
            _reject_forbidden_content(item, field)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_content(item, field)


def _finite_number(value: object, field: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _shared_cash_policy(
    value: object, expected_reference: object
) -> Mapping[str, object]:
    body = _mapping(value, _SHARED_CASH_FIELDS, "shared_cash_policy_body")
    if body["schema_version"] != SHARED_CASH_POLICY_SCHEMA:
        raise ValueError("shared_cash_policy_body schema is invalid")
    if (
        _finite_number(
            body["initial_capital"], "shared_cash_policy_body.initial_capital"
        )
        <= 0
    ):
        raise ValueError("shared_cash_policy_body.initial_capital must be positive")
    if body["allocation_policy"] != "pro_rata_then_ticker":
        raise ValueError("shared_cash_policy_body allocation policy is invalid")
    if body["order_priority"] != "sells_then_buys_then_ticker":
        raise ValueError("shared_cash_policy_body order priority is invalid")
    if body["single_cash_pool"] is not True or body["per_symbol_sleeves"] is not False:
        raise ValueError("shared_cash_policy_body cash scope is invalid")
    reference = _reference(expected_reference, "shared cash policy reference")
    if (
        body["schema_version"] != reference["schema_version"]
        or _sha256(body, "shared_cash_policy_body") != reference["sha256"]
    ):
        raise ValueError("shared_cash_policy_body does not match the export reference")
    return body


def _risk_policy(value: object, expected_reference: object) -> Mapping[str, object]:
    body = _mapping(value, _RISK_FIELDS, "risk_policy_body")
    if body["schema_version"] != RISK_POLICY_SCHEMA:
        raise ValueError("risk_policy_body schema is invalid")
    if body["long_only"] is not True or body["leverage_allowed"] is not False:
        raise ValueError("risk_policy_body trading boundary is invalid")
    minimum = _finite_number(
        body["target_weight_min"], "risk_policy_body.target_weight_min"
    )
    maximum = _finite_number(
        body["target_weight_max"], "risk_policy_body.target_weight_max"
    )
    gross_limit = _finite_number(
        body["gross_target_weight_limit"], "risk_policy_body.gross_target_weight_limit"
    )
    if not 0 <= minimum <= maximum <= 1 or not 0 < gross_limit <= 1:
        raise ValueError("risk_policy_body weight bounds are invalid")
    reference = _reference(expected_reference, "risk policy reference")
    if (
        body["schema_version"] != reference["schema_version"]
        or _sha256(body, "risk_policy_body") != reference["sha256"]
    ):
        raise ValueError("risk_policy_body does not match the export reference")
    return body


def _component_payloads(
    value: object, export: Mapping[str, object]
) -> Mapping[str, object]:
    components = _mapping(value, frozenset(REQUIRED_COMPONENTS), "component_payloads")
    references = export["component_references"]
    if not isinstance(references, Mapping):
        raise TypeError("provider export component references are invalid")
    for component in REQUIRED_COMPONENTS:
        payload = components[component]
        if not isinstance(payload, Mapping):
            raise TypeError(f"component_payloads.{component} must be a JSON object")
        _reject_forbidden_content(payload, f"component_payloads.{component}")
        schema = payload.get("schema_version")
        evidence = references.get(component)
        if not isinstance(evidence, Mapping):
            raise TypeError("provider export component evidence is invalid")
        reference = _reference(
            evidence.get("artifact_reference"),
            f"provider export component reference {component}",
        )
        if (
            schema != reference["schema_version"]
            or _sha256(payload, f"component_payloads.{component}")
            != reference["sha256"]
        ):
            raise ValueError(
                f"component_payloads.{component} does not match the export"
            )
    return components


def _verified_export(
    provider_export: object, expected_bindings: object
) -> Mapping[str, object]:
    return verify_trusted_local_research_replay_export(
        provider_export,
        expected_bindings=expected_bindings,
    )


def _expected_references(
    provider_export: object, expected_bindings: object
) -> tuple[dict[str, str], dict[str, str]]:
    return (
        {
            "schema_version": RESEARCH_REPLAY_EXPORT_SCHEMA,
            "sha256": _sha256(provider_export, "provider_export"),
        },
        {
            "schema_version": EXPECTED_BINDINGS_SCHEMA,
            "sha256": _sha256(expected_bindings, "expected_bindings"),
        },
    )


def _verify_materialization(
    payload: object,
    *,
    provider_export: object,
    expected_bindings: object,
) -> dict[str, object]:
    export = _verified_export(provider_export, expected_bindings)
    materialization = _mapping(payload, _FIELDS, "research replay materialization")
    if materialization["schema_version"] != MATERIALIZATION_SCHEMA:
        raise ValueError("research replay materialization schema is invalid")
    export_reference, bindings_reference = _expected_references(
        provider_export, expected_bindings
    )
    if materialization["provider_export_reference"] != export_reference:
        raise ValueError("provider export reference does not match")
    if materialization["provider_expected_bindings_reference"] != bindings_reference:
        raise ValueError("provider expected bindings reference does not match")
    _component_payloads(materialization["component_payloads"], export)
    policies = export["replay_policy_binding"]
    if not isinstance(policies, Mapping):
        raise TypeError("provider export replay policies are invalid")
    shared_cash = materialization["shared_cash_policy_body"]
    risk = materialization["risk_policy_body"]
    _reject_forbidden_content(shared_cash, "shared_cash_policy_body")
    _reject_forbidden_content(risk, "risk_policy_body")
    _shared_cash_policy(shared_cash, policies.get("shared_cash_policy_reference"))
    _risk_policy(risk, policies.get("risk_policy_reference"))
    digest = _digest(
        materialization["materialization_sha256"], "materialization_sha256"
    )
    body = {
        field: value
        for field, value in materialization.items()
        if field != "materialization_sha256"
    }
    if digest != _sha256(body, "research replay materialization"):
        raise ValueError("research replay materialization identity has drifted")
    return json.loads(
        _canonical_bytes(materialization, "research replay materialization")
    )


def build_trusted_local_research_replay_materialization(
    *,
    provider_export: object,
    expected_bindings: object,
    component_payloads: object,
    shared_cash_policy_body: object,
    risk_policy_body: object,
) -> dict[str, object]:
    """Build one immutable materialization from explicit verified replay inputs."""

    export = _verified_export(provider_export, expected_bindings)
    components = _component_payloads(component_payloads, export)
    policies = export["replay_policy_binding"]
    if not isinstance(policies, Mapping):
        raise TypeError("provider export replay policies are invalid")
    _reject_forbidden_content(shared_cash_policy_body, "shared_cash_policy_body")
    _reject_forbidden_content(risk_policy_body, "risk_policy_body")
    _shared_cash_policy(
        shared_cash_policy_body, policies.get("shared_cash_policy_reference")
    )
    _risk_policy(risk_policy_body, policies.get("risk_policy_reference"))
    export_reference, bindings_reference = _expected_references(
        provider_export, expected_bindings
    )
    body: dict[str, object] = {
        "schema_version": MATERIALIZATION_SCHEMA,
        "provider_export_reference": export_reference,
        "provider_expected_bindings_reference": bindings_reference,
        "component_payloads": components,
        "shared_cash_policy_body": shared_cash_policy_body,
        "risk_policy_body": risk_policy_body,
    }
    payload = {**body, "materialization_sha256": _sha256(body, "materialization")}
    return _verify_materialization(
        payload,
        provider_export=provider_export,
        expected_bindings=expected_bindings,
    )


def verify_trusted_local_research_replay_materialization(
    payload: object,
    *,
    provider_export: object,
    expected_bindings: object,
) -> dict[str, object]:
    """Verify one immutable materialization against explicit replay inputs."""

    return _verify_materialization(
        payload,
        provider_export=provider_export,
        expected_bindings=expected_bindings,
    )


__all__ = [
    "build_trusted_local_research_replay_materialization",
    "verify_trusted_local_research_replay_materialization",
]
