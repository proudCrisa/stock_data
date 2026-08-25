"""Read-only export of a verified stock_data contract and readiness receipt."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from stockdata.collector_continuity import (
    CLOSURE_SCHEMA,
    CONTINUITY_CLOSURE_REFERENCE_KIND,
    CollectorContinuityError,
    OpenedRegularFile,
    open_nofollow_regular,
    verify_registered_collector_materialization_snapshot,
    verify_file_identity,
)
from stockdata.companion_snapshot import (
    build_companion_snapshot,
    verify_bound_readiness,
)
from stockdata.rqgm_provider_contract import (
    COMPANION_SNAPSHOT_SCHEMA,
    DATABASE_SCHEMA,
    EXACT_PANEL_SCHEMA,
    READINESS_REPORT_SCHEMA,
    REQUIRED_COMPONENTS,
    ProviderArtifactReference,
    build_rqgm_provider_contract,
)


_ExceptionGroup = getattr(builtins, "ExceptionGroup", None)
_BaseExceptionGroup = getattr(builtins, "BaseExceptionGroup", None)


BUNDLE_SCHEMA = "stockdata-rqgm-provider-bundle/2"
EXPORT_SCHEMA = "stockdata-rqgm-provider-export/1"
REGISTRATION_REFERENCE_KIND = "stock-data-forward-panel-registration"
REGISTRATION_SCHEMA = "rqgm-forward-panel-registration/4"
LEDGER_REFERENCE_KIND = "stock-data-forward-collector-ledger-snapshot"
LEDGER_SNAPSHOT_SCHEMA = "stockdata-forward-collector-ledger-snapshot/1"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


@dataclass
class _RetainedArtifact:
    path: Path
    opened: OpenedRegularFile
    raw: bytes
    field: str


def _read_retained(opened: OpenedRegularFile, field: str) -> bytes:
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
            raise ValueError(f"{field} artifact was truncated")
        raw.extend(chunk)
        offset += len(chunk)
    current = os.fstat(opened.descriptor)
    if (
        current.st_dev != status.st_dev
        or current.st_ino != status.st_ino
        or current.st_size != status.st_size
    ):
        raise ValueError(f"{field} artifact identity has drifted")
    return bytes(raw)


def _open_retained(path: str | Path, field: str) -> _RetainedArtifact:
    candidate = os.path.abspath(os.path.expanduser(os.fspath(path)))
    try:
        opened = open_nofollow_regular(candidate)
    except (CollectorContinuityError, OSError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must name a canonical no-follow regular file") from exc
    try:
        return _RetainedArtifact(
            Path(candidate), opened, _read_retained(opened, field), field
        )
    except BaseException:
        opened.close()
        raise


def _locator(
    value: object, field: str
) -> tuple[ProviderArtifactReference, _RetainedArtifact]:
    if not isinstance(value, Mapping) or set(value) != {"reference", "path"}:
        raise ValueError(f"{field} locator is incomplete")
    reference = ProviderArtifactReference.from_dict(value["reference"])
    reference.validate()
    path_value = value["path"]
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"{field} locator path is invalid")
    canonical = os.path.abspath(os.path.expanduser(path_value))
    if path_value != canonical:
        raise ValueError(f"{field} locator path must be canonical")
    retained = _open_retained(canonical, field)
    if hashlib.sha256(retained.raw).hexdigest() != reference.identifier:
        retained.opened.close()
        raise ValueError(f"{field} artifact content has drifted (identity mismatch)")
    return reference, retained


def _require_reference(
    reference: ProviderArtifactReference,
    field: str,
    *,
    kind: str,
    schema: str,
) -> None:
    if reference.kind != kind or reference.schema_version != schema:
        raise ValueError(f"{field} reference has wrong kind or schema")


def _physical_identity(identity: object, field: str) -> tuple[int, int]:
    device = getattr(identity, "file_st_dev", None)
    inode = getattr(identity, "file_st_ino", None)
    if type(device) is not int or type(inode) is not int:
        raise ValueError(f"{field} physical identity is invalid")
    return device, inode


def _reject_locator_aliases(
    values: list[tuple[object, str]],
) -> None:
    references: set[ProviderArtifactReference] = set()
    paths: set[str] = set()
    for value, field in values:
        if not isinstance(value, Mapping) or set(value) != {"reference", "path"}:
            raise ValueError(f"{field} locator is incomplete")
        reference = ProviderArtifactReference.from_dict(value["reference"])
        reference.validate()
        path = value["path"]
        if not isinstance(path, str) or not path:
            raise ValueError(f"{field} locator path is invalid")
        if reference in references:
            raise ValueError("provider bundle repeats an artifact reference")
        if path in paths:
            raise ValueError("provider bundle aliases multiple references to one path")
        references.add(reference)
        paths.add(path)


def _verify_provider_bundle_core(
    bundle: object,
    bundle_raw: bytes,
    retained_artifacts: list[_RetainedArtifact],
) -> dict[str, object]:
    """Verify one unpublished or published canonical bundle mapping and bytes."""

    if _canonical(bundle) != bundle_raw:
        raise ValueError("provider bundle must use canonical JSON bytes")
    required = {
        "schema_version",
        "coverage_start",
        "coverage_end",
        "checkout",
        "database",
        "registration",
        "ledger_snapshot",
        "continuity_closure",
        "source_receipts",
        "execution_adjustment_identity",
        "signal_adjustment_identity",
        "exact_panel",
        "components",
        "readiness_report",
    }
    if not isinstance(bundle, Mapping) or set(bundle) != required:
        raise ValueError("provider bundle schema is incomplete")
    if bundle["schema_version"] != BUNDLE_SCHEMA:
        raise ValueError("unsupported provider bundle schema")
    receipt_values = bundle["source_receipts"]
    component_values = bundle["components"]
    if not isinstance(receipt_values, list) or not isinstance(
        component_values, Mapping
    ):
        raise ValueError("provider bundle receipt or component locators are malformed")
    if set(component_values) != set(REQUIRED_COMPONENTS):
        raise ValueError("provider bundle component set is incomplete")
    _reject_locator_aliases(
        [
            (bundle["checkout"], "checkout"),
            (bundle["database"], "database"),
            (bundle["registration"], "registration"),
            (bundle["ledger_snapshot"], "ledger_snapshot"),
            (bundle["continuity_closure"], "continuity_closure"),
            *(
                (value, f"source_receipts[{index}]")
                for index, value in enumerate(receipt_values)
            ),
            (bundle["execution_adjustment_identity"], "execution_adjustment_identity"),
            (bundle["signal_adjustment_identity"], "signal_adjustment_identity"),
            (bundle["exact_panel"], "exact_panel"),
            *(
                (component_values[component], f"components.{component}")
                for component in REQUIRED_COMPONENTS
            ),
            (bundle["readiness_report"], "readiness_report"),
        ]
    )
    paths: dict[ProviderArtifactReference, _RetainedArtifact] = {}
    references_by_path: dict[Path, ProviderArtifactReference] = {}
    references_by_identity: dict[tuple[int, int], ProviderArtifactReference] = {}

    def bind(value: object, field: str) -> ProviderArtifactReference:
        reference, retained = _locator(value, field)
        retained_artifacts.append(retained)
        path = retained.path
        identity = retained.opened.identity
        if reference in paths:
            raise ValueError("provider bundle repeats an artifact reference")
        if path in references_by_path:
            raise ValueError("provider bundle aliases multiple references to one path")
        physical_identity = _physical_identity(identity, field)
        if physical_identity in references_by_identity:
            raise ValueError(
                "provider bundle aliases multiple references to one physical file"
            )
        paths[reference] = retained
        references_by_path[path] = reference
        references_by_identity[physical_identity] = reference
        return reference

    checkout = bind(bundle["checkout"], "checkout")
    database = bind(bundle["database"], "database")
    _require_reference(
        database,
        "database",
        kind="stock-data-database",
        schema=DATABASE_SCHEMA,
    )
    registration = bind(bundle["registration"], "registration")
    _require_reference(
        registration,
        "registration",
        kind=REGISTRATION_REFERENCE_KIND,
        schema=REGISTRATION_SCHEMA,
    )
    ledger = bind(bundle["ledger_snapshot"], "ledger_snapshot")
    _require_reference(
        ledger,
        "ledger_snapshot",
        kind=LEDGER_REFERENCE_KIND,
        schema=LEDGER_SNAPSHOT_SCHEMA,
    )
    continuity_closure = bind(
        bundle["continuity_closure"], "continuity_closure"
    )
    _require_reference(
        continuity_closure,
        "continuity_closure",
        kind=CONTINUITY_CLOSURE_REFERENCE_KIND,
        schema=CLOSURE_SCHEMA,
    )
    source_receipts = tuple(
        bind(value, f"source_receipts[{index}]")
        for index, value in enumerate(receipt_values)
    )
    execution_adjustment = bind(
        bundle["execution_adjustment_identity"],
        "execution_adjustment_identity",
    )
    signal_adjustment = bind(
        bundle["signal_adjustment_identity"],
        "signal_adjustment_identity",
    )
    exact_panel = bind(bundle["exact_panel"], "exact_panel")
    _require_reference(
        exact_panel,
        "exact_panel",
        kind="stock-data-exact-panel",
        schema=EXACT_PANEL_SCHEMA,
    )
    components = {
        component: bind(component_values[component], f"components.{component}")
        for component in REQUIRED_COMPONENTS
    }
    readiness_reference = bind(bundle["readiness_report"], "readiness_report")
    readiness_raw = paths[readiness_reference].raw
    if (
        readiness_reference.kind != "stock-data-readiness-report"
        or readiness_reference.schema_version != READINESS_REPORT_SCHEMA
    ):
        raise ValueError("readiness report reference has wrong kind or schema")

    try:
        verify_registered_collector_materialization_snapshot(
            paths[registration].raw,
            paths[ledger].raw,
            paths[continuity_closure].raw,
            paths[database].opened,
            database.to_dict(),
            exact_panel_raw=paths[exact_panel].raw,
        )
    except CollectorContinuityError as exc:
        raise ValueError(
            "collector continuity semantic verification failed"
        ) from exc

    companion = build_companion_snapshot(
        coverage_start=bundle["coverage_start"],
        coverage_end=bundle["coverage_end"],
        checkout=checkout,
        database=database,
        source_receipts=source_receipts,
        execution_adjustment_identity=execution_adjustment,
        signal_adjustment_identity=signal_adjustment,
        exact_panel=exact_panel,
        components=components,
    )
    contract = build_rqgm_provider_contract(
        checkout=checkout,
        database=database,
        source_receipts=source_receipts,
        execution_adjustment_identity=execution_adjustment,
        signal_adjustment_identity=signal_adjustment,
        exact_panel=exact_panel,
        readiness_report=readiness_reference,
        companion_snapshot=ProviderArtifactReference(
            "stock-data-companion-snapshot",
            companion.snapshot_sha256,
            COMPANION_SNAPSHOT_SCHEMA,
        ),
    )
    try:
        report = json.loads(readiness_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("readiness report is unreadable or invalid JSON") from exc

    def content_reader(reference: ProviderArtifactReference) -> bytes:
        bound = paths.get(reference)
        if bound is None:
            raise ValueError(f"no provider path is bound for {reference.kind}")
        raw = _read_retained(bound.opened, bound.field)
        try:
            verify_file_identity(bound.path, bound.opened.identity)
        except CollectorContinuityError as exc:
            raise ValueError(
                f"{reference.kind} artifact identity has drifted"
            ) from exc
        if raw != bound.raw or hashlib.sha256(raw).hexdigest() != reference.identifier:
            raise ValueError(f"{reference.kind} artifact content has drifted")
        return raw

    ready = verify_bound_readiness(
        report=report,
        contract=contract,
        companion_snapshot=companion,
        content_reader=content_reader,
    )
    for reference in paths:
        content_reader(reference)
    return {
        "schema_version": EXPORT_SCHEMA,
        "ready": ready,
        "contract": contract.to_dict(),
        "companion_snapshot": companion.to_dict(),
        "readiness_report": report,
    }


def _collect_provider_bundle_verification(
    bundle: object, bundle_raw: bytes
) -> tuple[
    dict[str, object] | None,
    BaseException | None,
    list[BaseException],
]:
    """Verify one canonical bundle and collect every locator close failure."""

    retained: list[_RetainedArtifact] = []
    body_error: BaseException | None = None
    result: dict[str, object] | None = None
    try:
        result = _verify_provider_bundle_core(bundle, bundle_raw, retained)
    except BaseException as exc:
        body_error = exc
    close_errors: list[BaseException] = []
    for artifact in reversed(retained):
        try:
            artifact.opened.close()
        except BaseException as exc:
            close_errors.append(exc)
    if body_error is None and result is None:
        body_error = ValueError("provider bundle verification produced no result")
    return result, body_error, close_errors


def _raise_provider_cleanup_failures(
    body_error: BaseException | None,
    close_errors: list[BaseException],
    *,
    body_label: str,
    cleanup_label: str,
) -> None:
    """Raise one ordered native group or deterministic compatibility wrapper."""

    if body_error is not None:
        if close_errors:
            group_type = (
                _ExceptionGroup
                if isinstance(body_error, Exception)
                and all(isinstance(error, Exception) for error in close_errors)
                else _BaseExceptionGroup
            )
            if group_type is not None:
                raise group_type(body_label, [body_error, *close_errors])
            classes = ", ".join(type(error).__name__ for error in close_errors)
            raise ValueError(
                f"{body_label}; additional cleanup failures: "
                f"{len(close_errors)} ({classes})"
            ) from body_error
        raise body_error
    if close_errors:
        group_type = (
            _ExceptionGroup
            if all(isinstance(error, Exception) for error in close_errors)
            else _BaseExceptionGroup
        )
        if group_type is not None:
            raise group_type(cleanup_label, close_errors)
        additional = close_errors[1:]
        message = cleanup_label
        if additional:
            classes = ", ".join(type(error).__name__ for error in additional)
            message = (
                f"{message}; additional cleanup failures: {len(additional)} "
                f"({classes})"
            )
        raise ValueError(message) from close_errors[0]


def _verify_provider_bundle(
    bundle: object, bundle_raw: bytes
) -> dict[str, object]:
    """Verify one canonical bundle while retaining every locator descriptor."""

    result, body_error, close_errors = _collect_provider_bundle_verification(
        bundle, bundle_raw
    )
    _raise_provider_cleanup_failures(
        body_error,
        close_errors,
        body_label="provider bundle verification and locator cleanup failed",
        cleanup_label="provider bundle locator cleanup failed",
    )
    if result is None:
        raise AssertionError("provider bundle verification result is unavailable")
    return result


def _export_verified_provider_receipt(
    bundle_file: str | Path,
) -> dict[str, object]:
    """Read one unpublished or published bundle and delegate verification."""

    try:
        bundle_path = os.path.abspath(
            os.path.expanduser(os.fspath(bundle_file))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("provider bundle path is invalid") from exc
    retained = _open_retained(bundle_path, "provider bundle")
    result: dict[str, object] | None = None
    body_error: BaseException | None = None
    close_errors: list[BaseException] = []
    try:
        try:
            bundle = json.loads(retained.raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("provider bundle is unreadable or invalid JSON") from exc
        result, body_error, close_errors = _collect_provider_bundle_verification(
            bundle, retained.raw
        )
        if body_error is None and not close_errors:
            if _read_retained(retained.opened, retained.field) != retained.raw:
                raise ValueError("provider bundle content has drifted")
            try:
                verify_file_identity(retained.path, retained.opened.identity)
            except CollectorContinuityError as exc:
                raise ValueError("provider bundle identity has drifted") from exc
    except BaseException as exc:
        body_error = exc
    try:
        retained.opened.close()
    except BaseException as exc:
        close_errors.append(exc)
    _raise_provider_cleanup_failures(
        body_error,
        close_errors,
        body_label="provider bundle verification and cleanup failed",
        cleanup_label="provider bundle cleanup failed",
    )
    if result is None:
        raise AssertionError("provider bundle export result is unavailable")
    return result


def export_verified_provider_receipt(bundle_file: str | Path) -> dict[str, object]:
    """Verify one formally published bundle without any writes."""

    try:
        supplied = os.path.expanduser(os.fspath(bundle_file))
        bundle_path = os.path.abspath(supplied)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider bundle path is invalid") from exc
    if supplied != bundle_path:
        raise ValueError("public provider bundle path must be lexical absolute")
    if os.path.basename(bundle_path) != "bundle.json":
        raise ValueError("public provider export requires formal bundle.json")
    return _export_verified_provider_receipt(bundle_path)
