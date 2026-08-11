"""Content-addressed companion snapshots for complete RQGM data authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import hashlib
import json

from stockdata.authority import (
    PROVIDER_TRUST_REGISTRY_SHA256,
    load_provider_trust_registry,
)
from stockdata.adjustment_identity import verify_adjustment_identity
from stockdata.rqgm_provider_contract import (
    CHECKOUT_SCHEMA,
    COMPONENT_SCHEMAS,
    COMPANION_SNAPSHOT_SCHEMA,
    DATABASE_SCHEMA,
    EXACT_PANEL_SCHEMA,
    EXECUTION_ADJUSTMENT_SCHEMA,
    REQUIRED_COMPONENTS,
    SIGNAL_ADJUSTMENT_SCHEMA,
    SOURCE_RECEIPT_SCHEMA,
    ProviderArtifactReference,
    RQGMProviderContract,
    verify_readiness_report,
)


def _identity_payload(
    *,
    schema_version: str,
    coverage_start: str,
    coverage_end: str,
    trust_registry_sha256: str,
    checkout: ProviderArtifactReference,
    database: ProviderArtifactReference,
    source_receipts: Sequence[ProviderArtifactReference],
    execution_adjustment_identity: ProviderArtifactReference,
    signal_adjustment_identity: ProviderArtifactReference,
    exact_panel: ProviderArtifactReference,
    components: Sequence[tuple[str, ProviderArtifactReference]],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "trust_registry_sha256": trust_registry_sha256,
        "checkout": checkout.to_dict(),
        "database": database.to_dict(),
        "source_receipts": [receipt.to_dict() for receipt in source_receipts],
        "execution_adjustment_identity": execution_adjustment_identity.to_dict(),
        "signal_adjustment_identity": signal_adjustment_identity.to_dict(),
        "exact_panel": exact_panel.to_dict(),
        "components": {
            name: reference.to_dict() for name, reference in components
        },
    }


@dataclass(frozen=True)
class CompanionSnapshot:
    snapshot_sha256: str
    coverage_start: str
    coverage_end: str
    checkout: ProviderArtifactReference
    database: ProviderArtifactReference
    source_receipts: tuple[ProviderArtifactReference, ...]
    execution_adjustment_identity: ProviderArtifactReference
    signal_adjustment_identity: ProviderArtifactReference
    exact_panel: ProviderArtifactReference
    components: tuple[tuple[str, ProviderArtifactReference], ...]
    trust_registry_sha256: str = PROVIDER_TRUST_REGISTRY_SHA256
    schema_version: str = COMPANION_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.schema_version != COMPANION_SNAPSHOT_SCHEMA:
            raise ValueError("unsupported companion snapshot schema")
        _date(self.coverage_start, "coverage_start")
        _date(self.coverage_end, "coverage_end")
        if self.coverage_start > self.coverage_end:
            raise ValueError("companion snapshot coverage is reversed")
        _sha256(self.snapshot_sha256, "snapshot_sha256")
        _sha256(self.trust_registry_sha256, "trust_registry_sha256")
        expected_references = (
            (self.checkout, "stock-data-checkout", CHECKOUT_SCHEMA),
            (self.database, "stock-data-database", DATABASE_SCHEMA),
            (
                self.execution_adjustment_identity,
                "stock-data-execution-adjustment",
                EXECUTION_ADJUSTMENT_SCHEMA,
            ),
            (
                self.signal_adjustment_identity,
                "stock-data-signal-adjustment",
                SIGNAL_ADJUSTMENT_SCHEMA,
            ),
            (self.exact_panel, "stock-data-exact-panel", EXACT_PANEL_SCHEMA),
        )
        for reference, kind, schema_version in expected_references:
            reference.validate()
            if reference.kind != kind or reference.schema_version != schema_version:
                raise ValueError("companion snapshot reference has wrong kind or schema")
        if not self.source_receipts:
            raise ValueError("companion snapshot requires source receipts")
        receipt_ids = [receipt.identifier for receipt in self.source_receipts]
        for receipt in self.source_receipts:
            receipt.validate()
            if (
                receipt.kind != "stock-data-source-receipt"
                or receipt.schema_version != SOURCE_RECEIPT_SCHEMA
            ):
                raise ValueError("companion snapshot source receipt has wrong kind")
        if receipt_ids != sorted(receipt_ids) or len(receipt_ids) != len(
            set(receipt_ids)
        ):
            raise ValueError(
                "companion snapshot source receipts must be sorted and unique"
            )
        component_names = [name for name, _ in self.components]
        if component_names != list(REQUIRED_COMPONENTS):
            raise ValueError("companion snapshot component set or order is invalid")
        for name, reference in self.components:
            reference.validate()
            if reference.kind != f"stock-data-{name.replace('_', '-')}" or (
                reference.schema_version != COMPONENT_SCHEMAS[name]
            ):
                raise ValueError(f"companion snapshot {name} reference is invalid")
        if self.snapshot_sha256 != _digest(self.identity_payload()):
            raise ValueError("companion snapshot content identity mismatch")

    def identity_payload(self) -> dict[str, object]:
        return _identity_payload(
            schema_version=self.schema_version,
            coverage_start=self.coverage_start,
            coverage_end=self.coverage_end,
            trust_registry_sha256=self.trust_registry_sha256,
            checkout=self.checkout,
            database=self.database,
            source_receipts=self.source_receipts,
            execution_adjustment_identity=self.execution_adjustment_identity,
            signal_adjustment_identity=self.signal_adjustment_identity,
            exact_panel=self.exact_panel,
            components=self.components,
        )

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            **self.identity_payload(),
            "snapshot_sha256": self.snapshot_sha256,
        }


@dataclass(frozen=True)
class VerifiedCompanionSnapshot:
    snapshot: CompanionSnapshot
    verified_artifacts: tuple[tuple[ProviderArtifactReference, bytes], ...]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical ISO date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical ISO date")
    return value


def build_companion_snapshot(
    *,
    coverage_start: str,
    coverage_end: str,
    checkout: ProviderArtifactReference,
    database: ProviderArtifactReference,
    source_receipts: Sequence[ProviderArtifactReference],
    execution_adjustment_identity: ProviderArtifactReference,
    signal_adjustment_identity: ProviderArtifactReference,
    exact_panel: ProviderArtifactReference,
    components: Mapping[str, ProviderArtifactReference],
) -> CompanionSnapshot:
    """Build one deterministic manifest without reading or rewriting provider data."""

    normalized_receipts = tuple(
        sorted(source_receipts, key=lambda receipt: receipt.identifier)
    )
    normalized_components = tuple(
        (name, components[name]) for name in REQUIRED_COMPONENTS
    ) if set(components) == set(REQUIRED_COMPONENTS) else ()
    values = {
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "checkout": checkout,
        "database": database,
        "source_receipts": normalized_receipts,
        "execution_adjustment_identity": execution_adjustment_identity,
        "signal_adjustment_identity": signal_adjustment_identity,
        "exact_panel": exact_panel,
        "components": normalized_components,
        "trust_registry_sha256": PROVIDER_TRUST_REGISTRY_SHA256,
        "schema_version": COMPANION_SNAPSHOT_SCHEMA,
    }
    snapshot_sha256 = _digest(_identity_payload(**values))
    return CompanionSnapshot(**{**values, "snapshot_sha256": snapshot_sha256})


def companion_snapshot_from_dict(value: object) -> CompanionSnapshot:
    """Parse a strict serialized companion snapshot."""

    required = {
        "schema_version",
        "snapshot_sha256",
        "coverage_start",
        "coverage_end",
        "trust_registry_sha256",
        "checkout",
        "database",
        "source_receipts",
        "execution_adjustment_identity",
        "signal_adjustment_identity",
        "exact_panel",
        "components",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("serialized companion snapshot is incomplete")
    receipts = value["source_receipts"]
    components = value["components"]
    if not isinstance(receipts, list) or not isinstance(components, Mapping):
        raise ValueError("serialized companion references are malformed")
    if set(components) != set(REQUIRED_COMPONENTS):
        raise ValueError("serialized companion component set is incomplete")
    return CompanionSnapshot(
        schema_version=value["schema_version"],
        snapshot_sha256=value["snapshot_sha256"],
        coverage_start=value["coverage_start"],
        coverage_end=value["coverage_end"],
        trust_registry_sha256=value["trust_registry_sha256"],
        checkout=ProviderArtifactReference.from_dict(value["checkout"]),
        database=ProviderArtifactReference.from_dict(value["database"]),
        source_receipts=tuple(
            ProviderArtifactReference.from_dict(receipt) for receipt in receipts
        ),
        execution_adjustment_identity=ProviderArtifactReference.from_dict(
            value["execution_adjustment_identity"]
        ),
        signal_adjustment_identity=ProviderArtifactReference.from_dict(
            value["signal_adjustment_identity"]
        ),
        exact_panel=ProviderArtifactReference.from_dict(value["exact_panel"]),
        components=tuple(
            (
                component,
                ProviderArtifactReference.from_dict(components[component]),
            )
            for component in REQUIRED_COMPONENTS
        ),
    )


def verify_companion_snapshot(
    snapshot: CompanionSnapshot | Mapping[str, object],
    *,
    content_reader: Callable[[ProviderArtifactReference], bytes],
) -> VerifiedCompanionSnapshot:
    """Re-read and hash every authority before returning a verified snapshot."""

    parsed = (
        snapshot
        if isinstance(snapshot, CompanionSnapshot)
        else companion_snapshot_from_dict(snapshot)
    )
    parsed.validate()
    if load_provider_trust_registry().registry_sha256 != (
        parsed.trust_registry_sha256
    ):
        raise ValueError("companion snapshot trust registry has drifted")
    references = (
        parsed.checkout,
        parsed.database,
        *parsed.source_receipts,
        parsed.execution_adjustment_identity,
        parsed.signal_adjustment_identity,
        parsed.exact_panel,
        *(reference for _, reference in parsed.components),
    )
    verified = []
    for reference in references:
        raw = content_reader(reference)
        if not isinstance(raw, bytes):
            raise ValueError("companion content reader must return bytes")
        if hashlib.sha256(raw).hexdigest() != reference.identifier:
            raise ValueError(
                f"companion {reference.kind} artifact content has drifted"
            )
        verified.append((reference, raw))
    verified_content = dict(verified)
    for role, reference in (
        ("execution", parsed.execution_adjustment_identity),
        ("signal", parsed.signal_adjustment_identity),
    ):
        try:
            adjustment = json.loads(verified_content[reference].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{role} adjustment identity is invalid JSON") from exc
        if verify_adjustment_identity(
            adjustment, expected_price_role=role
        ).identifier != reference.identifier:
            raise ValueError(f"{role} adjustment identity content mismatch")
    return VerifiedCompanionSnapshot(
        snapshot=parsed,
        verified_artifacts=tuple(verified),
    )


def verify_bound_readiness(
    *,
    report: object,
    contract: RQGMProviderContract,
    companion_snapshot: CompanionSnapshot | Mapping[str, object],
    content_reader: Callable[[ProviderArtifactReference], bytes],
) -> bool:
    """Verify all companion bytes before exposing a readiness decision."""

    verified = verify_companion_snapshot(
        companion_snapshot, content_reader=content_reader
    )
    snapshot = verified.snapshot
    if contract.companion_snapshot.identifier != snapshot.snapshot_sha256:
        raise ValueError("provider contract binds a different companion snapshot")
    expected = (
        (contract.checkout, snapshot.checkout),
        (contract.database, snapshot.database),
        (
            contract.execution_adjustment_identity,
            snapshot.execution_adjustment_identity,
        ),
        (
            contract.signal_adjustment_identity,
            snapshot.signal_adjustment_identity,
        ),
        (contract.exact_panel, snapshot.exact_panel),
    )
    if any(contract_ref != snapshot_ref for contract_ref, snapshot_ref in expected):
        raise ValueError("provider contract differs from companion authorities")
    if contract.source_receipts != snapshot.source_receipts:
        raise ValueError("provider contract differs from companion source receipts")
    if isinstance(report, Mapping):
        components = report.get("components")
        snapshot_components = dict(snapshot.components)
        if isinstance(components, Mapping):
            for component in REQUIRED_COMPONENTS:
                evidence = components.get(component)
                if isinstance(evidence, Mapping) and evidence.get("ready") is True:
                    if ProviderArtifactReference.from_dict(
                        evidence.get("artifact")
                    ) != snapshot_components[component]:
                        raise ValueError(
                            f"{component} readiness differs from companion snapshot"
                        )
    return verify_readiness_report(report, contract)
