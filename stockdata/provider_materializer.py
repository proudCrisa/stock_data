"""Materialize a content-addressed, fail-closed RQGM provider bundle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
from typing import cast

from stockdata.adjustment_identity import (
    EXECUTION_ADJUSTMENT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
    verify_adjustment_identity,
)
from stockdata.companion_snapshot import build_companion_snapshot
from stockdata.collector_continuity import (
    CLOSURE_SCHEMA,
    CONTINUITY_CLOSURE_REFERENCE_KIND,
    CollectorContinuityError,
    create_registered_collector_materialization_snapshot,
    default_collector_ledger_path,
    open_nofollow_regular,
    probe_database_collector_genesis_strict,
    verify_file_identity,
)
from stockdata.component_availability import verify_component_availability_records
from stockdata.execution_readiness import load_panel
from stockdata.full_execution_readiness import check_full_execution_readiness
from stockdata.authority import load_provider_trust_registry
from stockdata.provider_authority_admission import (
    AdmittedProviderAuthority,
    SIGNED_COMPONENTS,
    admit_signed_component_authority,
)
from stockdata.provider_export import (
    _export_verified_provider_receipt,
    BUNDLE_SCHEMA,
    EXPORT_SCHEMA,
    LEDGER_REFERENCE_KIND,
    LEDGER_SNAPSHOT_SCHEMA,
    REGISTRATION_REFERENCE_KIND,
    REGISTRATION_SCHEMA,
    export_verified_provider_receipt,  # noqa: F401 - retained compatibility module attribute
)
from stockdata.provider_intrinsic import (
    INTRINSIC_COMPONENTS,
    IntrinsicEvidenceError,
    reconstruct_intrinsic_evidence,
    verify_intrinsic_evidence,
)
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


@dataclass(frozen=True)
class _SnapshotArtifact:
    reference: ProviderArtifactReference
    path: Path
    raw: bytes
    identity: object


@dataclass(frozen=True)
class _CollectorSnapshot:
    staging_directory: Path
    database: _SnapshotArtifact
    registration: _SnapshotArtifact
    ledger: _SnapshotArtifact
    continuity_closure: _SnapshotArtifact


def _require_collector_materialization_candidate(database_file: str | Path) -> None:
    database_path = os.path.abspath(os.path.expanduser(os.fspath(database_file)))
    ledger_path = default_collector_ledger_path(database_path)
    try:
        os.lstat(ledger_path)
    except FileNotFoundError:
        try:
            has_genesis = probe_database_collector_genesis_strict(database_path)
        except CollectorContinuityError as exc:
            raise ProviderMaterializationError(str(exc)) from exc
        if has_genesis:
            raise ProviderMaterializationError(
                "collector materialization requires its continuity ledger"
            )
        raise ProviderMaterializationError(
            "collector materialization requires a registered collector genesis"
        )
    except OSError as exc:
        raise ProviderMaterializationError(
            "collector continuity ledger cannot be inspected"
        ) from exc


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


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ProviderMaterializationError(f"{field} must be a lowercase SHA-256")
    return value


def _write_all(descriptor: int, raw: bytes, field: str) -> None:
    offset = 0
    while offset < len(raw):
        try:
            written = os.write(descriptor, raw[offset:])
        except OSError as exc:
            raise ProviderMaterializationError(f"{field} cannot be written") from exc
        if written <= 0:
            raise ProviderMaterializationError(f"{field} cannot be written")
        offset += written


def _fsync_directory(path: Path, field: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as exc:
        raise ProviderMaterializationError(f"{field} cannot be fsynced") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_exclusive(path: Path, raw: bytes, field: str) -> Path:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        _write_all(descriptor, raw, field)
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise ProviderMaterializationError(f"{field} collides with an existing file") from exc
    except OSError as exc:
        raise ProviderMaterializationError(f"{field} cannot be created durably") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent, f"{field} directory")
    return path


def _write_artifact(directory: Path, raw: bytes) -> Path:
    target = directory / "artifacts" / hashlib.sha256(raw).hexdigest()
    return _write_exclusive(target, raw, "provider artifact")


def _locator(reference: ProviderArtifactReference, path: Path) -> dict[str, object]:
    return {"reference": reference.to_dict(), "path": str(path)}


def _snapshot_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ProviderMaterializationError(f"{field} path is invalid")
    canonical = os.path.abspath(os.path.expanduser(value))
    if value != canonical:
        raise ProviderMaterializationError(f"{field} path must be canonical")
    return Path(canonical)


def _snapshot_reference(
    value: object,
    field: str,
    *,
    kind: str,
    schema: str,
) -> ProviderArtifactReference:
    try:
        reference = ProviderArtifactReference.from_dict(value)
        reference.validate()
    except ValueError as exc:
        raise ProviderMaterializationError(f"{field} reference is invalid") from exc
    if reference.kind != kind or reference.schema_version != schema:
        raise ProviderMaterializationError(f"{field} reference has wrong kind or schema")
    return reference


def _read_snapshot_artifact(
    path: Path,
    field: str,
    *,
    expected_sha256: str,
) -> tuple[bytes, object]:
    try:
        opened = open_nofollow_regular(path)
    except CollectorContinuityError as exc:
        raise ProviderMaterializationError(
            f"{field} must be a canonical no-follow regular file"
        ) from exc
    try:
        status = os.fstat(opened.descriptor)
        raw = bytearray()
        offset = 0
        while offset < status.st_size:
            chunk = os.pread(
                opened.descriptor,
                min(1024 * 1024, status.st_size - offset),
                offset,
            )
            if not chunk:
                raise ProviderMaterializationError(f"{field} was truncated")
            raw.extend(chunk)
            offset += len(chunk)
        current = os.fstat(opened.descriptor)
        if (
            current.st_dev != status.st_dev
            or current.st_ino != status.st_ino
            or current.st_size != status.st_size
        ):
            raise ProviderMaterializationError(f"{field} identity drifted")
        verify_file_identity(path, opened.identity)
        content = bytes(raw)
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ProviderMaterializationError(f"{field} content hash drifted")
        return content, opened.identity
    except CollectorContinuityError as exc:
        raise ProviderMaterializationError(f"{field} identity drifted") from exc
    finally:
        opened.close()


def _snapshot_physical_identity(identity: object, field: str) -> tuple[int, int]:
    if (
        isinstance(identity, tuple)
        and len(identity) == 2
        and type(identity[0]) is int
        and type(identity[1]) is int
    ):
        return identity
    device = getattr(identity, "file_st_dev", None)
    inode = getattr(identity, "file_st_ino", None)
    if type(device) is not int or type(inode) is not int:
        raise ProviderMaterializationError(f"{field} physical identity is invalid")
    return device, inode


def _validate_collector_snapshot(
    value: object,
    *,
    staging_parent: str | Path,
) -> _CollectorSnapshot:
    expected_top = {
        "staging_directory",
        "database",
        "registration",
        "ledger",
        "continuity_closure",
    }
    if not isinstance(value, Mapping) or set(value) != expected_top:
        raise ProviderMaterializationError("collector snapshot result fields are not exact")
    staging = _snapshot_path(value["staging_directory"], "staging_directory")
    parent = Path(os.path.abspath(os.path.expanduser(os.fspath(staging_parent))))
    leaf = staging.name
    if (
        staging.parent != parent
        or not leaf.startswith(".collector-snapshot-")
        or len(leaf.removeprefix(".collector-snapshot-")) != 64
        or any(
            character not in "0123456789abcdef"
            for character in leaf.removeprefix(".collector-snapshot-")
        )
    ):
        raise ProviderMaterializationError("collector snapshot staging directory is invalid")
    try:
        status = os.lstat(staging)
    except OSError as exc:
        raise ProviderMaterializationError("collector snapshot staging is unavailable") from exc
    if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o700:
        raise ProviderMaterializationError("collector snapshot staging is not private")

    database_value = value["database"]
    registration_value = value["registration"]
    ledger_value = value["ledger"]
    closure_value = value["continuity_closure"]
    if (
        not isinstance(database_value, Mapping)
        or set(database_value) != {"path", "reference"}
        or not isinstance(registration_value, Mapping)
        or set(registration_value) != {"path", "sha256"}
        or not isinstance(ledger_value, Mapping)
        or set(ledger_value) != {"path", "sha256"}
        or not isinstance(closure_value, Mapping)
        or set(closure_value) != {"path", "reference"}
    ):
        raise ProviderMaterializationError("collector snapshot artifact fields are not exact")

    database_reference = _snapshot_reference(
        database_value["reference"],
        "collector snapshot database",
        kind="stock-data-database",
        schema=DATABASE_SCHEMA,
    )
    registration_sha256 = _sha256(
        registration_value["sha256"], "collector snapshot registration"
    )
    ledger_sha256 = _sha256(ledger_value["sha256"], "collector snapshot ledger")
    closure_reference = _snapshot_reference(
        closure_value["reference"],
        "collector continuity closure",
        kind=CONTINUITY_CLOSURE_REFERENCE_KIND,
        schema=CLOSURE_SCHEMA,
    )
    artifacts = (
        (
            "database",
            _snapshot_path(database_value["path"], "collector snapshot database"),
            database_reference,
        ),
        (
            "registration",
            _snapshot_path(
                registration_value["path"], "collector snapshot registration"
            ),
            ProviderArtifactReference(
                REGISTRATION_REFERENCE_KIND,
                registration_sha256,
                REGISTRATION_SCHEMA,
            ),
        ),
        (
            "ledger",
            _snapshot_path(ledger_value["path"], "collector snapshot ledger"),
            ProviderArtifactReference(
                LEDGER_REFERENCE_KIND,
                ledger_sha256,
                LEDGER_SNAPSHOT_SCHEMA,
            ),
        ),
        (
            "continuity_closure",
            _snapshot_path(
                closure_value["path"], "collector continuity closure"
            ),
            closure_reference,
        ),
    )
    if len({path for _, path, _ in artifacts}) != len(artifacts) or len(
        {reference for _, _, reference in artifacts}
    ) != len(artifacts):
        raise ProviderMaterializationError("collector snapshot artifacts alias each other")

    validated: dict[str, _SnapshotArtifact] = {}
    physical_identities: set[tuple[int, int]] = set()
    for name, path, reference in artifacts:
        if path.parent != staging or path.name != reference.identifier:
            raise ProviderMaterializationError(
                f"collector snapshot {name} is not content addressed"
            )
        raw, identity = _read_snapshot_artifact(
            path,
            f"collector snapshot {name}",
            expected_sha256=reference.identifier,
        )
        physical_identity = _snapshot_physical_identity(
            identity, f"collector snapshot {name}"
        )
        if physical_identity in physical_identities:
            raise ProviderMaterializationError(
                "collector snapshot artifacts physically alias each other"
            )
        physical_identities.add(physical_identity)
        validated[name] = _SnapshotArtifact(reference, path, raw, identity)

    try:
        registration_object = json.loads(validated["registration"].raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderMaterializationError("collector registration snapshot is invalid") from exc
    if (
        not isinstance(registration_object, Mapping)
        or registration_object.get("schema_version") != REGISTRATION_SCHEMA
        or _canonical(registration_object) != validated["registration"].raw
    ):
        raise ProviderMaterializationError(
            "collector registration snapshot must be canonical registration /4"
        )
    try:
        closure_object = json.loads(
            validated["continuity_closure"].raw.decode("ascii")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderMaterializationError("collector continuity closure is invalid") from exc
    if (
        not isinstance(closure_object, Mapping)
        or closure_object.get("schema_version") != CLOSURE_SCHEMA
        or _canonical(closure_object) != validated["continuity_closure"].raw
    ):
        raise ProviderMaterializationError(
            "collector continuity closure must be canonical"
        )
    return _CollectorSnapshot(
        staging,
        validated["database"],
        validated["registration"],
        validated["ledger"],
        validated["continuity_closure"],
    )


def _reverify_collector_snapshot(snapshot: _CollectorSnapshot) -> None:
    for name in ("database", "registration", "ledger", "continuity_closure"):
        artifact = getattr(snapshot, name)
        raw, identity = _read_snapshot_artifact(
            artifact.path,
            f"collector snapshot {name}",
            expected_sha256=artifact.reference.identifier,
        )
        if identity != artifact.identity or raw != artifact.raw:
            raise ProviderMaterializationError(
                f"collector snapshot {name} identity drifted"
            )


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


def _readiness_report(
    *,
    full_report: Mapping[str, object],
    database: ProviderArtifactReference,
    execution_adjustment: ProviderArtifactReference,
    signal_adjustment: ProviderArtifactReference,
    panel: ProviderArtifactReference,
    panel_size: int,
    companion_sha256: str,
    admitted_authorities: Mapping[str, AdmittedProviderAuthority] | None = None,
    intrinsic_evidence: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    reported_components = full_report.get("components")
    if not isinstance(reported_components, Mapping):
        raise ProviderMaterializationError("full readiness component report is malformed")

    components: dict[str, dict[str, object]] = {}
    blockers: list[dict[str, object]] = []
    admitted_authorities = admitted_authorities or {}
    intrinsic_evidence = intrinsic_evidence or {}
    for component in REQUIRED_COMPONENTS:
        admitted = admitted_authorities.get(component)
        if admitted is not None:
            evidence = admitted.readiness_evidence()
            components[component] = evidence
            continue
        intrinsic = intrinsic_evidence.get(component)
        if intrinsic is not None:
            evidence = dict(intrinsic)
            components[component] = evidence
            blockers.extend(
                {**item, "component": component}
                for item in cast(Sequence[object], evidence.get("blockers", []))
                if isinstance(item, Mapping)
            )
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

    ready = not blockers and all(
        evidence.get("ready") is True for evidence in components.values()
    )
    return {
        "schema_version": READINESS_REPORT_SCHEMA,
        "ready": ready,
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
    registration_file: str | Path,
    snapshot_staging_directory: str | Path,
    panel_file: str | Path,
    source_receipt_files: Sequence[str | Path],
    execution_adjustment_file: str | Path,
    signal_adjustment_file: str | Path,
    component_files: Mapping[str, str | Path],
    component_authority_files: Mapping[str, str | Path] | None = None,
    source: str,
) -> dict[str, object]:
    """Create one immutable bundle from reconstructed and admitted evidence.

    This function does not fetch, repair, or self-attest data. Provider-owned
    components are reconstructed from the database; external facts require enrolled
    authority. Missing evidence remains a valid blocked bundle.
    """

    requested_destination = Path(output_dir).expanduser()
    if os.path.lexists(requested_destination):
        raise ProviderMaterializationError("output_dir must not already exist")
    destination = requested_destination.resolve()
    if os.path.lexists(destination):
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
    for value, field in (
        (database_file, "database_file"),
        (registration_file, "registration_file"),
        (snapshot_staging_directory, "snapshot_staging_directory"),
    ):
        try:
            valid = bool(os.fspath(value))
        except TypeError as exc:
            raise ProviderMaterializationError(f"{field} is invalid") from exc
        if not valid:
            raise ProviderMaterializationError(f"{field} is invalid")

    _require_collector_materialization_candidate(database_file)
    try:
        snapshot_result = create_registered_collector_materialization_snapshot(
            registration_file,
            database=database_file,
            staging_directory=snapshot_staging_directory,
        )
    except CollectorContinuityError as exc:
        raise ProviderMaterializationError(str(exc)) from exc
    snapshot = _validate_collector_snapshot(
        snapshot_result,
        staging_parent=snapshot_staging_directory,
    )
    snapshot_database = str(snapshot.database.path)

    panel_entries, panel = _canonical_panel(panel_file)
    panel_raw = _canonical(panel_entries)
    database_raw = snapshot.database.raw
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
    if execution_verified.source != source:
        raise ProviderMaterializationError(
            "execution adjustment identity must use the declared price source"
        )
    execution_raw = _canonical(execution_value)
    signal_raw = _canonical(signal_value)
    if hashlib.sha256(execution_raw).hexdigest() != execution_verified.identifier:
        raise AssertionError("execution adjustment canonical identity drifted")
    if hashlib.sha256(signal_raw).hexdigest() != signal_verified.identifier:
        raise AssertionError("signal adjustment canonical identity drifted")

    receipt_raws = [_read_regular(path, f"source_receipt_files[{index}]") for index, path in enumerate(source_receipt_files)]
    source_receipt_inputs = []
    for index, raw in enumerate(receipt_raws):
        _source_receipt(raw, f"source_receipt_files[{index}]")
        value = _json(raw, f"source_receipt_files[{index}]")
        if _canonical(value) != raw:
            raise ProviderMaterializationError(
                f"source_receipt_files[{index}] must be canonical JSON"
            )
        source_receipt_inputs.append(value)
    if len(receipt_raws) != len(set(receipt_raws)):
        raise ProviderMaterializationError("source_receipt_files must not repeat an artifact")
    ordered_receipts = sorted(
        zip(receipt_raws, source_receipt_inputs),
        key=lambda item: hashlib.sha256(item[0]).hexdigest(),
    )
    receipt_raws = [raw for raw, _ in ordered_receipts]
    source_receipt_inputs = [value for _, value in ordered_receipts]
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

    output_created = False
    committed = False
    try:
        try:
            destination.mkdir(mode=0o700)
            output_created = True
            (destination / "artifacts").mkdir(mode=0o700)
        except OSError as exc:
            raise ProviderMaterializationError(
                "output_dir cannot be created exclusively"
            ) from exc
        _fsync_directory(destination / "artifacts", "provider artifact directory")
        _fsync_directory(destination, "provider output directory")
        checkout_raw = _canonical({"schema_version": CHECKOUT_SCHEMA, "repository": "stock_data"})
        checkout = _reference("stock-data-checkout", CHECKOUT_SCHEMA, checkout_raw)
        database = snapshot.database.reference
        if database != _reference("stock-data-database", DATABASE_SCHEMA, database_raw):
            raise ProviderMaterializationError(
                "collector snapshot database reference drifted"
            )
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
            reference.identifier: value
            for reference, value in zip(source_receipts, source_receipt_inputs)
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
            database: snapshot.database.path,
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
            snapshot_database,
            source=source,
            adjustment_mode=execution_verified.adjustment_mode,
            adjustment_version=execution_verified.adjustment_version,
            signal_adjustment_mode=signal_verified.adjustment_mode,
            signal_adjustment_version=signal_verified.adjustment_version,
            signal_source=signal_verified.source,
            panel=panel,
        )
        admitted_authorities: dict[str, AdmittedProviderAuthority] = {}
        if "trading_calendar" in authority_files:
            registry = load_provider_trust_registry()
            ordered_components = [
                component
                for component in (
                    "trading_calendar",
                    "instrument_status",
                    "market_rules",
                    "universe",
                    "corporate_actions",
                )
                if component in authority_files
            ]
            for component in ordered_components:
                if (
                    component == "market_rules"
                    and "instrument_status" not in admitted_authorities
                ):
                    continue
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
                    instrument_status_authority=(
                        admitted_authorities.get("instrument_status")
                        if component == "market_rules"
                        else None
                    ),
                )
            calendar = admitted_authorities.get("trading_calendar")
            if calendar is None:
                raise ProviderMaterializationError(
                    "signed component admission requires trading_calendar authority"
                )
        calendar = admitted_authorities.get("trading_calendar")
        if calendar is None:
            intrinsic_evidence = {
                component: {
                    "ready": False,
                    "blockers": [
                        {
                            "code": "signed_calendar_required_for_intrinsic_reconstruction",
                            "count": 1,
                        }
                    ],
                }
                for component in INTRINSIC_COMPONENTS
            }
        else:
            try:
                reconstructed = reconstruct_intrinsic_evidence(
                    snapshot_database,
                    panel=panel_entries,
                    execution_adjustment=execution_verified,
                    signal_adjustment=signal_verified,
                    decision_cutoffs=calendar.decision_cutoff_by_panel,
                )
                intrinsic_evidence = verify_intrinsic_evidence(
                    reconstructed,
                    claimed_components={
                        component: component_values[component]
                        for component in INTRINSIC_COMPONENTS
                    },
                    component_references={
                        component: components[component]
                        for component in INTRINSIC_COMPONENTS
                    },
                    bound_source_receipts=source_receipt_values,
                    database_sha256=database.identifier,
                )
            except IntrinsicEvidenceError as exc:
                intrinsic_evidence = {
                    component: {
                        "ready": False,
                        "blockers": [{"code": exc.code, "count": 1}],
                    }
                    for component in INTRINSIC_COMPONENTS
                }
        upstream_ready = all(
            (
                intrinsic_evidence[component].get("ready") is True
                if component in INTRINSIC_COMPONENTS
                else component in admitted_authorities
            )
            for component in REQUIRED_COMPONENTS
            if component != "availability_records"
        )
        if not upstream_ready or calendar is None:
            availability_evidence = {
                "ready": False,
                "blockers": [
                    {
                        "code": "availability_depends_on_unready_component",
                        "count": 1,
                    }
                ],
            }
        else:
            try:
                verified_availability = verify_component_availability_records(
                    component_values["availability_records"],
                    expected_panel_sha256=exact_panel.identifier,
                    expected_panel_size=len(panel),
                    expected_decision_cutoffs=calendar.decision_cutoff_by_panel,
                    bound_source_receipt_ids=sorted(source_receipt_values),
                    component_records={
                        component: cast(
                            Mapping[str, Sequence[Mapping[str, object]]],
                            component_values[component],
                        )["records"]
                        for component in REQUIRED_COMPONENTS
                        if component != "availability_records"
                    },
                    expected_signed_calendar_phases=(
                        calendar.signed_calendar_phases_by_panel
                    ),
                )
                availability_payload = {
                    "verifier_schema": "stockdata-provider-availability-verifier/1",
                    "artifact": components["availability_records"].to_dict(),
                    "source_receipt_ids": list(
                        verified_availability.source_receipt_ids
                    ),
                    "coverage_count": verified_availability.record_count,
                    "panel_sha256": verified_availability.panel_sha256,
                }
                availability_evidence = {
                    "ready": verified_availability.ready,
                    "blockers": [
                        {"code": code, "count": 1}
                        for code in verified_availability.blockers
                    ],
                    **availability_payload,
                    "evidence_sha256": hashlib.sha256(
                        _canonical(availability_payload)
                    ).hexdigest(),
                }
            except (KeyError, TypeError, ValueError):
                availability_evidence = {
                    "ready": False,
                    "blockers": [
                        {"code": "availability_verification_failed", "count": 1}
                    ],
                }
        intrinsic_evidence = {
            **intrinsic_evidence,
            "availability_records": availability_evidence,
        }
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
        report = _readiness_report(
            full_report=full_report,
            database=database,
            execution_adjustment=execution_adjustment,
            signal_adjustment=signal_adjustment,
            panel=exact_panel,
            panel_size=len(panel),
            companion_sha256=companion.snapshot_sha256,
            admitted_authorities=admitted_authorities,
            intrinsic_evidence=intrinsic_evidence,
        )
        report_raw = _canonical(report)
        readiness = _reference("stock-data-readiness-report", READINESS_REPORT_SCHEMA, report_raw)
        readiness_path = _write_artifact(destination, report_raw)
        _reverify_collector_snapshot(snapshot)
        bundle = {
            "schema_version": BUNDLE_SCHEMA,
            "coverage_start": min(day for _, day in panel),
            "coverage_end": max(day for _, day in panel),
            "checkout": _locator(checkout, paths[checkout]),
            "database": _locator(database, paths[database]),
            "registration": _locator(
                snapshot.registration.reference, snapshot.registration.path
            ),
            "ledger_snapshot": _locator(
                snapshot.ledger.reference, snapshot.ledger.path
            ),
            "continuity_closure": _locator(
                snapshot.continuity_closure.reference,
                snapshot.continuity_closure.path,
            ),
            "source_receipts": [_locator(reference, paths[reference]) for reference in source_receipts],
            "execution_adjustment_identity": _locator(execution_adjustment, paths[execution_adjustment]),
            "signal_adjustment_identity": _locator(signal_adjustment, paths[signal_adjustment]),
            "exact_panel": _locator(exact_panel, paths[exact_panel]),
            "components": {component: _locator(components[component], paths[components[component]]) for component in REQUIRED_COMPONENTS},
            "readiness_report": _locator(readiness, readiness_path),
        }
        bundle_file = destination / "bundle.json"
        _write_exclusive(
            destination / "companion_snapshot.json",
            _canonical(companion.to_dict()),
            "companion snapshot",
        )
        temporary_bundle = destination / f".bundle-{secrets.token_hex(16)}.json"
        bundle_raw = _canonical(bundle)
        _write_exclusive(temporary_bundle, bundle_raw, "provider bundle temporary")
        receipt = _export_verified_provider_receipt(temporary_bundle)
        if receipt.get("schema_version") != EXPORT_SCHEMA:
            raise AssertionError("provider export envelope schema drifted")
        if receipt["ready"] is not report["ready"]:
            raise AssertionError(
                "materialized readiness differs from independent export verification"
            )
        try:
            os.replace(temporary_bundle, bundle_file)
        except OSError as exc:
            raise ProviderMaterializationError(
                "provider bundle cannot be published atomically"
            ) from exc
        _fsync_directory(destination, "provider output directory")
        _fsync_directory(destination.parent, "provider output parent")
        committed = True
        return {"bundle_file": str(bundle_file), "receipt": receipt}
    except Exception:
        if output_created and not committed:
            shutil.rmtree(destination, ignore_errors=True)
        raise
