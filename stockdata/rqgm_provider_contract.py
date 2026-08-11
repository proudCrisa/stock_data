"""Versioned stock_data provider contract for RQGM PIT consumers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from stockdata.authority import (
    load_provider_trust_registry,
    verify_authority_envelope,
)
from stockdata.adjustment_identity import (
    EXECUTION_ADJUSTMENT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
)
from stockdata.component_availability import AVAILABILITY_RECORDS_SCHEMA


PROVIDER_CONTRACT_SCHEMA = "stockdata-rqgm-provider-contract/1"
CHECKOUT_SCHEMA = "stockdata-checkout-identity/1"
DATABASE_SCHEMA = "stockdata-database-identity/1"
SOURCE_RECEIPT_SCHEMA = "stockdata-source-receipt/1"
EXACT_PANEL_SCHEMA = "stockdata-exact-panel/1"
READINESS_REPORT_SCHEMA = "stockdata-full-execution-readiness/1"
COMPANION_SNAPSHOT_SCHEMA = "stockdata-companion-snapshot/1"
REPOSITORY_OWNER = "stock_data"

REQUIRED_COMPONENTS = (
    "execution_prices",
    "signal_prices",
    "decision_context",
    "trading_calendar",
    "universe",
    "instrument_status",
    "corporate_actions",
    "market_rules",
    "availability_records",
)

SIGNED_COMPONENTS = frozenset(
    {
        "trading_calendar",
        "universe",
        "instrument_status",
        "corporate_actions",
        "market_rules",
    }
)

COMPONENT_SCHEMAS = {
    component: f"stockdata-{component.replace('_', '-')}/1"
    for component in REQUIRED_COMPONENTS
}
COMPONENT_SCHEMAS["availability_records"] = AVAILABILITY_RECORDS_SCHEMA

_REFERENCE_CONTRACTS = {
    "checkout": ("stock-data-checkout", CHECKOUT_SCHEMA),
    "database": ("stock-data-database", DATABASE_SCHEMA),
    "execution_adjustment_identity": (
        "stock-data-execution-adjustment",
        EXECUTION_ADJUSTMENT_SCHEMA,
    ),
    "signal_adjustment_identity": (
        "stock-data-signal-adjustment",
        SIGNAL_ADJUSTMENT_SCHEMA,
    ),
    "exact_panel": ("stock-data-exact-panel", EXACT_PANEL_SCHEMA),
    "readiness_report": ("stock-data-readiness-report", READINESS_REPORT_SCHEMA),
    "companion_snapshot": (
        "stock-data-companion-snapshot",
        COMPANION_SNAPSHOT_SCHEMA,
    ),
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class ProviderArtifactReference:
    kind: str
    identifier: str
    schema_version: str

    def validate(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("artifact kind must be non-empty")
        _sha256(self.identifier, "artifact identifier")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("artifact schema_version must be non-empty")

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {
            "kind": self.kind,
            "identifier": self.identifier,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ProviderArtifactReference":
        if not isinstance(value, Mapping) or set(value) != {
            "kind",
            "identifier",
            "schema_version",
        }:
            raise ValueError("artifact reference is incomplete")
        return cls(
            kind=value["kind"],
            identifier=value["identifier"],
            schema_version=value["schema_version"],
        )


def _require_reference(
    value: object, field: str, kind: str, schema_version: str
) -> ProviderArtifactReference:
    if not isinstance(value, ProviderArtifactReference):
        raise ValueError(f"{field} must be a ProviderArtifactReference")
    value.validate()
    if value.kind != kind or value.schema_version != schema_version:
        raise ValueError(f"{field} has the wrong kind or schema_version")
    return value


def _contract_payload(
    *,
    checkout: ProviderArtifactReference,
    database: ProviderArtifactReference,
    source_receipts: Sequence[ProviderArtifactReference],
    execution_adjustment_identity: ProviderArtifactReference,
    signal_adjustment_identity: ProviderArtifactReference,
    exact_panel: ProviderArtifactReference,
    readiness_report: ProviderArtifactReference,
    companion_snapshot: ProviderArtifactReference,
) -> dict[str, object]:
    return {
        "schema_version": PROVIDER_CONTRACT_SCHEMA,
        "repository_owner": REPOSITORY_OWNER,
        "checkout": checkout.to_dict(),
        "database": database.to_dict(),
        "source_receipts": [receipt.to_dict() for receipt in source_receipts],
        "execution_adjustment_identity": execution_adjustment_identity.to_dict(),
        "signal_adjustment_identity": signal_adjustment_identity.to_dict(),
        "exact_panel": exact_panel.to_dict(),
        "readiness_report": readiness_report.to_dict(),
        "companion_snapshot": companion_snapshot.to_dict(),
        "required_components": list(REQUIRED_COMPONENTS),
    }


@dataclass(frozen=True)
class RQGMProviderContract:
    contract_sha256: str
    checkout: ProviderArtifactReference
    database: ProviderArtifactReference
    source_receipts: tuple[ProviderArtifactReference, ...]
    execution_adjustment_identity: ProviderArtifactReference
    signal_adjustment_identity: ProviderArtifactReference
    exact_panel: ProviderArtifactReference
    readiness_report: ProviderArtifactReference
    companion_snapshot: ProviderArtifactReference
    schema_version: str = PROVIDER_CONTRACT_SCHEMA
    repository_owner: str = REPOSITORY_OWNER
    required_components: tuple[str, ...] = REQUIRED_COMPONENTS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.schema_version != PROVIDER_CONTRACT_SCHEMA:
            raise ValueError("unsupported provider contract schema")
        if self.repository_owner != REPOSITORY_OWNER:
            raise ValueError("provider contract has the wrong repository owner")
        if self.required_components != REQUIRED_COMPONENTS:
            raise ValueError("provider contract component set is incomplete")
        _sha256(self.contract_sha256, "contract_sha256")
        for field, value in (
            ("checkout", self.checkout),
            ("database", self.database),
            (
                "execution_adjustment_identity",
                self.execution_adjustment_identity,
            ),
            ("signal_adjustment_identity", self.signal_adjustment_identity),
            ("exact_panel", self.exact_panel),
            ("readiness_report", self.readiness_report),
            ("companion_snapshot", self.companion_snapshot),
        ):
            kind, schema_version = _REFERENCE_CONTRACTS[field]
            _require_reference(value, field, kind, schema_version)
        if not isinstance(self.source_receipts, tuple) or not self.source_receipts:
            raise ValueError("source_receipts must be a non-empty immutable tuple")
        identities = []
        for receipt in self.source_receipts:
            _require_reference(
                receipt,
                "source_receipts",
                "stock-data-source-receipt",
                SOURCE_RECEIPT_SCHEMA,
            )
            identities.append(receipt.identifier)
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("source_receipts must be unique and canonically ordered")
        if self.contract_sha256 != _digest(self.identity_payload()):
            raise ValueError("provider contract content identity mismatch")

    def identity_payload(self) -> dict[str, object]:
        return _contract_payload(
            checkout=self.checkout,
            database=self.database,
            source_receipts=self.source_receipts,
            execution_adjustment_identity=self.execution_adjustment_identity,
            signal_adjustment_identity=self.signal_adjustment_identity,
            exact_panel=self.exact_panel,
            readiness_report=self.readiness_report,
            companion_snapshot=self.companion_snapshot,
        )

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {**self.identity_payload(), "contract_sha256": self.contract_sha256}


def build_rqgm_provider_contract(
    *,
    checkout: ProviderArtifactReference,
    database: ProviderArtifactReference,
    source_receipts: Sequence[ProviderArtifactReference],
    execution_adjustment_identity: ProviderArtifactReference,
    signal_adjustment_identity: ProviderArtifactReference,
    exact_panel: ProviderArtifactReference,
    readiness_report: ProviderArtifactReference,
    companion_snapshot: ProviderArtifactReference,
) -> RQGMProviderContract:
    normalized_receipts = tuple(
        sorted(source_receipts, key=lambda receipt: receipt.identifier)
    )
    payload = _contract_payload(
        checkout=checkout,
        database=database,
        source_receipts=normalized_receipts,
        execution_adjustment_identity=execution_adjustment_identity,
        signal_adjustment_identity=signal_adjustment_identity,
        exact_panel=exact_panel,
        readiness_report=readiness_report,
        companion_snapshot=companion_snapshot,
    )
    return RQGMProviderContract(
        contract_sha256=_digest(payload),
        checkout=checkout,
        database=database,
        source_receipts=normalized_receipts,
        execution_adjustment_identity=execution_adjustment_identity,
        signal_adjustment_identity=signal_adjustment_identity,
        exact_panel=exact_panel,
        readiness_report=readiness_report,
        companion_snapshot=companion_snapshot,
    )


def _component_reference(
    value: object, component: str
) -> ProviderArtifactReference:
    reference = ProviderArtifactReference.from_dict(value)
    expected_kind = f"stock-data-{component.replace('_', '-')}"
    if (
        reference.kind != expected_kind
        or reference.schema_version != COMPONENT_SCHEMAS[component]
    ):
        raise ValueError(f"{component} artifact has the wrong kind or schema")
    return reference


def verify_readiness_report(
    report: object,
    contract: RQGMProviderContract,
) -> bool:
    """Verify report structure and identity; return its fail-closed readiness state."""

    contract.validate()
    if not isinstance(report, Mapping):
        raise ValueError("readiness report must be an object")
    if _digest(report) != contract.readiness_report.identifier:
        raise ValueError("readiness report content identity mismatch")
    if report.get("schema_version") != READINESS_REPORT_SCHEMA:
        raise ValueError("unsupported readiness report schema")
    request = report.get("request")
    expected_request_keys = {
        "database_sha256",
        "execution_adjustment_sha256",
        "signal_adjustment_sha256",
        "panel_sha256",
        "panel_size",
        "companion_snapshot_sha256",
    }
    if not isinstance(request, Mapping) or set(request) != expected_request_keys:
        raise ValueError("readiness request identity is incomplete")
    if (
        request["database_sha256"] != contract.database.identifier
        or request["execution_adjustment_sha256"]
        != contract.execution_adjustment_identity.identifier
        or request["signal_adjustment_sha256"]
        != contract.signal_adjustment_identity.identifier
        or request["panel_sha256"] != contract.exact_panel.identifier
        or request["companion_snapshot_sha256"]
        != contract.companion_snapshot.identifier
        or isinstance(request["panel_size"], bool)
        or not isinstance(request["panel_size"], int)
        or request["panel_size"] < 1
    ):
        raise ValueError("readiness request differs from provider contract")
    components = report.get("components")
    if not isinstance(components, Mapping) or set(components) != set(
        REQUIRED_COMPONENTS
    ):
        raise ValueError("readiness report component set is incomplete")
    bound_receipts = {receipt.identifier for receipt in contract.source_receipts}
    authority_registry = None
    component_states = []
    blocked_components = set()
    for component in REQUIRED_COMPONENTS:
        evidence = components[component]
        if not isinstance(evidence, Mapping):
            raise ValueError(f"{component} readiness must be an object")
        ready = evidence.get("ready")
        blockers = evidence.get("blockers")
        if not isinstance(ready, bool) or not isinstance(blockers, list):
            raise ValueError(f"{component} readiness is malformed")
        component_states.append(ready)
        if not ready:
            if not blockers:
                raise ValueError(f"{component} is blocked without a reason")
            blocked_components.add(component)
            continue
        if blockers:
            raise ValueError(f"{component} is ready with blockers")
        artifact = _component_reference(evidence.get("artifact"), component)
        receipt_ids = evidence.get("source_receipt_ids")
        if (
            not isinstance(receipt_ids, list)
            or not receipt_ids
            or any(_sha256(value, "source_receipt_ids") not in bound_receipts for value in receipt_ids)
        ):
            raise ValueError(f"{component} lacks bound source receipts")
        if component in SIGNED_COMPONENTS:
            for field in ("publisher_key_id", "trust_root_id", "signature_id"):
                _sha256(evidence.get(field), f"{component}.{field}")
            if authority_registry is None:
                authority_registry = load_provider_trust_registry()
            verified = verify_authority_envelope(
                evidence.get("authority_envelope"),
                registry=authority_registry,
                expected_component=component,
                expected_artifact=artifact.to_dict(),
                expected_source_receipt_ids=receipt_ids,
            )
            for field in ("publisher_key_id", "trust_root_id", "signature_id"):
                if evidence[field] != getattr(verified, field):
                    raise ValueError(
                        f"{component}.{field} disagrees with verified authority"
                    )
        if component == "availability_records":
            raise ValueError(
                "availability_records requires verified companion calendar cutoffs"
            )
    aggregate_ready = report.get("ready")
    aggregate_blockers = report.get("blockers")
    if not isinstance(aggregate_ready, bool) or not isinstance(aggregate_blockers, list):
        raise ValueError("aggregate readiness is malformed")
    expected_ready = all(component_states) and not aggregate_blockers
    if aggregate_ready != expected_ready:
        raise ValueError("aggregate readiness disagrees with components")
    reported_blocked = {
        item.get("component")
        for item in aggregate_blockers
        if isinstance(item, Mapping)
    }
    if not aggregate_ready and not blocked_components.issubset(reported_blocked):
        raise ValueError("aggregate blockers omit blocked components")
    return aggregate_ready


__all__ = [
    "CHECKOUT_SCHEMA",
    "COMPANION_SNAPSHOT_SCHEMA",
    "COMPONENT_SCHEMAS",
    "DATABASE_SCHEMA",
    "EXACT_PANEL_SCHEMA",
    "EXECUTION_ADJUSTMENT_SCHEMA",
    "PROVIDER_CONTRACT_SCHEMA",
    "ProviderArtifactReference",
    "READINESS_REPORT_SCHEMA",
    "REPOSITORY_OWNER",
    "REQUIRED_COMPONENTS",
    "RQGMProviderContract",
    "SOURCE_RECEIPT_SCHEMA",
    "SIGNAL_ADJUSTMENT_SCHEMA",
    "build_rqgm_provider_contract",
    "verify_readiness_report",
]
