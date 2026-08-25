from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest
import stockdata.collector_continuity as continuity
import stockdata.future_panel_registration as future_registration
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stockdata.adjustment_identity import verify_adjustment_identity
from stockdata.authority import (
    AUTHORITY_ENVELOPE_SCHEMA,
    SIGNER_ENROLLMENT_SCHEMA,
    TRUST_REGISTRY_SCHEMA,
    load_enrolled_trust_registry,
    load_provider_trust_registry,
)
from stockdata.companion_snapshot import build_companion_snapshot
from stockdata.component_availability import (
    VERIFIED_AVAILABILITY_RECORDS_SCHEMA,
    verify_component_availability_records,
)
from stockdata.forward_context import (
    SOURCE as CONTEXT_SOURCE,
    CapturedMarketRows,
    _COUNT_URL,
    _PAGE_SIZE,
    _PAGE_URL,
    capture_forward_context,
)
from stockdata.market_rules import MARKET_RULE_PAYLOAD_SCHEMA, _symbol_board
from stockdata.provider_authority_admission import (
    SIGNED_COMPONENTS,
    SOURCE_RECEIPT_SCHEMA,
    admit_signed_component_authority,
)
from stockdata.provider_export import export_verified_provider_receipt
from stockdata.provider_intrinsic import reconstruct_intrinsic_evidence
from stockdata.provider_materializer import materialize_provider_bundle
from stockdata.rqgm_provider_contract import (
    COMPONENT_SCHEMAS,
    EXECUTION_ADJUSTMENT_SCHEMA,
    REQUIRED_COMPONENTS,
    ProviderArtifactReference,
    SOURCE_RECEIPT_SCHEMA as CONTRACT_SOURCE_RECEIPT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
)
from test_collector_phase_orchestration import _append_completed_attempt
from test_collector_step_state import (
    _SESSIONS as COLLECTOR_SESSIONS,
    _SYMBOLS as COLLECTOR_SYMBOLS,
    _bound_registration,
    _schedule,
)


DAY = COLLECTOR_SESSIONS[0]
NEXT_SESSION = COLLECTOR_SESSIONS[1]
PANEL = sorted(
    f"{symbol}@{session}"
    for symbol in COLLECTOR_SYMBOLS
    for session in COLLECTOR_SESSIONS
)
DECISION_CUTOFF = f"{DAY}T09:25:00+08:00"
NEXT_DECISION_CUTOFF = f"{NEXT_SESSION}T09:25:00+08:00"
SIGNED = tuple(sorted(SIGNED_COMPONENTS))


