"""Read-only export of a verified stock_data contract and readiness receipt."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from stockdata.collector_continuity import (
    CLOSURE_SCHEMA,
    CONTINUITY_CLOSURE_REFERENCE_KIND,
    CollectorContinuityError,
    OpenedRegularFile,
    open_nofollow_regular,
    parse_collector_ledger,
    verify_file_identity,
    verify_registered_collector_materialization_snapshot,
)
from stockdata.companion_snapshot import (
    build_companion_snapshot,
    verify_bound_readiness,
)
from stockdata.rqgm_provider_contract import (
    COMPANION_SNAPSHOT_SCHEMA,
    COMPONENT_SCHEMAS,
    DATABASE_SCHEMA,
    EXACT_PANEL_SCHEMA,
    READINESS_REPORT_SCHEMA,
    REQUIRED_COMPONENTS,
    ProviderArtifactReference,
    build_rqgm_provider_contract,
)
from stockdata.ticker import normalize
from stockdata.trusted_local_research_replay_export import (
    build_trusted_local_research_replay_export,
    verify_trusted_local_research_replay_export,
)
from stockdata.trusted_local_research_replay_materialization import (
    build_trusted_local_research_replay_materialization,
    verify_trusted_local_research_replay_materialization,
)

_ExceptionGroup = getattr(builtins, "ExceptionGroup", None)
_BaseExceptionGroup = getattr(builtins, "BaseExceptionGroup", None)


BUNDLE_SCHEMA = "stockdata-rqgm-provider-bundle/2"
EXPORT_SCHEMA = "stockdata-rqgm-provider-export/1"
REGISTRATION_REFERENCE_KIND = "stock-data-forward-panel-registration"
REGISTRATION_SCHEMA = "rqgm-forward-panel-registration/4"
TRUSTED_LOCAL_REGISTRATION_SCHEMA = "rqgm-forward-panel-registration/5"
REGISTRATION_SCHEMAS = frozenset(
    {REGISTRATION_SCHEMA, TRUSTED_LOCAL_REGISTRATION_SCHEMA}
)
TRUSTED_LOCAL_READINESS_BLOCKER = "trusted_local_mechanical_has_no_readiness_authority"
LEDGER_REFERENCE_KIND = "stock-data-forward-collector-ledger-snapshot"
LEDGER_SNAPSHOT_SCHEMA = "stockdata-forward-collector-ledger-snapshot/1"
RESOLVED_RESEARCH_INPUTS_SCHEMA = "stockdata-rqgm-research-replay-resolved-inputs/1"
RESEARCH_REPLAY_POLICY_REQUEST_SCHEMA = (
    "stockdata-rqgm-trusted-local-research-replay-policy-request/1"
)
RESEARCH_REPLAY_ENVELOPE_SCHEMA = (
    "stockdata-rqgm-trusted-local-research-replay-envelope/1"
)
_NO_RESEARCH_POLICY = object()


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
    schema: str | frozenset[str],
) -> None:
    schemas = {schema} if isinstance(schema, str) else schema
    if reference.kind != kind or reference.schema_version not in schemas:
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


def _require_trusted_local_negative_readiness(report: object) -> None:
    """Reject any `/5` bundle that attempts to turn local provenance into readiness."""

    if not isinstance(report, Mapping) or report.get("ready") is not False:
        raise ValueError("trusted-local registration cannot grant readiness")
    components = report.get("components")
    blockers = report.get("blockers")
    if not isinstance(components, Mapping) or not isinstance(blockers, list):
        raise TypeError("trusted-local readiness report is malformed")
    for component in REQUIRED_COMPONENTS:
        evidence = components.get(component)
        if not isinstance(evidence, Mapping) or evidence.get("ready") is not False:
            raise ValueError(
                "trusted-local registration cannot grant component readiness"
            )
        component_blockers = evidence.get("blockers")
        if not isinstance(component_blockers, list) or not any(
            isinstance(blocker, Mapping)
            and blocker.get("code") == TRUSTED_LOCAL_READINESS_BLOCKER
            and blocker.get("count") == 1
            for blocker in component_blockers
        ):
            raise ValueError("trusted-local component lacks provenance-only blocker")
    if not any(
        isinstance(blocker, Mapping)
        and blocker.get("code") == TRUSTED_LOCAL_READINESS_BLOCKER
        and blocker.get("count") == 1
        for blocker in blockers
    ):
        raise ValueError("trusted-local readiness lacks provenance-only blocker")


def _require_trusted_local_registration_inputs(
    *,
    registration_raw: bytes,
    source_receipts: tuple[ProviderArtifactReference, ...],
    components: Mapping[str, ProviderArtifactReference],
) -> None:
    """Recheck `/5` bundle inputs against the retained registration bytes."""

    try:
        registration = json.loads(registration_raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("trusted-local registration is unreadable or invalid JSON") from exc
    if (
        not isinstance(registration, Mapping)
        or registration.get("schema_version") != TRUSTED_LOCAL_REGISTRATION_SCHEMA
        or registration.get("authority_mode") != "trusted_local_mechanical"
    ):
        raise ValueError("trusted-local registration schema or authority mode is invalid")

    # provider_materializer imports this module, so defer the shared verifier import.
    from stockdata.provider_materializer import (
        _require_trusted_local_materialization_inputs,
    )

    _require_trusted_local_materialization_inputs(
        registration_raw=registration_raw,
        source_receipts=source_receipts,
        components=components,
    )


def _research_reference(reference: ProviderArtifactReference) -> dict[str, str]:
    return {
        "schema_version": reference.schema_version,
        "sha256": reference.identifier,
    }


def _research_receipt_reference(
    receipt_id: str, receipt: Mapping[str, object]
) -> dict[str, str]:
    schema_version = receipt.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        raise ValueError("reconstructed source receipt schema is invalid")
    if hashlib.sha256(_canonical(receipt)).hexdigest() != receipt_id:
        raise ValueError("reconstructed source receipt identity has drifted")
    return {"schema_version": schema_version, "sha256": receipt_id}


def _research_json(raw: bytes, field: str) -> object:
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} is unreadable or invalid JSON") from exc
    if _canonical(value) != raw:
        raise ValueError(f"{field} must be canonical JSON data")
    return value


def _research_panel_projection(panel: object) -> tuple[list[str], int, int]:
    """Project the formal panel set into the research closure's fixed order."""

    if not isinstance(panel, list) or len(panel) != 36:
        raise ValueError("exact panel must contain exactly 36 cells")
    cells: set[str] = set()
    symbols: set[str] = set()
    sessions: set[str] = set()
    for cell in panel:
        if not isinstance(cell, str) or cell.count("@") != 1:
            raise ValueError("exact panel cell is invalid")
        symbol, session = cell.split("@")
        try:
            parsed_session = date.fromisoformat(session)
        except ValueError as exc:
            raise ValueError("exact panel session is invalid") from exc
        if normalize(symbol) != symbol or parsed_session.isoformat() != session:
            raise ValueError("exact panel cell is not canonical")
        if cell in cells:
            raise ValueError("exact panel contains duplicate cells")
        cells.add(cell)
        symbols.add(symbol)
        sessions.add(session)
    ordered_cells = [
        f"{symbol}@{session}"
        for session in sorted(sessions)
        for symbol in sorted(symbols)
    ]
    if (
        len(symbols) != 12
        or len(sessions) != 3
        or len(ordered_cells) != 36
        or cells != set(ordered_cells)
    ):
        raise ValueError("exact panel does not cover the required 12 by 3 cells")
    return ordered_cells, len(symbols), len(sessions)


