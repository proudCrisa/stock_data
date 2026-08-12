"""Materialize a content-addressed, fail-closed RQGM provider bundle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import shutil

from stockdata.adjustment_identity import (
    EXECUTION_ADJUSTMENT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
    verify_adjustment_identity,
)
from stockdata.companion_snapshot import build_companion_snapshot
from stockdata.execution_readiness import load_panel
from stockdata.full_execution_readiness import check_full_execution_readiness
from stockdata.authority import load_provider_trust_registry
from stockdata.provider_authority_admission import (
    AdmittedProviderAuthority,
    SIGNED_COMPONENTS,
    admit_signed_component_authority,
)
from stockdata.provider_export import EXPORT_SCHEMA, export_verified_provider_receipt
from stockdata.rqgm_provider_contract import (
    CHECKOUT_SCHEMA,
    COMPONENT_SCHEMAS,
    DATABASE_SCHEMA,
    EXACT_PANEL_SCHEMA,
    READINESS_REPORT_SCHEMA,
    REQUIRED_COMPONENTS,
    SOURCE_RECEIPT_SCHEMA,
    ProviderArtifactReference,
)


class ProviderMaterializationError(ValueError):
    """Raised when an input cannot safely enter a provider evidence closure."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _read_regular(path: str | Path, field: str) -> bytes:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ProviderMaterializationError(f"{field} must name a regular file")
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise ProviderMaterializationError(f"{field} is unreadable") from exc


def _json(raw: bytes, field: str) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderMaterializationError(f"{field} must be valid JSON") from exc


def _reference(kind: str, schema: str, raw: bytes) -> ProviderArtifactReference:
    return ProviderArtifactReference(kind, hashlib.sha256(raw).hexdigest(), schema)


def _write_artifact(directory: Path, raw: bytes) -> Path:
    target = directory / "artifacts" / hashlib.sha256(raw).hexdigest()
    if not target.exists():
        target.write_bytes(raw)
    return target


def _locator(reference: ProviderArtifactReference, path: Path) -> dict[str, object]:
    return {"reference": reference.to_dict(), "path": str(path)}


def _canonical_panel(panel_file: str | Path) -> tuple[list[str], tuple[tuple[str, str], ...]]:
    try:
        panel = tuple(load_panel(str(panel_file)))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderMaterializationError("panel_file is invalid") from exc
    if not panel:
        raise ProviderMaterializationError("panel_file must contain at least one slot")
    canonical = sorted({f"{symbol}@{day}" for symbol, day in panel})
    if len(canonical) != len(panel):
        raise ProviderMaterializationError("panel_file contains duplicate slots")
    return canonical, panel


def _source_receipt(raw: bytes, field: str) -> None:
    value = _json(raw, field)
    if not isinstance(value, Mapping):
        raise ProviderMaterializationError(f"{field} must be a JSON object")
    schema = value.get("schema_version")
    if isinstance(schema, str) and "research" in schema.lower():
        raise ProviderMaterializationError(f"{field} is research-only and cannot enter provider evidence")


def _blocked_report(
    *,
    full_report: Mapping[str, object],
    database: ProviderArtifactReference,
    execution_adjustment: ProviderArtifactReference,
    signal_adjustment: ProviderArtifactReference,
    panel: ProviderArtifactReference,
    panel_size: int,
    companion_sha256: str,
    admitted_authorities: Mapping[str, AdmittedProviderAuthority] | None = None,
) -> dict[str, object]:
    reported_components = full_report.get("components")
    if not isinstance(reported_components, Mapping):
        raise ProviderMaterializationError("full readiness component report is malformed")

    components: dict[str, dict[str, object]] = {}
    blockers: list[dict[str, object]] = []
    admitted_authorities = admitted_authorities or {}
    for component in REQUIRED_COMPONENTS:
        admitted = admitted_authorities.get(component)
        if admitted is not None:
            evidence = admitted.readiness_evidence()
            components[component] = evidence
            continue
        value = reported_components.get(component)
        if not isinstance(value, Mapping):
            raise ProviderMaterializationError(f"full readiness lacks {component}")
        original_blockers = value.get("blockers")
        if not isinstance(original_blockers, list):
            raise ProviderMaterializationError(f"full readiness blockers for {component} are malformed")
        component_blockers = [dict(item) for item in original_blockers if isinstance(item, Mapping)]
        if len(component_blockers) != len(original_blockers):
            raise ProviderMaterializationError(f"full readiness blocker for {component} is malformed")
        # The local readiness checker has no independent component-attestation input.
        # Preserve its verdict but never allow this materializer to manufacture one.
        component_blockers.append(
            {"code": "provider_component_authority_not_attested", "count": 1}
        )
        components[component] = {"ready": False, "blockers": component_blockers}
        blockers.extend({**item, "component": component} for item in component_blockers)

    return {
        "schema_version": READINESS_REPORT_SCHEMA,
        "ready": False,
        "request": {
            "database_sha256": database.identifier,
            "execution_adjustment_sha256": execution_adjustment.identifier,
            "signal_adjustment_sha256": signal_adjustment.identifier,
            "panel_sha256": panel.identifier,
            "panel_size": panel_size,
            "companion_snapshot_sha256": companion_sha256,
        },
        "blockers": blockers,
        "components": components,
    }