def _next_session(day: str) -> str:
    try:
        return COLLECTOR_SESSIONS[COLLECTOR_SESSIONS.index(day) + 1]
    except (ValueError, IndexError):
        return (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()


def _decision_cutoff(panel_entry: str) -> str:
    return f"{panel_entry.rsplit('@', 1)[1]}T09:25:00+08:00"


def _session_close(panel_entry: str) -> str:
    return f"{panel_entry.rsplit('@', 1)[1]}T15:00:00+08:00"


def _next_decision_cutoff(panel_entry: str) -> str:
    day = panel_entry.rsplit("@", 1)[1]
    return f"{_next_session(day)}T09:25:00+08:00"


DECISION_CUTOFFS = {entry: _decision_cutoff(entry) for entry in PANEL}
SIGNED_CALENDAR_PHASES = {
    entry: {
        "decision_cutoff_at": _decision_cutoff(entry),
        "session_close_at": _session_close(entry),
        "next_session_decision_cutoff_at": _next_decision_cutoff(entry),
    }
    for entry in PANEL
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _write(path: Path, value: object) -> Path:
    path.write_bytes(_canonical(value))
    return path


def _public(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _key_id(private_key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_public(private_key)).hexdigest()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _ephemeral_registry(
    directory: Path,
    *,
    roles: tuple[str, ...] = SIGNED,
    self_signed: bool = False,
):
    root = Ed25519PrivateKey.generate()
    signer = root if self_signed else Ed25519PrivateKey.generate()
    authorization = {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "publisher_key_id": _key_id(signer),
        "publisher_public_key_base64": _b64(_public(signer)),
        "trust_root_id": _key_id(root),
        "component_roles": list(roles),
        "valid_from": "2025-01-01T00:00:00+00:00",
        "valid_until": "2100-01-01T00:00:00+00:00",
    }
    registry_value = {
        "schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "trust_roots": [
            {
                "trust_root_id": _key_id(root),
                "public_key_base64": _b64(_public(root)),
            }
        ],
        "signer_enrollments": [
            {
                "publisher_key_id": _key_id(signer),
                "trust_root_id": _key_id(root),
                "public_key_base64": _b64(_public(signer)),
                "component_roles": list(roles),
                "valid_from": authorization["valid_from"],
                "valid_until": authorization["valid_until"],
                "authorization_signature_base64": _b64(
                    root.sign(_canonical(authorization))
                ),
            }
        ],
    }
    raw = _canonical(registry_value)
    path = directory / f"registry-{hashlib.sha256(raw).hexdigest()}.json"
    path.write_bytes(raw)
    registry = load_enrolled_trust_registry(
        path, expected_sha256=hashlib.sha256(raw).hexdigest()
    )
    return registry, root, signer


def _adjustment(role: str, mode: str, version: str) -> dict[str, object]:
    return {
        "schema_version": (
            EXECUTION_ADJUSTMENT_SCHEMA
            if role == "execution"
            else SIGNAL_ADJUSTMENT_SCHEMA
        ),
        "price_role": role,
        "source": "tencent",
        "adjustment_mode": mode,
        "adjustment_version": version,
    }


def _context_rows() -> list[dict[str, object]]:
    rows = [
        {
            "symbol": f"{'sz' if symbol.endswith('.SZ') else 'sh'}{symbol[:6]}",
            "name": f"Fixture {symbol}",
            "trade": "10.0",
            "volume": 1000,
        }
        for symbol in COLLECTOR_SYMBOLS
    ]
    return rows


def _append_provider_attempt(
    prepared: dict[str, object], lease: object, spec: object
) -> None:
    from test_collector_recovery import _tencent_capture, _writer_cache
    import stockdata.cache as cache_module
    import stockdata.sync as sync_module

    if spec.step_id == "pre_open_corporate_actions":
        _append_completed_attempt(lease, spec)
        return
    started_at = (
        f"{spec.session}T08:35:00+08:00"
        if spec.phase == "pre_open"
        else f"{spec.session}T15:01:00+08:00"
    )
    finished_at = (
        f"{spec.session}T09:20:00+08:00"
        if spec.phase == "pre_open"
        else f"{spec.session}T16:20:00+08:00"
    )
    launch = continuity._begin_collector_step_attempt(
        lease, spec, now=lambda: started_at
    )
    with pytest.MonkeyPatch.context() as writer_patch, _writer_cache(
        prepared, spec, lease, launch
    ) as cache:
        if spec.step_id in {"pre_open_context", "post_close_context"}:
            observed_at = (
                f"{spec.session}T08:40:00+08:00"
                if spec.phase == "pre_open"
                else f"{spec.session}T15:05:00+08:00"
            )
            rows = _context_rows()
            receipt = {
                "observed_at": observed_at,
                "source": CONTEXT_SOURCE,
                "request": {
                    "count_url": _COUNT_URL,
                    "page_url": _PAGE_URL,
                    "node": "hs_a",
                    "page_size": _PAGE_SIZE,
                },
                "response": {
                    "advertised_count": len(rows),
                    "count_raw": str(len(rows)),
                    "raw_pages": [json.dumps(rows)],
                },
            }
            writer_patch.setattr(cache_module, "_utc_now", lambda: observed_at)
            capture_forward_context(
                cache,
                spec.session,
                fetcher=lambda: CapturedMarketRows(rows, receipt),
                now=datetime.fromisoformat(observed_at),
            )
        else:
            observed_at = f"{spec.session}T16:00:00+08:00"
            writer_patch.setattr(cache_module, "_utc_now", lambda: observed_at)
            writer_patch.setattr(
                sync_module, "default_final_date", lambda: spec.session
            )
            writer_patch.setattr(
                sync_module, "latest_finalized_date", lambda: spec.session
            )
            sync_module.sync_symbols(
                cache,
                COLLECTOR_SYMBOLS,
                COLLECTOR_SESSIONS[0],
                spec.session,
                source="tencent",
                adjustment_mode="raw",
                adjustment_version="tencent-qt-daily-v1",
                fetcher=lambda code, start, end: _tencent_capture(
                    code,
                    end,
                    observed_at=observed_at,
                    start_date=start,
                ),
            )
    with continuity.open_registered_collector_read_connection(spec) as token:
        raw = continuity.verify_collector_raw_postcondition(
            token,
            spec,
            launch.baseline,
            attempt_started_at=started_at,
            attempt_finished_at=finished_at,
        )
    assert raw.raw_class == "complete"
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    process = continuity._CollectorProcessResult(
        True, 0, empty_sha256, 0, empty_sha256, 0, False
    )
    event_type, detail = continuity._terminal_attempt_event(
        launch,
        raw,
        process,
        process_launch_state="handle_obtained",
        finished_at=finished_at,
        failure_classification=None,
    )
    continuity._append_terminal_once(lease, launch, event_type=event_type, event=detail)


def _calendar_payload(panel_entry: str, *, complete: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision_cutoff_at": _decision_cutoff(panel_entry),
        "is_trading_day": True,
    }
    if complete:
        payload.update(
            {
                "session_close_at": _session_close(panel_entry),
                "next_session_decision_cutoff_at": _next_decision_cutoff(
                    panel_entry
                ),
            }
        )
    return payload


def _external_payload(
    component: str,
    *,
    panel_entry: str,
    response_sha256: str,
    complete_calendar: bool,
    corporate_action_announcement_at: str | None = None,
) -> dict[str, object]:
    symbol, day = panel_entry.split("@", 1)
    if component == "trading_calendar":
        return _calendar_payload(panel_entry, complete=complete_calendar)
    if component == "universe":
        return {"is_member": True, "universe_id": hashlib.sha256(b"u").hexdigest()}
    if component == "instrument_status":
        return {"is_st": False, "is_suspended": False, "listing_status": "listed"}
    if component == "corporate_actions":
        announcement_at = corporate_action_announcement_at or (
            f"{day}T08:00:00+08:00"
        )
        return {
            "events": [
                {
                    "announcement_at": announcement_at,
                    "effective_date": day,
                    "event_id": hashlib.sha256(
                        b"fixture-corporate-action"
                    ).hexdigest(),
                    "event_type": "cash_dividend",
                }
            ]
        }
    board = _symbol_board(symbol)
    return {
        "schema_version": MARKET_RULE_PAYLOAD_SCHEMA,
        "policy_id": (
            f"cn-a-share-{board.lower()}-{symbol[-2:].lower()}-{day}-v1"
        ),
        "source": "fixture-market-rules",
        "source_sha256": response_sha256,
        "security_type": "A_SHARE",
        "board": board,
        "exchange": symbol[-2:],
        "effective_from": day,
        "effective_until": day,
        "listing_age_min": 0,
        "listing_age_max": None,
        "is_st": False,
        "lot_size": 100,
        "t_plus_one": True,
        "reject_suspended": True,
        "reject_zero_volume": True,
        "price_limit_up": 0.10,
        "price_limit_down": 0.10,
        "price_limit_reference": "RECORD_OR_PREVIOUS_CLOSE",
        "price_tick": 0.01,
        "price_rounding": "HALF_UP",
        "locked_limit_order_policy": "REJECT_SIDE",
        "commission_rate": 0.0003,
        "minimum_commission": 5.0,
        "transfer_fee_rate": 0.0,
        "stamp_duty_sell_rate": 0.0005,
        "slippage_model": "OPEN_BPS",
        "slippage_bps": 2.0,
        "slippage_bounds": "BAR_AND_PRICE_LIMITS",
        "time_in_force": "DAY",
        "cancel_unfilled_at_close": True,
    }


def _external_authority(
    component: str,
    *,
    complete_calendar: bool,
    registry,
    root: Ed25519PrivateKey,
    signer: Ed25519PrivateKey,
    corporate_action_announcement_at: str | None = None,
    record_available_at: str | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source = f"fixture-{component.replace('_', '-')}"
    response_sha256 = hashlib.sha256(f"{component}-response".encode()).hexdigest()
    records = []
    for panel_entry in PANEL:
        day = panel_entry.rsplit("@", 1)[1]
        payload = _external_payload(
            component,
            panel_entry=panel_entry,
            response_sha256=response_sha256,
            complete_calendar=complete_calendar,
            corporate_action_announcement_at=corporate_action_announcement_at,
        )
        records.append(
            {
                "panel_entry": panel_entry,
                "payload": payload,
                "record_sha256": _sha256(payload),
                "source_receipt_ids": [],
                "effective_at": f"{day}T00:00:00+08:00",
                "available_at": record_available_at or f"{day}T08:00:00+08:00",
            }
        )
    receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": "fixture-market-rules" if component == "market_rules" else source,
        "observed_at": f"{DAY}T08:00:00+08:00",
        "response_sha256": response_sha256,
        "bindings": [
            {
                "component": component,
                "panel_entry": record["panel_entry"],
                "record_sha256": record["record_sha256"],
            }
            for record in records
        ],
    }
    receipt_id = _sha256(receipt)
    for record in records:
        record["source_receipt_ids"] = [receipt_id]
    artifact = {
        "schema_version": COMPONENT_SCHEMAS[component],
        "component": component,
        "panel": PANEL,
        "records": records,
    }
    artifact_reference = {
        "kind": f"stock-data-{component.replace('_', '-')}",
        "identifier": _sha256(artifact),
        "schema_version": COMPONENT_SCHEMAS[component],
    }
    envelope_payload = {
        "component_role": component,
        "artifact": artifact_reference,
        "source_receipt_ids": [receipt_id],
        "effective_at": f"{DAY}T00:00:00+08:00",
        "available_at": record_available_at or f"{DAY}T08:00:00+08:00",
        "publisher_key_id": _key_id(signer),
        "trust_root_id": _key_id(root),
        "trust_registry_sha256": registry.registry_sha256,
    }
    envelope = {
        "schema_version": AUTHORITY_ENVELOPE_SCHEMA,
        "algorithm": "ed25519",
        "payload": envelope_payload,
        "signature_base64": _b64(signer.sign(_canonical(envelope_payload))),
    }
    return artifact, receipt, envelope


def _availability(
    components: dict[str, dict[str, object]], receipt_ids: list[str]
) -> dict[str, object]:
    del receipt_ids  # Component records, not a caller summary, define the closure.
    rows = []
    for component in REQUIRED_COMPONENTS:
        if component == "availability_records":
            continue
        for record in components[component]["records"]:
            is_price = component in {"execution_prices", "signal_prices"}
            rows.append(
                {
                    "component": component,
                    "panel_entry": record["panel_entry"],
                    "record_sha256": record["record_sha256"],
                    "source_receipt_ids": record["source_receipt_ids"],
                    "event_at": record["effective_at"],
                    "available_at": record["available_at"],
                    "cutoff_kind": (
                        "next_session_decision_cutoff_at"
                        if is_price
                        else "decision_cutoff_at"
                    ),
                    "applicable_cutoff_at": (
                        _next_decision_cutoff(record["panel_entry"])
                        if is_price
                        else _decision_cutoff(record["panel_entry"])
                    ),
                }
            )
    return {
        "schema_version": VERIFIED_AVAILABILITY_RECORDS_SCHEMA,
        "panel": PANEL,
        "records": sorted(rows, key=lambda row: (row["component"], row["panel_entry"])),
    }


@dataclass(frozen=True)
class ProviderFixture:
    root: Path
    registry: object
    root_key: Ed25519PrivateKey
    signer_key: Ed25519PrivateKey
    database: Path
    registration_file: Path
    panel_file: Path
    execution_adjustment_file: Path
    signal_adjustment_file: Path
    receipt_files: tuple[Path, ...]
    authority_receipt_files: tuple[Path, ...]
    receipt_values: dict[str, dict[str, object]]
    component_files: dict[str, Path]
    components: dict[str, dict[str, object]]
    authority_files: dict[str, Path]
    authorities: dict[str, dict[str, object]]


def _fixture(tmp_path: Path, *, complete_calendar: bool) -> ProviderFixture:
    root = tmp_path / ("complete" if complete_calendar else "characterization")
    root.mkdir()
    registry, root_key, signer_key = _ephemeral_registry(root)
    panel_file = _write(root / "panel.json", PANEL)
    database = root / "provider.sqlite"
    future_registration.prepare_future_collector_database(
        database_file=database,
        panel_file=panel_file,
    )
    registration_file = _bound_registration(database)
    ledger = continuity.default_collector_ledger_path(database)
    prepared = {
        "database": database,
        "ledger": ledger,
        "registration": registration_file,
    }
    with continuity.acquire_collector_phase_lease(ledger) as lease:
        for spec in _schedule(database):
            _append_provider_attempt(prepared, lease, spec)
    execution_value = _adjustment("execution", "raw", "tencent-qt-daily-v1")
    signal_value = _adjustment("signal", "raw", "tencent-qt-daily-v1")
    execution = verify_adjustment_identity(execution_value, expected_price_role="execution")
    signal = verify_adjustment_identity(signal_value, expected_price_role="signal")
    reconstructed = reconstruct_intrinsic_evidence(
        database,
        panel=PANEL,
        execution_adjustment=execution,
        signal_adjustment=signal,
        decision_cutoffs=DECISION_CUTOFFS,
    )

    components = {
        name: dict(value) for name, value in reconstructed.components.items()
    }
    receipt_values = {
        receipt_id: dict(value)
        for receipt_id, value in reconstructed.source_receipts.items()
    }
    authority_receipt_ids: set[str] = set()
    authorities: dict[str, dict[str, object]] = {}
    for component in SIGNED:
        artifact, receipt, envelope = _external_authority(
            component,
            complete_calendar=complete_calendar,
            registry=registry,
            root=root_key,
            signer=signer_key,
        )
        components[component] = artifact
        receipt_id = _sha256(receipt)
        receipt_values[receipt_id] = receipt
        authority_receipt_ids.add(receipt_id)
        authorities[component] = envelope
    components["availability_records"] = _availability(
        components, sorted(receipt_values)
    )

    execution_file = _write(root / "execution-adjustment.json", execution_value)
    signal_file = _write(root / "signal-adjustment.json", signal_value)
    receipt_files = tuple(
        _write(root / f"receipt-{receipt_id}.json", receipt)
        for receipt_id, receipt in sorted(receipt_values.items())
    )
    authority_receipt_files = tuple(
        root / f"receipt-{receipt_id}.json" for receipt_id in sorted(authority_receipt_ids)
    )
    component_files = {
        component: _write(root / f"component-{component}.json", components[component])
        for component in REQUIRED_COMPONENTS
    }
    authority_files = {
        component: _write(root / f"authority-{component}.json", authorities[component])
        for component in SIGNED
    }
    return ProviderFixture(
        root=root,
        registry=registry,
        root_key=root_key,
        signer_key=signer_key,
        database=database,
        registration_file=registration_file,
        panel_file=panel_file,
        execution_adjustment_file=execution_file,
        signal_adjustment_file=signal_file,
        receipt_files=receipt_files,
        authority_receipt_files=authority_receipt_files,
        receipt_values=receipt_values,
        component_files=component_files,
        components=components,
        authority_files=authority_files,
        authorities=authorities,
    )


def _patch_test_trust(monkeypatch: pytest.MonkeyPatch, registry: object) -> None:
    for module in (
        "stockdata.provider_materializer",
        "stockdata.rqgm_provider_contract",
        "stockdata.companion_snapshot",
    ):
        monkeypatch.setattr(f"{module}.load_provider_trust_registry", lambda: registry)
    monkeypatch.setattr(
        "stockdata.companion_snapshot.PROVIDER_TRUST_REGISTRY_SHA256",
        registry.registry_sha256,
    )


def _materialize(
    fixture: ProviderFixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    authority_files: dict[str, Path] | None = None,
    include_intrinsic_receipts: bool = True,
) -> dict[str, object]:
    _patch_test_trust(monkeypatch, fixture.registry)
    snapshot_staging = fixture.root / f"{name}-snapshot-staging"
    snapshot_staging.mkdir(mode=0o700)
    return materialize_provider_bundle(
        output_dir=fixture.root / name,
        database_file=fixture.database,
        registration_file=fixture.registration_file,
        snapshot_staging_directory=snapshot_staging,
        panel_file=fixture.panel_file,
        source_receipt_files=(
            fixture.receipt_files
            if include_intrinsic_receipts
            else fixture.authority_receipt_files
        ),
        execution_adjustment_file=fixture.execution_adjustment_file,
        signal_adjustment_file=fixture.signal_adjustment_file,
        component_files=fixture.component_files,
        component_authority_files=(
            fixture.authority_files if authority_files is None else authority_files
        ),
        source="tencent",
    )


def _verify_availability(
    fixture: ProviderFixture,
    *,
    components: dict[str, dict[str, object]] | None = None,
    availability: dict[str, object] | None = None,
):
    values = fixture.components if components is None else components
    artifact = values["availability_records"] if availability is None else availability
    return verify_component_availability_records(
        artifact,
        expected_panel_sha256=_sha256(PANEL),
        expected_panel_size=len(PANEL),
        expected_decision_cutoffs=DECISION_CUTOFFS,
        bound_source_receipt_ids=sorted(fixture.receipt_values),
        component_records={
            component: values[component]["records"]
            for component in REQUIRED_COMPONENTS
            if component != "availability_records"
        },
        expected_signed_calendar_phases=SIGNED_CALENDAR_PHASES,
    )


def _bundle_locator(bundle_file: Path, field: str, component: str | None = None) -> Path:
    bundle = json.loads(bundle_file.read_text())
    locator = bundle[field] if component is None else bundle[field][component]
    return Path(locator["path"])


def _rewrite_export_component(
    bundle_file: Path,
    *,
    component: str,
    signer: Ed25519PrivateKey,
    mutate_payload,
) -> None:
    bundle = json.loads(bundle_file.read_text())
    component_locator = bundle["components"][component]
    component_path = Path(component_locator["path"])
    artifact = json.loads(component_path.read_text())
    old_receipt_id = artifact["records"][0]["source_receipt_ids"][0]
    for record in artifact["records"]:
        mutate_payload(record["payload"], record)
        record["record_sha256"] = _sha256(record["payload"])

    receipt_locator = next(
        locator
        for locator in bundle["source_receipts"]
        if locator["reference"]["identifier"] == old_receipt_id
    )
    receipt_path = Path(receipt_locator["path"])
    receipt = json.loads(receipt_path.read_text())
    for binding, record in zip(receipt["bindings"], artifact["records"]):
        if (
            binding["component"] == component
            and binding["panel_entry"] == record["panel_entry"]
        ):
            binding["record_sha256"] = record["record_sha256"]
    new_receipt_id = _sha256(receipt)
    for record in artifact["records"]:
        record["source_receipt_ids"] = [new_receipt_id]

    component_path.write_bytes(_canonical(artifact))
    component_reference = ProviderArtifactReference(
        f"stock-data-{component.replace('_', '-')}",
        hashlib.sha256(_canonical(artifact)).hexdigest(),
        COMPONENT_SCHEMAS[component],
    ).to_dict()
    component_locator["reference"] = component_reference
    receipt_path.write_bytes(_canonical(receipt))
    receipt_locator["reference"] = ProviderArtifactReference(
        "stock-data-source-receipt",
        new_receipt_id,
        CONTRACT_SOURCE_RECEIPT_SCHEMA,
    ).to_dict()

    report_path = _bundle_locator(bundle_file, "readiness_report")
    report = json.loads(report_path.read_text())
    evidence = report["components"][component]
    evidence["artifact"] = component_reference
    evidence["source_receipt_ids"] = [new_receipt_id]
    envelope = deepcopy(evidence["authority_envelope"])
    envelope["payload"]["artifact"] = component_reference
    envelope["payload"]["source_receipt_ids"] = [new_receipt_id]
    envelope["signature_base64"] = _b64(
        signer.sign(_canonical(envelope["payload"]))
    )
    evidence["authority_envelope"] = envelope
    evidence["signature_id"] = hashlib.sha256(
        base64.b64decode(envelope["signature_base64"])
    ).hexdigest()
    if component == "market_rules":
        evidence["execution_rule_selection"]["rulebook_artifact_sha256"] = (
            component_reference["identifier"]
        )

    availability_locator = bundle["components"]["availability_records"]
    availability_path = Path(availability_locator["path"])
    availability = json.loads(availability_path.read_text())
    for row in availability["records"]:
        if row["component"] == component:
            record = next(
                item
                for item in artifact["records"]
                if item["panel_entry"] == row["panel_entry"]
            )
            row["record_sha256"] = record["record_sha256"]
            row["source_receipt_ids"] = [new_receipt_id]
            row["event_at"] = record["effective_at"]
            row["available_at"] = record["available_at"]
    availability_reference = ProviderArtifactReference(
        "stock-data-availability-records",
        hashlib.sha256(_canonical(availability)).hexdigest(),
        COMPONENT_SCHEMAS["availability_records"],
    ).to_dict()
    availability_path.write_bytes(_canonical(availability))
    availability_locator["reference"] = availability_reference
    availability_evidence = report["components"]["availability_records"]
    availability_evidence["artifact"] = availability_reference
    availability_evidence["source_receipt_ids"] = [
        new_receipt_id
        if value == old_receipt_id
        else value
        for value in availability_evidence["source_receipt_ids"]
    ]
    availability_payload = {
        "verifier_schema": "stockdata-provider-availability-verifier/1",
        "artifact": availability_reference,
        "source_receipt_ids": availability_evidence["source_receipt_ids"],
        "coverage_count": availability_evidence["coverage_count"],
        "panel_sha256": availability_evidence["panel_sha256"],
    }
    availability_evidence["evidence_sha256"] = hashlib.sha256(
        _canonical(availability_payload)
    ).hexdigest()

    def reference(field: str) -> ProviderArtifactReference:
        return ProviderArtifactReference.from_dict(bundle[field]["reference"])

    component_references = {
        name: ProviderArtifactReference.from_dict(
            bundle["components"][name]["reference"]
        )
        for name in REQUIRED_COMPONENTS
    }
    snapshot = build_companion_snapshot(
        coverage_start=bundle["coverage_start"],
        coverage_end=bundle["coverage_end"],
        checkout=reference("checkout"),
        database=reference("database"),
        source_receipts=[
            ProviderArtifactReference.from_dict(item["reference"])
            for item in bundle["source_receipts"]
        ],
        execution_adjustment_identity=reference("execution_adjustment_identity"),
        signal_adjustment_identity=reference("signal_adjustment_identity"),
        exact_panel=reference("exact_panel"),
        components=component_references,
    )
    report["request"]["companion_snapshot_sha256"] = snapshot.snapshot_sha256
    report_raw = _canonical(report)
    report_path.write_bytes(report_raw)
    bundle["readiness_report"]["reference"]["identifier"] = hashlib.sha256(
        report_raw
    ).hexdigest()
    bundle_file.write_bytes(_canonical(bundle))


def _rewrite_export_report(bundle_file: Path, mutate_report) -> None:
    bundle = json.loads(bundle_file.read_text())
    report_path = _bundle_locator(bundle_file, "readiness_report")
    report = json.loads(report_path.read_text())
    mutate_report(report)
    report_raw = _canonical(report)
    report_path.write_bytes(report_raw)
    bundle["readiness_report"]["reference"]["identifier"] = hashlib.sha256(
        report_raw
    ).hexdigest()
    bundle_file.write_bytes(_canonical(bundle))


def test_current_path_cannot_emit_ready_with_all_five_signed_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)

    result = _materialize(
        fixture,
        monkeypatch,
        name="blocked-bundle",
        include_intrinsic_receipts=False,
    )

    report = result["receipt"]["readiness_report"]
    assert result["receipt"]["ready"] is False
    assert all(report["components"][component]["ready"] for component in SIGNED)
    assert all(
        report["components"][component]["ready"] is False
        for component in ("execution_prices", "signal_prices", "decision_context")
    )
    assert report["components"]["availability_records"]["ready"] is False
    assert report["components"]["availability_records"]["blockers"] == [
        {"code": "availability_depends_on_unready_component", "count": 1}
    ]


def test_complete_nine_component_fixture_materializes_and_reexports_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    verified = _verify_availability(fixture)
    assert verified.ready is True
    assert verified.record_count == 8 * len(PANEL)

    materialized = _materialize(fixture, monkeypatch, name="ready-bundle")
    bundle_file = Path(materialized["bundle_file"])
    first_export = export_verified_provider_receipt(bundle_file)
    second_export = export_verified_provider_receipt(bundle_file)

    assert materialized["receipt"]["ready"] is True
    assert first_export["ready"] is True
    assert second_export["ready"] is True
    assert second_export == first_export == materialized["receipt"]


@pytest.mark.parametrize(
    "announcement_at",
    [DECISION_CUTOFF, f"{DAY}T09:25:01+08:00"],
)
def test_signed_corporate_action_announcement_at_cutoff_is_rejected(
    tmp_path: Path, announcement_at: str
) -> None:
    registry, root, signer = _ephemeral_registry(tmp_path, roles=("corporate_actions",))
    artifact, receipt, envelope = _external_authority(
        "corporate_actions",
        complete_calendar=True,
        registry=registry,
        root=root,
        signer=signer,
        corporate_action_announcement_at=announcement_at,
    )
    receipt_id = _sha256(receipt)

    assert artifact["records"][0]["effective_at"] == f"{DAY}T00:00:00+08:00"
    with pytest.raises(ValueError, match="announcement is post-cutoff"):
        admit_signed_component_authority(
            component="corporate_actions",
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=PANEL,
            bound_source_receipts={receipt_id: receipt},
            registry=registry,
            decision_cutoff_by_panel=DECISION_CUTOFFS,
        )


def test_signed_corporate_action_record_available_at_cutoff_is_rejected(
    tmp_path: Path,
) -> None:
    registry, root, signer = _ephemeral_registry(tmp_path, roles=("corporate_actions",))
    artifact, receipt, envelope = _external_authority(
        "corporate_actions",
        complete_calendar=True,
        registry=registry,
        root=root,
        signer=signer,
        record_available_at=DECISION_CUTOFF,
    )
    receipt_id = _sha256(receipt)

    with pytest.raises(ValueError, match="post-cutoff"):
        admit_signed_component_authority(
            component="corporate_actions",
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=PANEL,
            bound_source_receipts={receipt_id: receipt},
            registry=registry,
            decision_cutoff_by_panel=DECISION_CUTOFFS,
        )


@pytest.mark.parametrize("self_signed", [False, True])
def test_ephemeral_or_self_signed_key_never_enters_production_trust(
    tmp_path: Path, self_signed: bool
) -> None:
    directory = tmp_path / f"trust-{self_signed}"
    directory.mkdir()
    registry, root, signer = _ephemeral_registry(
        directory, roles=("universe",), self_signed=self_signed
    )
    artifact, receipt, envelope = _external_authority(
        "universe",
        complete_calendar=True,
        registry=registry,
        root=root,
        signer=signer,
    )
    receipt_id = _sha256(receipt)
    admitted = admit_signed_component_authority(
        component="universe",
        artifact_value=artifact,
        authority_envelope=envelope,
        expected_panel=PANEL,
        bound_source_receipts={receipt_id: receipt},
        registry=registry,
        decision_cutoff_by_panel=DECISION_CUTOFFS,
    )
    assert admitted.publisher_key_id == _key_id(signer)

    with pytest.raises(ValueError):
        admit_signed_component_authority(
            component="universe",
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=PANEL,
            bound_source_receipts={receipt_id: receipt},
            registry=load_provider_trust_registry(),
            decision_cutoff_by_panel=DECISION_CUTOFFS,
        )


def test_unknown_signer_is_rejected_by_ephemeral_registry(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    artifact = fixture.components["universe"]
    receipt_id = artifact["records"][0]["source_receipt_ids"][0]
    envelope = deepcopy(fixture.authorities["universe"])
    unknown = Ed25519PrivateKey.generate()
    envelope["payload"]["publisher_key_id"] = _key_id(unknown)
    envelope["signature_base64"] = _b64(unknown.sign(_canonical(envelope["payload"])))

    with pytest.raises(ValueError, match="unknown signer"):
        admit_signed_component_authority(
            component="universe",
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=PANEL,
            bound_source_receipts={receipt_id: fixture.receipt_values[receipt_id]},
            registry=fixture.registry,
            decision_cutoff_by_panel=DECISION_CUTOFFS,
        )


def test_signed_component_byte_drift_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    artifact = deepcopy(fixture.components["universe"])
    receipt_id = artifact["records"][0]["source_receipt_ids"][0]
    artifact["records"][0]["payload"]["is_member"] = False
    artifact["records"][0]["record_sha256"] = _sha256(
        artifact["records"][0]["payload"]
    )

    with pytest.raises(ValueError):
        admit_signed_component_authority(
            component="universe",
            artifact_value=artifact,
            authority_envelope=fixture.authorities["universe"],
            expected_panel=PANEL,
            bound_source_receipts={receipt_id: fixture.receipt_values[receipt_id]},
            registry=fixture.registry,
            decision_cutoff_by_panel=DECISION_CUTOFFS,
        )


def test_forged_readiness_report_is_rejected_on_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    result = _materialize(
        fixture,
        monkeypatch,
        name="forged-report-bundle",
        include_intrinsic_receipts=False,
    )
    bundle_file = Path(result["bundle_file"])
    report_file = _bundle_locator(bundle_file, "readiness_report")
    report = json.loads(report_file.read_text())
    assert report["ready"] is False
    assert report["blockers"]
    report["ready"] = True
    report["blockers"] = []
    report_file.write_bytes(_canonical(report))

    with pytest.raises(ValueError, match=r"identity mismatch|content has drifted"):
        export_verified_provider_receipt(bundle_file)


def test_component_byte_drift_is_rejected_on_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    result = _materialize(fixture, monkeypatch, name="drifted-component-bundle")
    bundle_file = Path(result["bundle_file"])
    component_file = _bundle_locator(bundle_file, "components", "universe")
    artifact = json.loads(component_file.read_text())
    artifact["records"][0]["payload"]["is_member"] = False
    component_file.write_bytes(_canonical(artifact))

    with pytest.raises(ValueError, match="artifact content has drifted"):
        export_verified_provider_receipt(bundle_file)


@pytest.mark.parametrize(
    ("component", "mutation", "message"),
    [
        ("market_rules", "board", "board does not match panel symbol"),
        ("instrument_status", "status", "ST exception differs"),
        ("market_rules", "source", "source differs"),
        ("market_rules", "predecision", "post-cutoff"),
        ("corporate_actions", "announcement", "announcement is post-cutoff"),
    ],
)
def test_export_replays_signed_semantics_before_exposing_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    mutation: str,
    message: str,
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    result = _materialize(fixture, monkeypatch, name=f"semantic-{mutation}")
    bundle_file = Path(result["bundle_file"])

    def mutate(payload: dict[str, object], record: dict[str, object]) -> None:
        if mutation == "board":
            payload["board"] = "CHINEXT"
        elif mutation == "status":
            payload["is_st"] = True
        elif mutation == "source":
            payload["source"] = "untrusted-rulebook"
        elif mutation == "predecision":
            record["available_at"] = DECISION_CUTOFF
        else:
            payload["events"][0]["announcement_at"] = DECISION_CUTOFF

    _rewrite_export_component(
        bundle_file,
        component=component,
        signer=fixture.signer_key,
        mutate_payload=mutate,
    )

    with pytest.raises(ValueError, match=message):
        export_verified_provider_receipt(bundle_file)


@pytest.mark.parametrize("field", ["publisher_key_id", "available_at_by_panel"])
def test_export_rejects_each_recomputed_readiness_evidence_field_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    result = _materialize(fixture, monkeypatch, name=f"report-drift-{field}")
    bundle_file = Path(result["bundle_file"])

    def mutate(report: dict[str, object]) -> None:
        evidence = report["components"]["universe"]
        if field == "publisher_key_id":
            evidence[field] = "0" * 64
        else:
            evidence[field][PANEL[0]] = f"{DAY}T08:00:01+08:00"

    _rewrite_export_report(bundle_file, mutate)

    with pytest.raises(ValueError, match="semantic authority re-admission"):
        export_verified_provider_receipt(bundle_file)


def test_companion_reverification_rejects_post_cutoff_corporate_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    result = _materialize(fixture, monkeypatch, name="companion-action-bundle")
    bundle_file = Path(result["bundle_file"])

    def mutate(payload: dict[str, object], record: dict[str, object]) -> None:
        del record
        payload["events"][0]["announcement_at"] = DECISION_CUTOFF

    _rewrite_export_component(
        bundle_file,
        component="corporate_actions",
        signer=fixture.signer_key,
        mutate_payload=mutate,
    )

    with pytest.raises(ValueError, match="corporate_actions announcement is post-cutoff"):
        export_verified_provider_receipt(bundle_file)


def test_missing_record_breaks_exact_availability_closure(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    availability = deepcopy(fixture.components["availability_records"])
    availability["records"].pop()

    with pytest.raises(ValueError, match="do not cover"):
        _verify_availability(fixture, availability=availability)


@pytest.mark.parametrize(
    ("component", "available_at", "message"),
    [
        ("decision_context", f"{DAY}T09:25:01+08:00", "post-cutoff"),
        ("execution_prices", f"{DAY}T14:59:59+08:00", "precedes session close"),
        ("signal_prices", NEXT_DECISION_CUTOFF, "post-cutoff"),
    ],
)
def test_phase_specific_post_cutoff_evidence_is_rejected(
    tmp_path: Path, component: str, available_at: str, message: str
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    components = deepcopy(fixture.components)
    components[component]["records"][0]["available_at"] = available_at
    availability = components["availability_records"]
    row = next(
        item for item in availability["records"] if item["component"] == component
    )
    row["available_at"] = available_at

    with pytest.raises(ValueError, match=message):
        _verify_availability(fixture, components=components, availability=availability)


@pytest.mark.parametrize("mutation", ["announcement_at", "available_at"])
def test_availability_reverification_rejects_post_cutoff_corporate_action(
    tmp_path: Path, mutation: str
) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    components = deepcopy(fixture.components)
    record = components["corporate_actions"]["records"][0]
    payload = record["payload"]
    assert isinstance(payload, dict)
    if mutation == "announcement_at":
        event = payload["events"][0]
        assert isinstance(event, dict)
        event["announcement_at"] = DECISION_CUTOFF
        record["record_sha256"] = _sha256(payload)
    else:
        record["available_at"] = DECISION_CUTOFF

    availability = deepcopy(components["availability_records"])
    row = next(
        item
        for item in availability["records"]
        if item["component"] == "corporate_actions"
    )
    row["record_sha256"] = record["record_sha256"]
    row["available_at"] = record["available_at"]

    with pytest.raises(ValueError, match="corporate action availability is post-cutoff"):
        _verify_availability(fixture, components=components, availability=availability)


def test_eight_of_nine_artifacts_cannot_form_readiness_closure(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, complete_calendar=True)
    components = deepcopy(fixture.components)
    components.pop("market_rules")

    with pytest.raises(ValueError, match="component record set is incomplete"):
        verify_component_availability_records(
            fixture.components["availability_records"],
            expected_panel_sha256=_sha256(PANEL),
            expected_panel_size=len(PANEL),
            expected_decision_cutoffs=DECISION_CUTOFFS,
            bound_source_receipt_ids=sorted(fixture.receipt_values),
            component_records={
                component: components[component]["records"]
                for component in REQUIRED_COMPONENTS
                if component not in {"availability_records", "market_rules"}
            },
            expected_signed_calendar_phases=SIGNED_CALENDAR_PHASES,
        )