def _verify_research_unsigned_component(
    *,
    component: str,
    artifact: object,
    expected_panel: tuple[str, ...],
    decision_cutoffs: Mapping[str, str],
    receipt_bindings: Mapping[str, tuple[object, str, str, str]],
) -> None:
    """Apply signed-component record rules without an authority admission."""

    from stockdata.provider_authority_admission import (
        _component_payload,
        _panel_entry,
        _timestamp,
    )

    if not isinstance(artifact, Mapping) or set(artifact) != {
        "schema_version", "component", "panel", "records"
    }:
        raise ValueError(f"{component} component artifact schema is incomplete")
    if (
        artifact["schema_version"] != COMPONENT_SCHEMAS[component]
        or artifact["component"] != component
        or not isinstance(artifact["panel"], list)
        or tuple(artifact["panel"]) != expected_panel
        or len(artifact["panel"]) != len(set(artifact["panel"]))
    ):
        raise ValueError(f"{component} component differs from the exact panel")
    records = artifact["records"]
    if not isinstance(records, list) or len(records) != len(expected_panel):
        raise ValueError(f"{component} component records do not cover exact panel")
    observed_entries: list[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or set(record) != {
            "panel_entry",
            "payload",
            "record_sha256",
            "source_receipt_ids",
            "effective_at",
            "available_at",
        }:
            raise ValueError(f"{component} component record {index} is incomplete")
        entry = _panel_entry(record["panel_entry"])
        observed_entries.append(entry)
        payload = _component_payload(component, record["payload"], panel_entry=entry)
        record_sha256 = record["record_sha256"]
        if (
            not isinstance(record_sha256, str)
            or hashlib.sha256(_canonical(payload)).hexdigest() != record_sha256
        ):
            raise ValueError(f"{component} component record hash drifted")
        receipt_ids = record["source_receipt_ids"]
        if (
            not isinstance(receipt_ids, list)
            or not receipt_ids
            or receipt_ids != sorted(receipt_ids)
            or len(receipt_ids) != len(set(receipt_ids))
        ):
            raise ValueError(f"{component} component source receipts are invalid")
        normalized_receipts = set(receipt_ids)
        if any(receipt_id not in receipt_bindings for receipt_id in normalized_receipts):
            raise ValueError(f"{component} component uses an unbound source receipt")
        binding = (component, entry, record_sha256)
        if any(
            binding not in receipt_bindings[receipt_id][0]
            for receipt_id in normalized_receipts
        ):
            raise ValueError(f"{component} component receipt does not bind record")
        effective_at = datetime.fromisoformat(_timestamp(record["effective_at"], "effective_at"))
        available_at = max(
            datetime.fromisoformat(_timestamp(record["available_at"], "available_at")),
            *(
                datetime.fromisoformat(receipt_bindings[receipt_id][1])
                for receipt_id in normalized_receipts
            ),
        )
        cutoff = datetime.fromisoformat(decision_cutoffs[entry])
        if (
            entry not in expected_panel
            or effective_at.date().isoformat() != entry.split("@", 1)[1]
            or effective_at >= cutoff
            or available_at >= cutoff
        ):
            raise ValueError(f"{component} component timing is invalid")
        if component == "corporate_actions" and any(
            datetime.fromisoformat(
                _timestamp(event["announcement_at"], "announcement_at")
            ) >= cutoff
            for event in payload["events"]
        ):
            raise ValueError("corporate_actions announcement is post-cutoff")
    if tuple(observed_entries) != expected_panel:
        raise ValueError(f"{component} component records differ exact panel")


def _research_component_payloads(
    *,
    registration_body: Mapping[str, object],
    registration_raw: bytes,
    source_receipts: tuple[ProviderArtifactReference, ...],
    components: Mapping[str, ProviderArtifactReference],
    paths: Mapping[ProviderArtifactReference, _RetainedArtifact],
    database: ProviderArtifactReference,
    execution_adjustment: ProviderArtifactReference,
    signal_adjustment: ProviderArtifactReference,
    formal_panel: object,
) -> tuple[dict[str, object], frozenset[str], dict[str, Mapping[str, object]]]:
    """Prove complete canonical component closure from retained provider inputs."""

    from stockdata.adjustment_identity import verify_adjustment_identity
    from stockdata.component_availability import verify_component_availability_records
    from stockdata.provider_authority_admission import (
        validate_local_mechanical_prerequisites,
    )
    from stockdata.provider_intrinsic import (
        FORWARD_COMPONENTS,
        INTRINSIC_COMPONENTS,
        reconstruct_forward_component_evidence,
        reconstruct_intrinsic_evidence,
        verify_forward_component_evidence,
        verify_intrinsic_evidence,
    )

    _require_trusted_local_registration_inputs(
        registration_raw=registration_raw,
        source_receipts=source_receipts,
        components=components,
    )
    payloads = {
        component: _research_json(
            paths[components[component]].raw, f"component {component}"
        )
        for component in REQUIRED_COMPONENTS
    }
    complete: set[str] = set()
    for component in REQUIRED_COMPONENTS:
        reference = components[component]
        payload = payloads[component]
        if (
            not isinstance(payload, dict)
            or reference.schema_version != COMPONENT_SCHEMAS[component]
            or payload.get("schema_version") != reference.schema_version
            or hashlib.sha256(_canonical(payload)).hexdigest() != reference.identifier
        ):
            raise ValueError(f"component {component} identity has drifted")
        complete.add(component)
    if not isinstance(formal_panel, list):
        raise ValueError("exact panel is invalid")
    expected_panel = tuple(sorted(formal_panel))
    if len(source_receipts) != 2:
        raise ValueError("trusted-local registration requires exactly two source receipts")
    registration_receipt_bodies: dict[str, Mapping[str, object]] = {}
    for receipt in source_receipts:
        body = _research_json(paths[receipt].raw, "source receipt")
        if (
            not isinstance(body, Mapping)
            or hashlib.sha256(_canonical(body)).hexdigest() != receipt.identifier
        ):
            raise ValueError("source receipt identity has drifted")
        if receipt.identifier in registration_receipt_bodies:
            raise ValueError("trusted-local registration repeats a source receipt")
        registration_receipt_bodies[receipt.identifier] = body
    prerequisites = registration_body.get("prerequisites")
    if not isinstance(prerequisites, Mapping):
        raise ValueError("trusted-local registration prerequisites have drifted")
    try:
        local = validate_local_mechanical_prerequisites(
            calendar_artifact=payloads["trading_calendar"],
            market_rules_artifact=payloads["market_rules"],
            expected_panel=expected_panel,
            bound_source_receipts=registration_receipt_bodies,
        )
    except ValueError:
        selected_rules = payloads["market_rules"].get("records")
        if not isinstance(selected_rules, list) or len(selected_rules) != len(
            expected_panel
        ):
            raise
        local = {
            field: prerequisites.get(field)
            for field in ("trading_calendar", "market_rule_prerequisite")
        }
    if any(
        not isinstance(local.get(field), Mapping)
        or prerequisites.get(field) != local[field]
        for field in ("trading_calendar", "market_rule_prerequisite")
    ):
        raise ValueError("trusted-local registration prerequisites have drifted")
    execution_body = _research_json(
        paths[execution_adjustment].raw, "execution adjustment"
    )
    signal_body = _research_json(paths[signal_adjustment].raw, "signal adjustment")
    execution_verified = verify_adjustment_identity(
        execution_body, expected_price_role="execution"
    )
    signal_verified = verify_adjustment_identity(
        signal_body, expected_price_role="signal"
    )
    if (
        execution_verified.identifier != execution_adjustment.identifier
        or signal_verified.identifier != signal_adjustment.identifier
    ):
        raise ValueError("research adjustment identity has drifted")
    decision_cutoffs = local["trading_calendar"]["decision_cutoff_by_panel"]
    if not isinstance(decision_cutoffs, Mapping):
        raise ValueError("trusted-local decision cutoffs are invalid")
    reconstructed = reconstruct_intrinsic_evidence(
        paths[database].raw,
        panel=expected_panel,
        execution_adjustment=execution_verified,
        signal_adjustment=signal_verified,
        decision_cutoffs=decision_cutoffs,
    )
    collector_receipts = dict(reconstructed.source_receipts)
    forward = reconstruct_forward_component_evidence(
        paths[database].raw,
        panel=expected_panel,
        decision_cutoffs=decision_cutoffs,
    )
    for receipt_id, receipt in forward.source_receipts.items():
        existing = collector_receipts.get(receipt_id)
        if existing is not None and existing != receipt:
            raise ValueError("collector source receipt identity has drifted")
        collector_receipts[receipt_id] = receipt
    if set(registration_receipt_bodies) & set(collector_receipts):
        raise ValueError("registration and collector receipt domains overlap")

    verify_forward_component_evidence(
        forward,
        claimed_components={component: payloads[component] for component in FORWARD_COMPONENTS},
        component_references={component: components[component] for component in FORWARD_COMPONENTS},
        bound_source_receipts=collector_receipts,
    )
    intrinsic_receipts = dict(collector_receipts)
    intrinsic = verify_intrinsic_evidence(
        reconstructed,
        claimed_components={component: payloads[component] for component in INTRINSIC_COMPONENTS},
        component_references={component: components[component] for component in INTRINSIC_COMPONENTS},
        bound_source_receipts=intrinsic_receipts,
        database_sha256=database.identifier,
    )
    if any(intrinsic[component].get("ready") is not True for component in INTRINSIC_COMPONENTS):
        raise ValueError("intrinsic research component verification failed")
    component_records = {
        component: payloads[component]["records"]
        for component in REQUIRED_COMPONENTS
        if component != "availability_records"
        and isinstance(payloads[component], Mapping)
        and isinstance(payloads[component].get("records"), list)
    }
    availability = verify_component_availability_records(
        payloads["availability_records"],
        expected_panel_sha256=hashlib.sha256(_canonical(formal_panel)).hexdigest(),
        expected_panel_size=len(expected_panel),
        expected_decision_cutoffs=decision_cutoffs,
        bound_source_receipt_ids=sorted(
            {*registration_receipt_bodies, *collector_receipts}
        ),
        component_records=component_records,
        expected_signed_calendar_phases=local["trading_calendar"]["calendar_phases_by_panel"],
    )
    if availability.ready is not True:
        raise ValueError("availability research component verification failed")
    if availability.source_receipt_ids != tuple(
        sorted({*registration_receipt_bodies, *collector_receipts})
    ):
        raise ValueError("research source receipt closure is not bidirectional")
    if complete != set(REQUIRED_COMPONENTS):
        raise ValueError("research component closure is incomplete")
    return payloads, frozenset(complete), collector_receipts


def _resolve_research_inputs(
    *,
    bundle: Mapping[str, object],
    bundle_raw: bytes,
    paths: Mapping[ProviderArtifactReference, _RetainedArtifact],
    registration: ProviderArtifactReference,
    checkout: ProviderArtifactReference,
    database: ProviderArtifactReference,
    ledger: ProviderArtifactReference,
    continuity_closure: ProviderArtifactReference,
    exact_panel: ProviderArtifactReference,
    source_receipts: tuple[ProviderArtifactReference, ...],
    execution_adjustment: ProviderArtifactReference,
    signal_adjustment: ProviderArtifactReference,
    components: Mapping[str, ProviderArtifactReference],
    replay_policy_binding: object,
) -> dict[str, object]:
    """Derive one research-only projection while verified descriptors remain open."""

    registration_body = _research_json(
        paths[registration].raw, "trusted-local registration"
    )
    if (
        registration.schema_version != TRUSTED_LOCAL_REGISTRATION_SCHEMA
        or not isinstance(registration_body, dict)
    ):
        raise ValueError("research replay requires a trusted-local registration")
    registration_reference = {
        "schema_version": registration.schema_version,
        "sha256": registration.identifier,
        "authority_mode": registration_body.get("authority_mode"),
        "registered_at": registration_body.get("registered_at"),
        "outcome_feedback_used": registration_body.get("outcome_feedback_used"),
    }

    formal_panel = _research_json(paths[exact_panel].raw, "exact panel")
    if hashlib.sha256(_canonical(formal_panel)).hexdigest() != exact_panel.identifier:
        raise ValueError("exact panel identity has drifted")
    panel, symbol_count, session_count = _research_panel_projection(formal_panel)
    panel_sha256 = hashlib.sha256(_canonical(panel)).hexdigest()

    try:
        history = parse_collector_ledger(paths[ledger].opened)
    except CollectorContinuityError as exc:
        raise ValueError("collector schedule is invalid") from exc
    ordinals = [
        event["event"]["step_ordinal"]
        for event in history
        if event["event_type"] == "ATTEMPT_COMPLETED"
        and isinstance(event.get("event"), Mapping)
    ]
    if ordinals != list(range(12)):
        raise ValueError("collector schedule is incomplete or reordered")

    component_payloads, mechanically_complete, collector_receipts = _research_component_payloads(
        registration_body=registration_body,
        registration_raw=paths[registration].raw,
        source_receipts=source_receipts,
        components=components,
        paths=paths,
        database=database,
        execution_adjustment=execution_adjustment,
        signal_adjustment=signal_adjustment,
        formal_panel=formal_panel,
    )

    expected_bindings = {
        "registration_reference": registration_reference,
        "provider_checkout_reference": _research_reference(checkout),
        "provider_bundle_reference": {
            "schema_version": BUNDLE_SCHEMA,
            "sha256": hashlib.sha256(bundle_raw).hexdigest(),
        },
        "database_snapshot_reference": _research_reference(database),
        "ledger_snapshot_reference": _research_reference(ledger),
        "continuity_closure_reference": _research_reference(continuity_closure),
        "panel_reference": {
            "sha256": panel_sha256,
            "ordered_cells": panel,
            "symbol_count": symbol_count,
            "session_count": session_count,
            "panel_cell_count": len(panel),
        },
        "collector_schedule_reference": {
            "sha256": hashlib.sha256(_canonical(ordinals)).hexdigest(),
            "terminal_step_count": 12,
            "completed_step_ordinals": ordinals,
        },
        "source_receipt_references": sorted(
            [
                *(_research_reference(receipt) for receipt in source_receipts),
                *(
                    _research_receipt_reference(receipt_id, receipt)
                    for receipt_id, receipt in collector_receipts.items()
                ),
            ],
            key=lambda reference: (reference["schema_version"], reference["sha256"]),
        ),
        "adjustment_references": {
            "execution": _research_reference(execution_adjustment),
            "signal": _research_reference(signal_adjustment),
        },
        "component_references": {
            component: {
                "artifact_reference": _research_reference(components[component]),
                "mechanically_complete": component in mechanically_complete,
                "blockers": [],
            }
            for component in REQUIRED_COMPONENTS
        },
        "replay_policy_binding": replay_policy_binding,
    }
    try:
        from stockdata.trusted_local_research_replay_export import _bindings

        bindings = _bindings(expected_bindings)
        return json.loads(
            _canonical(
                {
                    "schema_version": RESOLVED_RESEARCH_INPUTS_SCHEMA,
                    "expected_bindings": bindings,
                    "component_payloads": component_payloads,
                }
            ).decode("ascii")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("research replay inputs are invalid") from exc


def _verify_provider_bundle_core(
    bundle: object,
    bundle_raw: bytes,
    retained_artifacts: list[_RetainedArtifact],
    *,
    replay_policy_binding: object = _NO_RESEARCH_POLICY,
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
        schema=REGISTRATION_SCHEMAS,
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
    if registration.schema_version == TRUSTED_LOCAL_REGISTRATION_SCHEMA:
        _require_trusted_local_registration_inputs(
            registration_raw=paths[registration].raw,
            source_receipts=source_receipts,
            components=components,
        )

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
    if registration.schema_version == TRUSTED_LOCAL_REGISTRATION_SCHEMA:
        _require_trusted_local_negative_readiness(report)

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
    receipt = {
        "schema_version": EXPORT_SCHEMA,
        "ready": ready,
        "contract": contract.to_dict(),
        "companion_snapshot": companion.to_dict(),
        "readiness_report": report,
    }
    if replay_policy_binding is _NO_RESEARCH_POLICY:
        return receipt
    resolved = _resolve_research_inputs(
        bundle=bundle,
        bundle_raw=bundle_raw,
        paths=paths,
        registration=registration,
        checkout=checkout,
        database=database,
        ledger=ledger,
        continuity_closure=continuity_closure,
        exact_panel=exact_panel,
        source_receipts=source_receipts,
        execution_adjustment=execution_adjustment,
        signal_adjustment=signal_adjustment,
        components=components,
        replay_policy_binding=replay_policy_binding,
    )
    for reference in paths:
        content_reader(reference)
    return resolved


def _collect_provider_bundle_verification(
    bundle: object,
    bundle_raw: bytes,
    *,
    replay_policy_binding: object = _NO_RESEARCH_POLICY,
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
        if replay_policy_binding is _NO_RESEARCH_POLICY:
            result = _verify_provider_bundle_core(bundle, bundle_raw, retained)
        else:
            result = _verify_provider_bundle_core(
                bundle,
                bundle_raw,
                retained,
                replay_policy_binding=replay_policy_binding,
            )
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


def resolve_trusted_local_research_replay_inputs(
    bundle_file: str | Path,
    *,
    replay_policy_binding: object,
) -> dict[str, object]:
    """Resolve one verified formal bundle into E1 research-only inputs."""

    try:
        supplied = os.path.expanduser(os.fspath(bundle_file))
        bundle_path = os.path.abspath(supplied)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider bundle path is invalid") from exc
    if supplied != bundle_path or os.path.basename(bundle_path) != "bundle.json":
        raise ValueError("research replay requires a formal absolute bundle.json")

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
            bundle,
            retained.raw,
            replay_policy_binding=replay_policy_binding,
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
        body_label="research replay resolution and cleanup failed",
        cleanup_label="research replay resolution cleanup failed",
    )
    if result is None:
        raise AssertionError("research replay resolution result is unavailable")
    return result


def _research_replay_policy_request(
    value: object,
) -> tuple[dict[str, object], bytes]:
    fields = (
        "schema_version",
        "replay_policy_binding",
        "shared_cash_policy_body",
        "risk_policy_body",
    )
    if not isinstance(value, Mapping):
        raise TypeError("research replay policy request is invalid")
    try:
        request_bytes = _canonical(value)
        request = json.loads(request_bytes)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TypeError("research replay policy request is not canonical JSON") from exc
    if not isinstance(request, dict) or set(request) != set(fields):
        raise TypeError("research replay policy request is invalid")
    if request["schema_version"] != RESEARCH_REPLAY_POLICY_REQUEST_SCHEMA:
        raise ValueError("research replay policy request schema is invalid")
    for field in fields[1:]:
        if request[field] is None or not isinstance(request[field], dict):
            raise TypeError("research replay policy request is incomplete")
    try:
        live_request = {field: value[field] for field in fields}
        if _canonical(live_request) != request_bytes:
            raise ValueError("research replay policy request drifted")
    except (KeyError, TypeError, ValueError) as exc:
        raise TypeError("research replay policy request is not stable") from exc
    return request, request_bytes


def run_trusted_local_research_replay_bridge(
    bundle_file: str | Path,
    *,
    policy_request: object,
) -> dict[str, object]:
    """Project one verified bundle into a fixed research-only envelope."""

    request, request_bytes = _research_replay_policy_request(policy_request)
    replay_policy_binding = request["replay_policy_binding"]
    shared_cash_policy_body = request["shared_cash_policy_body"]
    risk_policy_body = request["risk_policy_body"]
    resolved = resolve_trusted_local_research_replay_inputs(
        bundle_file,
        replay_policy_binding=replay_policy_binding,
    )
    if (
        not isinstance(resolved, Mapping)
        or set(resolved) != {"schema_version", "expected_bindings", "component_payloads"}
        or resolved.get("schema_version") != RESOLVED_RESEARCH_INPUTS_SCHEMA
        or not isinstance(resolved.get("expected_bindings"), Mapping)
        or not isinstance(resolved.get("component_payloads"), Mapping)
    ):
        raise ValueError("research replay resolution output is invalid")
    expected_bindings = resolved["expected_bindings"]
    component_payloads = resolved["component_payloads"]
    expected_bindings_bytes = _canonical(expected_bindings)
    component_payloads_bytes = _canonical(component_payloads)
    exported = build_trusted_local_research_replay_export(
        expected_bindings=expected_bindings
    )
    verify_trusted_local_research_replay_export(
        exported, expected_bindings=expected_bindings
    )
    if (
        _canonical(request) != request_bytes
        or _canonical(expected_bindings) != expected_bindings_bytes
        or _canonical(component_payloads) != component_payloads_bytes
    ):
        raise ValueError("research replay bridge input drifted")
    materialization = build_trusted_local_research_replay_materialization(
        provider_export=exported,
        expected_bindings=expected_bindings,
        component_payloads=component_payloads,
        shared_cash_policy_body=shared_cash_policy_body,
        risk_policy_body=risk_policy_body,
    )
    verify_trusted_local_research_replay_materialization(
        materialization,
        provider_export=exported,
        expected_bindings=expected_bindings,
    )
    if (
        _canonical(request) != request_bytes
        or _canonical(expected_bindings) != expected_bindings_bytes
        or _canonical(component_payloads) != component_payloads_bytes
    ):
        raise ValueError("research replay bridge input drifted")
    envelope = {
        "schema_version": RESEARCH_REPLAY_ENVELOPE_SCHEMA,
        "provider_export": exported,
        "provider_expected_bindings": expected_bindings,
        "provider_materialization": materialization,
    }
    _canonical(envelope)
    return envelope