def materialize_provider_bundle(
    *,
    output_dir: str | Path,
    database_file: str | Path,
    panel_file: str | Path,
    source_receipt_files: Sequence[str | Path],
    execution_adjustment_file: str | Path,
    signal_adjustment_file: str | Path,
    component_files: Mapping[str, str | Path],
    component_authority_files: Mapping[str, str | Path] | None = None,
    source: str,
) -> dict[str, object]:
    """Create one immutable, independently verifiable, but fail-closed bundle.

    This function only binds files supplied by the caller. It does not fetch data,
    repair evidence, or attest authority; absence of those facts remains blocked.
    """

    destination = Path(output_dir).expanduser().resolve()
    if destination.exists():
        raise ProviderMaterializationError("output_dir must not already exist")
    if set(component_files) != set(REQUIRED_COMPONENTS):
        raise ProviderMaterializationError("component_files must contain every required component exactly once")
    authority_files = dict(component_authority_files or {})
    if not set(authority_files).issubset(SIGNED_COMPONENTS):
        raise ProviderMaterializationError(
            "component_authority_files contain unsupported component"
        )
    if not isinstance(source, str) or not source:
        raise ProviderMaterializationError("source must be non-empty")
    if not source_receipt_files:
        raise ProviderMaterializationError("at least one source receipt is required")

    panel_entries, panel = _canonical_panel(panel_file)
    panel_raw = _canonical(panel_entries)
    database_raw = _read_regular(database_file, "database_file")
    execution_value = _json(
        _read_regular(execution_adjustment_file, "execution_adjustment_file"),
        "execution_adjustment_file",
    )
    signal_value = _json(
        _read_regular(signal_adjustment_file, "signal_adjustment_file"),
        "signal_adjustment_file",
    )
    execution_verified = verify_adjustment_identity(
        execution_value, expected_price_role="execution"
    )
    signal_verified = verify_adjustment_identity(signal_value, expected_price_role="signal")
    if execution_verified.source != source or signal_verified.source != source:
        raise ProviderMaterializationError(
            "adjustment identities must use the declared price source"
        )
    execution_raw = _canonical(execution_value)
    signal_raw = _canonical(signal_value)
    if hashlib.sha256(execution_raw).hexdigest() != execution_verified.identifier:
        raise AssertionError("execution adjustment canonical identity drifted")
    if hashlib.sha256(signal_raw).hexdigest() != signal_verified.identifier:
        raise AssertionError("signal adjustment canonical identity drifted")

    receipt_raws = [_read_regular(path, f"source_receipt_files[{index}]") for index, path in enumerate(source_receipt_files)]
    for index, raw in enumerate(receipt_raws):
        _source_receipt(raw, f"source_receipt_files[{index}]")
    if len(receipt_raws) != len(set(receipt_raws)):
        raise ProviderMaterializationError("source_receipt_files must not repeat an artifact")
    receipt_raws = sorted(receipt_raws, key=lambda raw: hashlib.sha256(raw).hexdigest())
    component_raws = {
        component: _read_regular(path, f"component_files.{component}")
        for component, path in component_files.items()
    }
    component_values = {
        component: _json(raw, f"component_files[{component}]")
        for component, raw in component_raws.items()
    }
    for component in SIGNED_COMPONENTS.intersection(authority_files):
        if _canonical(component_values[component]) != component_raws[component]:
            raise ProviderMaterializationError(
                f"component_files[{component}] must be canonical JSON"
            )
    if any(not raw for raw in component_raws.values()):
        raise ProviderMaterializationError("component files must not be empty")

    destination.mkdir(parents=True)
    (destination / "artifacts").mkdir()
    try:
        checkout_raw = _canonical({"schema_version": CHECKOUT_SCHEMA, "repository": "stock_data"})
        checkout = _reference("stock-data-checkout", CHECKOUT_SCHEMA, checkout_raw)
        database = _reference("stock-data-database", DATABASE_SCHEMA, database_raw)
        execution_adjustment = _reference(
            "stock-data-execution-adjustment", EXECUTION_ADJUSTMENT_SCHEMA, execution_raw
        )
        signal_adjustment = _reference(
            "stock-data-signal-adjustment", SIGNAL_ADJUSTMENT_SCHEMA, signal_raw
        )
        exact_panel = _reference("stock-data-exact-panel", EXACT_PANEL_SCHEMA, panel_raw)
        source_receipts = tuple(
            _reference("stock-data-source-receipt", SOURCE_RECEIPT_SCHEMA, raw)
            for raw in receipt_raws
        )
        source_receipt_values = {
            reference.identifier: _json(raw, "source_receipt_files")
            for reference, raw in zip(source_receipts, receipt_raws)
        }
        components = {
            component: _reference(
                f"stock-data-{component.replace('_', '-')}",
                COMPONENT_SCHEMAS[component],
                component_raws[component],
            )
            for component in REQUIRED_COMPONENTS
        }

        paths = {
            checkout: _write_artifact(destination, checkout_raw),
            database: _write_artifact(destination, database_raw),
            execution_adjustment: _write_artifact(destination, execution_raw),
            signal_adjustment: _write_artifact(destination, signal_raw),
            exact_panel: _write_artifact(destination, panel_raw),
        }
        paths.update(
            {_reference("stock-data-source-receipt", SOURCE_RECEIPT_SCHEMA, raw): _write_artifact(destination, raw) for raw in receipt_raws}
        )
        paths.update(
            {components[component]: _write_artifact(destination, component_raws[component]) for component in REQUIRED_COMPONENTS}
        )

        full_report = check_full_execution_readiness(
            database_file,
            source=source,
            adjustment_mode=execution_verified.adjustment_mode,
            adjustment_version=execution_verified.adjustment_version,
            signal_adjustment_mode=signal_verified.adjustment_mode,
            signal_adjustment_version=signal_verified.adjustment_version,
            panel=panel,
        )
        admitted_authorities = {}
        if authority_files:
            registry = load_provider_trust_registry()
            ordered_components = sorted(
                authority_files,
                key=lambda component: (component != "trading_calendar", component),
            )
            for component in ordered_components:
                path = authority_files[component]
                envelope = _json(
                    _read_regular(path, f"component_authority_files[{component}]"),
                    f"component_authority_files[{component}]",
                )
                admitted_authorities[component] = admit_signed_component_authority(
                    component=component,
                    artifact_value=component_values[component],
                    authority_envelope=envelope,
                    expected_panel=panel_entries,
                    bound_source_receipts=source_receipt_values,
                    registry=registry,
                    decision_cutoff_by_panel=(
                        admitted_authorities["trading_calendar"].decision_cutoff_by_panel
                        if component != "trading_calendar"
                        and "trading_calendar" in admitted_authorities
                        else None
                    ),
                )
            calendar = admitted_authorities.get("trading_calendar")
            if calendar is None:
                raise ProviderMaterializationError(
                    "signed component admission requires trading_calendar authority"
                )
        companion = build_companion_snapshot(
            coverage_start=min(day for _, day in panel),
            coverage_end=max(day for _, day in panel),
            checkout=checkout,
            database=database,
            source_receipts=source_receipts,
            execution_adjustment_identity=execution_adjustment,
            signal_adjustment_identity=signal_adjustment,
            exact_panel=exact_panel,
            components=components,
        )
        report = _blocked_report(
            full_report=full_report,
            database=database,
            execution_adjustment=execution_adjustment,
            signal_adjustment=signal_adjustment,
            panel=exact_panel,
            panel_size=len(panel),
            companion_sha256=companion.snapshot_sha256,
            admitted_authorities=admitted_authorities,
        )
        report_raw = _canonical(report)
        readiness = _reference("stock-data-readiness-report", READINESS_REPORT_SCHEMA, report_raw)
        readiness_path = _write_artifact(destination, report_raw)
        bundle = {
            "schema_version": EXPORT_SCHEMA,
            "coverage_start": min(day for _, day in panel),
            "coverage_end": max(day for _, day in panel),
            "checkout": _locator(checkout, paths[checkout]),
            "database": _locator(database, paths[database]),
            "source_receipts": [_locator(reference, paths[reference]) for reference in source_receipts],
            "execution_adjustment_identity": _locator(execution_adjustment, paths[execution_adjustment]),
            "signal_adjustment_identity": _locator(signal_adjustment, paths[signal_adjustment]),
            "exact_panel": _locator(exact_panel, paths[exact_panel]),
            "components": {component: _locator(components[component], paths[components[component]]) for component in REQUIRED_COMPONENTS},
            "readiness_report": _locator(readiness, readiness_path),
        }
        bundle_file = destination / "bundle.json"
        bundle_file.write_bytes(_canonical(bundle))
        (destination / "companion_snapshot.json").write_bytes(_canonical(companion.to_dict()))
        receipt = export_verified_provider_receipt(bundle_file)
        if receipt["ready"] is not False:
            raise AssertionError("materializer must remain fail-closed without authority attestation")
        return {"bundle_file": str(bundle_file), "receipt": receipt}
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
