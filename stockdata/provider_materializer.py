"""Materialize a content-addressed, fail-closed RQGM provider bundle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os as _stdlib_os
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
    verify_registered_collector_materialization_complete,
)
from stockdata.component_availability import verify_component_availability_records
from stockdata.execution_readiness import load_panel
from stockdata.full_execution_readiness import check_full_execution_readiness
from stockdata.authority import load_provider_trust_registry
from stockdata.provider_authority_admission import (
    AdmittedProviderAuthority,
    SIGNED_COMPONENTS,
    admit_signed_component_authority,
    validate_local_mechanical_prerequisites,
)
from stockdata.provider_export import (
    _export_verified_provider_receipt,
    BUNDLE_SCHEMA,
    EXPORT_SCHEMA,
    LEDGER_REFERENCE_KIND,
    LEDGER_SNAPSHOT_SCHEMA,
    REGISTRATION_REFERENCE_KIND,
    REGISTRATION_SCHEMAS,
    TRUSTED_LOCAL_READINESS_BLOCKER,
    TRUSTED_LOCAL_REGISTRATION_SCHEMA,
    export_verified_provider_receipt,  # noqa: F401 - retained compatibility module attribute
)
from stockdata.provider_intrinsic import (
    FORWARD_COMPONENTS,
    INTRINSIC_COMPONENTS,
    IntrinsicEvidenceError,
    reconstruct_forward_component_evidence,
    reconstruct_intrinsic_evidence,
    verify_forward_component_evidence,
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


class _ProviderOSFacade:
    """Keep materializer filesystem fault injection scoped to this module."""

    def __getattr__(self, name: str) -> object:
        return getattr(_stdlib_os, name)


os = _ProviderOSFacade()


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
    registration_path = _snapshot_path(
        registration_value["path"], "collector snapshot registration"
    )
    try:
        registration_object = json.loads(
            _read_snapshot_artifact(
                registration_path,
                "collector snapshot registration",
                expected_sha256=registration_sha256,
            )[0].decode("ascii")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderMaterializationError("collector registration snapshot is invalid") from exc
    registration_schema = (
        registration_object.get("schema_version")
        if isinstance(registration_object, Mapping)
        else None
    )
    if registration_schema not in REGISTRATION_SCHEMAS:
        raise ProviderMaterializationError("collector registration snapshot has unsupported schema")
    artifacts = (
        (
            "database",
            _snapshot_path(database_value["path"], "collector snapshot database"),
            database_reference,
        ),
        (
            "registration",
            registration_path,
            ProviderArtifactReference(
                REGISTRATION_REFERENCE_KIND,
                registration_sha256,
                registration_schema,
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

    if (
        not isinstance(registration_object, Mapping)
        or _canonical(registration_object) != validated["registration"].raw
    ):
        raise ProviderMaterializationError(
            "collector registration snapshot must be canonical"
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
    trusted_local_mechanical: bool = False,
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

    if trusted_local_mechanical:
        blockers = []
        for component, evidence in tuple(components.items()):
            original_blockers = evidence.get("blockers")
            if not isinstance(original_blockers, list) or any(
                not isinstance(item, Mapping) for item in original_blockers
            ):
                raise ProviderMaterializationError(
                    f"trusted-local readiness blockers for {component} are malformed"
                )
            component_blockers = [dict(item) for item in original_blockers]
            component_blockers.append(
                {
                    "code": TRUSTED_LOCAL_READINESS_BLOCKER,
                    "count": 1,
                }
            )
            components[component] = {
                "ready": False,
                "blockers": component_blockers,
            }
            blockers.extend(
                {**item, "component": component} for item in component_blockers
            )

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


def _require_trusted_local_materialization_inputs(
    *,
    registration_raw: bytes,
    source_receipts: Sequence[ProviderArtifactReference],
    components: Mapping[str, ProviderArtifactReference],
) -> None:
    """Keep `/5` bundle inputs attached to the registered local prerequisites."""

    registration = _json(registration_raw, "trusted-local registration")
    if not isinstance(registration, Mapping):
        raise ProviderMaterializationError("trusted-local registration is invalid")
    prerequisites = registration.get("prerequisites")
    if not isinstance(prerequisites, Mapping):
        raise ProviderMaterializationError(
            "trusted-local registration prerequisites are invalid"
        )
    receipt_ids = prerequisites.get("source_receipt_ids")
    calendar = prerequisites.get("trading_calendar")
    rules = prerequisites.get("market_rule_prerequisite")
    if (
        not isinstance(receipt_ids, list)
        or receipt_ids != sorted(receipt_ids)
        or len(receipt_ids) != len(set(receipt_ids))
        or any(
            _sha256(receipt_id, "trusted-local source receipt id") != receipt_id
            for receipt_id in receipt_ids
        )
        or not isinstance(calendar, Mapping)
        or not isinstance(rules, Mapping)
    ):
        raise ProviderMaterializationError(
            "trusted-local registration prerequisites are invalid"
        )
    if sorted(reference.identifier for reference in source_receipts) != receipt_ids:
        raise ProviderMaterializationError(
            "trusted-local materialization source receipts differ from registration"
        )
    calendar_sha256 = calendar.get("artifact_sha256")
    if (
        _sha256(calendar_sha256, "trusted-local trading_calendar artifact_sha256")
        != components["trading_calendar"].identifier
    ):
        raise ProviderMaterializationError(
            "trusted-local materialization trading_calendar differs from registration"
        )
    if not isinstance(rules.get("artifact_sha256"), str):
        raise ProviderMaterializationError(
            "trusted-local market_rules prerequisite is invalid"
        )


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
    _collector_snapshot: _CollectorSnapshot | None = None,
    _embed_collector_snapshot: bool = False,
    _published_output_dir: str | Path | None = None,
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

    if _collector_snapshot is None:
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
    else:
        snapshot = _collector_snapshot
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
        trusted_local_mechanical = (
            snapshot.registration.reference.schema_version
            == TRUSTED_LOCAL_REGISTRATION_SCHEMA
        )
        if trusted_local_mechanical:
            _require_trusted_local_materialization_inputs(
                registration_raw=snapshot.registration.raw,
                source_receipts=source_receipts,
                components=components,
            )

        paths = {
            checkout: _write_artifact(destination, checkout_raw),
            execution_adjustment: _write_artifact(destination, execution_raw),
            signal_adjustment: _write_artifact(destination, signal_raw),
            exact_panel: _write_artifact(destination, panel_raw),
        }
        if _embed_collector_snapshot:
            paths.update(
                {
                    database: _write_artifact(destination, snapshot.database.raw),
                    snapshot.registration.reference: _write_artifact(
                        destination, snapshot.registration.raw
                    ),
                    snapshot.ledger.reference: _write_artifact(
                        destination, snapshot.ledger.raw
                    ),
                    snapshot.continuity_closure.reference: _write_artifact(
                        destination, snapshot.continuity_closure.raw
                    ),
                }
            )
        else:
            paths[database] = snapshot.database.path
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
            trusted_local_mechanical=trusted_local_mechanical,
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
                snapshot.registration.reference,
                paths.get(snapshot.registration.reference, snapshot.registration.path),
            ),
            "ledger_snapshot": _locator(
                snapshot.ledger.reference,
                paths.get(snapshot.ledger.reference, snapshot.ledger.path),
            ),
            "continuity_closure": _locator(
                snapshot.continuity_closure.reference,
                paths.get(
                    snapshot.continuity_closure.reference,
                    snapshot.continuity_closure.path,
                ),
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
        if _published_output_dir is not None:
            published_root = Path(_published_output_dir).expanduser().resolve()
            projected = json.loads(bundle_raw.decode("ascii"))

            def project(locator: object) -> None:
                if not isinstance(locator, dict):
                    raise AssertionError("provider locator projection is invalid")
                path = Path(str(locator["path"]))
                locator["path"] = str(published_root / path.relative_to(destination))

            for field in (
                "checkout",
                "database",
                "registration",
                "ledger_snapshot",
                "continuity_closure",
                "execution_adjustment_identity",
                "signal_adjustment_identity",
                "exact_panel",
                "readiness_report",
            ):
                project(projected[field])
            for locator in projected["source_receipts"]:
                project(locator)
            for locator in projected["components"].values():
                project(locator)
            published_raw = _canonical(projected)
            temporary_published = destination / f".bundle-{secrets.token_hex(16)}.json"
            _write_exclusive(
                temporary_published,
                published_raw,
                "provider bundle publication temporary",
            )
            temporary_bundle.unlink()
            temporary_bundle = temporary_published
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


@dataclass
class _RetainedInput:
    path: Path
    opened: object
    raw: bytes
    field: str


def _retain_input(path: str | Path, field: str) -> _RetainedInput:
    try:
        canonical = Path(
            os.path.abspath(os.path.expanduser(os.fspath(path)))
        )
        opened = open_nofollow_regular(canonical)
    except (CollectorContinuityError, OSError, TypeError, ValueError) as exc:
        raise ProviderMaterializationError(
            f"{field} must name a canonical no-follow regular file"
        ) from exc
    try:
        status = os.fstat(opened.descriptor)
        raw = bytearray()
        offset = 0
        while offset < status.st_size:
            chunk = os.pread(
                opened.descriptor, min(1024 * 1024, status.st_size - offset), offset
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
        verify_file_identity(canonical, opened.identity)
        return _RetainedInput(canonical, opened, bytes(raw), field)
    except BaseException:
        opened.close()
        raise


def _reverify_retained_input(value: _RetainedInput) -> None:
    try:
        status = os.fstat(value.opened.descriptor)
        raw = os.pread(value.opened.descriptor, status.st_size, 0)
        if len(raw) != status.st_size or raw != value.raw:
            raise ProviderMaterializationError(f"{value.field} content drifted")
    except CollectorContinuityError as exc:
        raise ProviderMaterializationError(
            f"{value.field} identity drifted"
        ) from exc


def _require_complete_sqlite_bytes(raw: bytes) -> None:
    if len(raw) < 100 or raw[:16] != b"SQLite format 3\x00":
        raise ProviderMaterializationError("collector database is invalid")
    page_size = int.from_bytes(raw[16:18], "big")
    if page_size == 1:
        page_size = 65536
    page_count = int.from_bytes(raw[28:32], "big")
    if page_size < 512 or page_size & (page_size - 1) or not page_count:
        raise ProviderMaterializationError("collector database extent is invalid")
    if len(raw) != page_size * page_count:
        raise ProviderMaterializationError("collector database physical bytes drifted")


def _new_private_materialization_directory(output_dir: Path) -> Path:
    if os.path.lexists(output_dir):
        raise ProviderMaterializationError("output_dir must not already exist")
    parent = output_dir.parent
    try:
        status = os.lstat(parent)
    except OSError as exc:
        raise ProviderMaterializationError("output_dir parent is unavailable") from exc
    if not stat.S_ISDIR(status.st_mode):
        raise ProviderMaterializationError("output_dir parent must be a directory")
    for _ in range(8):
        private = parent / f".registered-provider-materialize-{secrets.token_hex(16)}"
        try:
            private.mkdir(mode=0o700)
            _fsync_directory(parent, "registered materializer parent")
            return private
        except FileExistsError:
            continue
        except OSError as exc:
            raise ProviderMaterializationError(
                "private materialization staging cannot be created"
            ) from exc
    raise ProviderMaterializationError("private materialization staging collides")


def _remove_private_materialization_directory(path: Path) -> None:
    for _ in range(2):
        if not os.path.lexists(path):
            return
        try:
            shutil.rmtree(path)
        except OSError:
            continue
    if os.path.lexists(path):
        raise ProviderMaterializationError("private materialization cleanup failed")


def _trusted_local_registration_inputs(
    registration: _RetainedInput,
) -> tuple[
    Mapping[str, object],
    tuple[str, ...],
    tuple[_RetainedInput, ...],
    _RetainedInput,
    _RetainedInput,
]:
    value = _json(registration.raw, "trusted-local registration")
    if (
        not isinstance(value, Mapping)
        or _canonical(value) != registration.raw
        or value.get("schema_version") != TRUSTED_LOCAL_REGISTRATION_SCHEMA
        or value.get("authority_mode") != "trusted_local_mechanical"
    ):
        raise ProviderMaterializationError("registration must be trusted-local /5")
    files = value.get("prerequisite_files")
    if (
        not isinstance(files, Mapping)
        or set(files) != {"source_receipts", "trading_calendar", "market_rules"}
        or not isinstance(files["source_receipts"], list)
        or len(files["source_receipts"]) != 2
        or any(not isinstance(path, str) for path in files["source_receipts"])
        or not isinstance(files["trading_calendar"], str)
        or not isinstance(files["market_rules"], str)
    ):
        raise ProviderMaterializationError("trusted-local prerequisite files are invalid")
    retained: list[_RetainedInput] = []
    try:
        receipts = tuple(
            _retain_input(path, f"trusted-local source receipt {index}")
            for index, path in enumerate(files["source_receipts"])
        )
        retained.extend(receipts)
        calendar = _retain_input(files["trading_calendar"], "trusted-local calendar")
        retained.append(calendar)
        rules = _retain_input(files["market_rules"], "trusted-local market rules")
        retained.append(rules)
        paths = [
            registration.path,
            *(item.path for item in receipts),
            calendar.path,
            rules.path,
        ]
        identities = [
            _snapshot_physical_identity(item.opened.identity, item.field)
            for item in (registration, *receipts, calendar, rules)
        ]
        if len(set(paths)) != len(paths) or len(set(identities)) != len(identities):
            raise ProviderMaterializationError("trusted-local prerequisite files alias")
        return value, tuple(files["source_receipts"]), receipts, calendar, rules
    except BaseException:
        for item in reversed(retained):
            item.opened.close()
        raise


def _select_trusted_local_market_rules(
    rules: Mapping[str, object], *, instrument_status: Mapping[str, object]
) -> Mapping[str, object]:
    records = rules.get("records")
    statuses = instrument_status.get("records")
    if not isinstance(records, list) or not isinstance(statuses, list):
        raise ProviderMaterializationError("trusted-local market rules are invalid")
    selected_status: dict[str, bool] = {}
    for record in statuses:
        if not isinstance(record, Mapping):
            raise ProviderMaterializationError("instrument status record is invalid")
        entry = record.get("panel_entry")
        payload = record.get("payload")
        if (
            not isinstance(entry, str)
            or not isinstance(payload, Mapping)
            or type(payload.get("is_st")) is not bool
            or entry in selected_status
        ):
            raise ProviderMaterializationError("instrument status selection is invalid")
        selected_status[entry] = payload["is_st"]
    selected: dict[str, Mapping[str, object]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ProviderMaterializationError("market rule record is invalid")
        entry = record.get("panel_entry")
        payload = record.get("payload")
        if not isinstance(entry, str) or not isinstance(payload, Mapping):
            raise ProviderMaterializationError("market rule record is invalid")
        if entry in selected_status and payload.get("is_st") is selected_status[entry]:
            if entry in selected:
                raise ProviderMaterializationError("market rule selection is ambiguous")
            selected[entry] = record
    if set(selected) != set(selected_status):
        raise ProviderMaterializationError("market rule selection is incomplete")
    result = dict(rules)
    result["records"] = [
        selected[entry]
        for entry in sorted(selected)
    ]
    return result


def _build_trusted_local_availability(
    *,
    panel: list[str],
    components: Mapping[str, Mapping[str, object]],
    decision_cutoffs: Mapping[str, str],
    calendar_phases: Mapping[str, Mapping[str, str]],
    source_receipt_ids: Sequence[str],
) -> dict[str, object]:
    calendar = components.get("trading_calendar")
    if not isinstance(calendar, Mapping) or not isinstance(
        calendar.get("records"), list
    ):
        raise ProviderMaterializationError("trusted-local calendar records are invalid")
    phases = {
        record.get("panel_entry"): record.get("payload")
        for record in calendar["records"]
        if isinstance(record, Mapping)
    }
    if set(phases) != set(panel) or any(
        not isinstance(value, Mapping) for value in phases.values()
    ):
        raise ProviderMaterializationError("trusted-local calendar phases are invalid")
    records: list[dict[str, object]] = []
    for component, artifact in components.items():
        if component == "availability_records":
            continue
        component_records = artifact.get("records")
        if not isinstance(component_records, list):
            raise ProviderMaterializationError(
                f"{component} component records are invalid"
            )
        for record in component_records:
            if not isinstance(record, Mapping):
                raise ProviderMaterializationError(
                    f"{component} component record is invalid"
                )
            entry = record.get("panel_entry")
            if not isinstance(entry, str) or entry not in phases:
                raise ProviderMaterializationError(
                    f"{component} component panel is invalid"
                )
            cutoff_kind = (
                "next_session_decision_cutoff_at"
                if component in {"execution_prices", "signal_prices"}
                else "decision_cutoff_at"
            )
            cutoff = phases[entry].get(cutoff_kind)
            if not isinstance(cutoff, str):
                raise ProviderMaterializationError("trusted-local cutoff is invalid")
            records.append(
                {
                    "component": component,
                    "panel_entry": entry,
                    "record_sha256": record.get("record_sha256"),
                    "source_receipt_ids": record.get("source_receipt_ids"),
                    "event_at": record.get("effective_at"),
                    "available_at": record.get("available_at"),
                    "cutoff_kind": cutoff_kind,
                    "applicable_cutoff_at": cutoff,
                }
            )
    artifact = {
        "schema_version": COMPONENT_SCHEMAS["availability_records"],
        "panel": panel,
        "records": sorted(
            records,
            key=lambda record: (
                str(record["component"]),
                str(record["panel_entry"]),
                str(record["record_sha256"]),
            ),
        ),
    }
    try:
        verified = verify_component_availability_records(
            artifact,
            expected_panel_sha256=hashlib.sha256(_canonical(panel)).hexdigest(),
            expected_panel_size=len(panel),
            expected_decision_cutoffs=decision_cutoffs,
            bound_source_receipt_ids=sorted(source_receipt_ids),
            component_records={
                component: cast(Sequence[Mapping[str, object]], artifact_value["records"])
                for component, artifact_value in components.items()
                if component != "availability_records"
            },
            expected_signed_calendar_phases=calendar_phases,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderMaterializationError(
            "trusted-local availability closure is invalid"
        ) from exc
    if (
        not verified.ready
        or verified.source_receipt_ids != tuple(sorted(source_receipt_ids))
    ):
        raise ProviderMaterializationError("trusted-local availability closure is incomplete")
    return artifact


def materialize_registered_provider_bundle(
    *,
    registration_file: str | Path,
    database: str | Path,
    output_dir: str | Path,
) -> Path:
    """Atomically materialize one complete trusted-local collector snapshot."""

    try:
        destination = Path(
            os.path.abspath(os.path.expanduser(os.fspath(output_dir)))
        )
    except (TypeError, ValueError) as exc:
        raise ProviderMaterializationError("output_dir is invalid") from exc
    retained: list[_RetainedInput] = []
    private: Path | None = None
    published = False
    result: Path | None = None
    body_error: BaseException | None = None
    try:
        registration = _retain_input(registration_file, "trusted-local registration")
        retained.append(registration)
        live_database = _retain_input(database, "trusted-local database")
        retained.append(live_database)
        _require_complete_sqlite_bytes(live_database.raw)
        (
            registration_value,
            _,
            source_receipts,
            calendar,
            market_rules,
        ) = _trusted_local_registration_inputs(registration)
        retained.extend((*source_receipts, calendar, market_rules))
        ledger = _retain_input(
            default_collector_ledger_path(str(live_database.path)),
            "trusted-local ledger",
        )
        retained.append(ledger)
        retained_inputs = {
            "registration": registration,
            "database": live_database,
            "ledger": ledger,
            "prerequisites": (source_receipts, calendar, market_rules),
        }
        try:
            verify_registered_collector_materialization_complete(
                registration_file,
                database=database,
                _retained_inputs=retained_inputs,
            )
        except CollectorContinuityError as exc:
            raise ProviderMaterializationError(str(exc)) from exc
        for item in retained:
            _reverify_retained_input(item)
        symbols = registration_value.get("symbols")
        sessions = registration_value.get("sessions")
        if not isinstance(symbols, list) or not isinstance(sessions, list):
            raise ProviderMaterializationError("trusted-local panel is invalid")
        panel = sorted(f"{symbol}@{session}" for symbol in symbols for session in sessions)
        if (
            len(panel) != 36
            or len(panel) != len(set(panel))
            or registration_value.get("panel_sha256")
            != hashlib.sha256(_canonical(panel)).hexdigest()
        ):
            raise ProviderMaterializationError("trusted-local panel is invalid")
        receipt_values = {
            hashlib.sha256(item.raw).hexdigest(): _json(item.raw, item.field)
            for item in source_receipts
        }
        if len(receipt_values) != 2:
            raise ProviderMaterializationError("trusted-local source receipts alias")
        calendar_value = _json(calendar.raw, calendar.field)
        market_rules_value = _json(market_rules.raw, market_rules.field)
        try:
            local = validate_local_mechanical_prerequisites(
                calendar_artifact=calendar_value,
                market_rules_artifact=market_rules_value,
                expected_panel=panel,
                bound_source_receipts=receipt_values,
            )
        except (TypeError, ValueError) as exc:
            raise ProviderMaterializationError(
                "trusted-local prerequisites are invalid"
            ) from exc
        prerequisites = registration_value.get("prerequisites")
        if not isinstance(prerequisites, Mapping) or any(
            prerequisites.get(field) != value for field, value in local.items()
        ):
            raise ProviderMaterializationError("trusted-local prerequisites drifted")
        calendar_prerequisite = prerequisites.get("trading_calendar")
        rules_prerequisite = prerequisites.get("market_rule_prerequisite")
        if (
            not isinstance(calendar_prerequisite, Mapping)
            or not isinstance(rules_prerequisite, Mapping)
            or calendar_prerequisite.get("artifact_sha256")
            != hashlib.sha256(calendar.raw).hexdigest()
            or rules_prerequisite.get("artifact_sha256")
            != hashlib.sha256(market_rules.raw).hexdigest()
        ):
            raise ProviderMaterializationError("trusted-local prerequisite bytes drifted")
        calendar_local = local.get("trading_calendar")
        if not isinstance(calendar_local, Mapping):
            raise ProviderMaterializationError("trusted-local calendar is invalid")
        decision_cutoffs = calendar_local.get("decision_cutoff_by_panel")
        calendar_phases = calendar_local.get("calendar_phases_by_panel")
        if not isinstance(decision_cutoffs, Mapping) or not isinstance(
            calendar_phases, Mapping
        ):
            raise ProviderMaterializationError("trusted-local calendar is invalid")

        private = _new_private_materialization_directory(destination)
        try:
            snapshot_result = create_registered_collector_materialization_snapshot(
                registration_file,
                database=database,
                staging_directory=private,
                _retained_inputs=retained_inputs,
            )
        except CollectorContinuityError as exc:
            raise ProviderMaterializationError(str(exc)) from exc
        snapshot = _validate_collector_snapshot(
            snapshot_result, staging_parent=private
        )
        execution_value = {
            "schema_version": EXECUTION_ADJUSTMENT_SCHEMA,
            "price_role": "execution",
            "source": registration_value["source"],
            "adjustment_mode": registration_value["adjustment_mode"],
            "adjustment_version": registration_value["adjustment_version"],
        }
        signal_value = {
            "schema_version": SIGNAL_ADJUSTMENT_SCHEMA,
            "price_role": "signal",
            "source": registration_value["source"],
            "adjustment_mode": registration_value["adjustment_mode"],
            "adjustment_version": registration_value["adjustment_version"],
        }
        execution = verify_adjustment_identity(
            execution_value, expected_price_role="execution"
        )
        signal = verify_adjustment_identity(
            signal_value, expected_price_role="signal"
        )
        try:
            intrinsic = reconstruct_intrinsic_evidence(
                snapshot.database.raw,
                panel=panel,
                execution_adjustment=execution,
                signal_adjustment=signal,
                decision_cutoffs=cast(Mapping[str, str], decision_cutoffs),
            )
            forward = reconstruct_forward_component_evidence(
                snapshot.database.raw,
                panel=panel,
                decision_cutoffs=cast(Mapping[str, str], decision_cutoffs),
            )
        except IntrinsicEvidenceError as exc:
            raise ProviderMaterializationError(exc.code) from exc
        collector_receipts = dict(intrinsic.source_receipts)
        for receipt_id, receipt in forward.source_receipts.items():
            existing = collector_receipts.get(receipt_id)
            if existing is not None and existing != receipt:
                raise ProviderMaterializationError("collector receipt identity drifted")
            collector_receipts[receipt_id] = receipt
        component_values: dict[str, Mapping[str, object]] = {
            "trading_calendar": cast(Mapping[str, object], calendar_value),
            "market_rules": _select_trusted_local_market_rules(
                cast(Mapping[str, object], market_rules_value),
                instrument_status=forward.components["instrument_status"],
            ),
            **intrinsic.components,
            **forward.components,
        }
        component_references = {
            component: _reference(
                f"stock-data-{component.replace('_', '-')}",
                COMPONENT_SCHEMAS[component],
                _canonical(component_values[component]),
            )
            for component in (*INTRINSIC_COMPONENTS, *FORWARD_COMPONENTS)
        }
        try:
            verify_intrinsic_evidence(
                intrinsic,
                claimed_components={
                    component: component_values[component]
                    for component in INTRINSIC_COMPONENTS
                },
                component_references={
                    component: component_references[component]
                    for component in INTRINSIC_COMPONENTS
                },
                bound_source_receipts=collector_receipts,
                database_sha256=snapshot.database.reference.identifier,
            )
            verify_forward_component_evidence(
                forward,
                claimed_components={
                    component: component_values[component]
                    for component in FORWARD_COMPONENTS
                },
                component_references={
                    component: component_references[component]
                    for component in FORWARD_COMPONENTS
                },
                bound_source_receipts=collector_receipts,
            )
        except IntrinsicEvidenceError as exc:
            raise ProviderMaterializationError(exc.code) from exc
        component_values["availability_records"] = _build_trusted_local_availability(
            panel=panel,
            components=component_values,
            decision_cutoffs=cast(Mapping[str, str], decision_cutoffs),
            calendar_phases=cast(Mapping[str, Mapping[str, str]], calendar_phases),
            source_receipt_ids=[*receipt_values, *collector_receipts],
        )

        derived = private / "derived"
        derived.mkdir(mode=0o700)
        _fsync_directory(derived, "registered materializer derived directory")
        panel_file = _write_exclusive(
            derived / "panel.json", _canonical(panel), "registered materializer panel"
        )
        execution_file = _write_exclusive(
            derived / "execution-adjustment.json",
            _canonical(execution_value),
            "registered materializer execution adjustment",
        )
        signal_file = _write_exclusive(
            derived / "signal-adjustment.json",
            _canonical(signal_value),
            "registered materializer signal adjustment",
        )
        receipt_files = [
            _write_exclusive(
                derived / f"receipt-{index}.json",
                item.raw,
                f"registered materializer source receipt {index}",
            )
            for index, item in enumerate(source_receipts)
        ]
        component_files = {
            component: _write_exclusive(
                derived / f"{component}.json",
                _canonical(component_values[component]),
                f"registered materializer component {component}",
            )
            for component in REQUIRED_COMPONENTS
        }
        _fsync_directory(derived, "registered materializer derived directory")
        materialized = materialize_provider_bundle(
            output_dir=private / "bundle-stage",
            database_file=snapshot.database.path,
            registration_file=snapshot.registration.path,
            snapshot_staging_directory=private,
            panel_file=panel_file,
            source_receipt_files=receipt_files,
            execution_adjustment_file=execution_file,
            signal_adjustment_file=signal_file,
            component_files=component_files,
            source=str(registration_value["source"]),
            _collector_snapshot=snapshot,
            _embed_collector_snapshot=True,
            _published_output_dir=destination,
        )
        staged_bundle = Path(materialized["bundle_file"])
        if staged_bundle.parent != private / "bundle-stage":
            raise ProviderMaterializationError("registered materializer output drifted")
        _reverify_collector_snapshot(snapshot)
        for item in retained:
            _reverify_retained_input(item)
        os.replace(staged_bundle.parent, destination)
        published = True
        result = destination / "bundle.json"
        export_verified_provider_receipt(result)
        _reverify_collector_snapshot(snapshot)
        for item in retained:
            _reverify_retained_input(item)
    except BaseException as exc:
        body_error = exc

    cleanup_error: BaseException | None = None
    if body_error is not None and published:
        try:
            _remove_private_materialization_directory(destination)
        except BaseException as exc:
            cleanup_error = exc
    if private is not None:
        try:
            _remove_private_materialization_directory(private)
        except BaseException as exc:
            cleanup_error = exc if cleanup_error is None else cleanup_error
    for item in reversed(retained):
        try:
            item.opened.close()
        except BaseException as exc:
            cleanup_error = exc if cleanup_error is None else cleanup_error
    if body_error is not None:
        if cleanup_error is not None:
            raise ProviderMaterializationError(
                "registered materialization and cleanup failed"
            ) from body_error
        raise body_error
    if cleanup_error is not None:
        if published:
            _remove_private_materialization_directory(destination)
        raise ProviderMaterializationError("registered materialization cleanup failed")
    if result is None:
        raise AssertionError("registered materialization result is unavailable")
    return result
