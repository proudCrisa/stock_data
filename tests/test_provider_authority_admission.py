from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import sqlite3
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stockdata.authority import (
    AUTHORITY_COMPONENT_ROLES,
    AUTHORITY_ENVELOPE_SCHEMA,
    SIGNER_ENROLLMENT_SCHEMA,
    TRUST_REGISTRY_SCHEMA,
    load_enrolled_trust_registry,
    require_enrolled_role_coverage,
)
import stockdata.collector_continuity as collector_continuity
from stockdata.collector_continuity import parse_collector_ledger
from stockdata.future_panel_registration import (
    FuturePanelRegistrationError,
    prepare_future_collector_database,
    register_future_panel,
)
from stockdata.market_rules import MARKET_RULE_PAYLOAD_SCHEMA
from stockdata.provider_authority_admission import (
    SOURCE_RECEIPT_SCHEMA,
    admit_signed_component_authority,
    require_predecision_authority,
)
from stockdata.provider_materializer import materialize_provider_bundle
from stockdata.registered_panel_capture import (
    RegisteredPanelCaptureError,
    capture_registered_panel,
)
from stockdata.rqgm_provider_contract import (
    COMPONENT_SCHEMAS,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _public(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _key_id(private_key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_public(private_key)).hexdigest()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _registry(tmp_path, root, signer, component):
    roles = [component] if isinstance(component, str) else sorted(component)
    authorization = {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "publisher_key_id": _key_id(signer),
        "publisher_public_key_base64": _b64(_public(signer)),
        "trust_root_id": _key_id(root),
        "component_roles": roles,
        "valid_from": "2025-01-01T00:00:00+00:00",
        "valid_until": "2027-01-01T00:00:00+00:00",
    }
    value = {
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
                "component_roles": roles,
                "valid_from": authorization["valid_from"],
                "valid_until": authorization["valid_until"],
                "authorization_signature_base64": _b64(
                    root.sign(_canonical(authorization))
                ),
            }
        ],
    }
    raw = _canonical(value)
    path = tmp_path / "registry.json"
    path.write_bytes(raw)
    return load_enrolled_trust_registry(
        path, expected_sha256=hashlib.sha256(raw).hexdigest()
    )


def _payload(component: str, day: str) -> dict[str, object]:
    if component == "trading_calendar":
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        return {
            "decision_cutoff_at": f"{day}T09:25:00+08:00",
            "session_close_at": f"{day}T15:00:00+08:00",
            "next_session_decision_cutoff_at": f"{next_day}T09:25:00+08:00",
            "is_trading_day": True,
        }
    if component == "universe":
        return {"is_member": True, "universe_id": hashlib.sha256(b"universe").hexdigest()}
    if component == "instrument_status":
        return {"is_st": False, "is_suspended": False, "listing_status": "listed"}
    if component == "corporate_actions":
        return {"events": []}
    return {
        "schema_version": MARKET_RULE_PAYLOAD_SCHEMA,
        "policy_id": f"cn-a-share-main-sz-{day}-v1",
        "source": "official-calendar",
        "source_sha256": hashlib.sha256(b"official-response").hexdigest(),
        "security_type": "A_SHARE",
        "board": "MAIN",
        "exchange": "SZ",
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


def _artifact(
    component: str,
    panel: list[str],
    receipt_id: str,
    *,
    available_at: str | None = None,
):
    records = []
    for entry in panel:
        day = entry.split("@")[1]
        payload = _payload(component, day)
        records.append(
            {
                "panel_entry": entry,
                "payload": payload,
                "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                "source_receipt_ids": [receipt_id],
                "effective_at": f"{day}T00:00:00+08:00",
                "available_at": available_at or f"{day}T08:00:00+08:00",
            }
        )
    return {
        "schema_version": COMPONENT_SCHEMAS[component],
        "component": component,
        "panel": panel,
        "records": records,
    }


def _generic_market_rules_artifact(panel: list[str]) -> tuple[dict[str, object], dict[str, object]]:
    records = []
    for entry in panel:
        day = entry.split("@")[1]
        symbol = entry.split("@")[0]
        board = "CHINEXT" if symbol.startswith(("300", "301")) else "MAIN"
        for is_st in (False, True):
            payload = _payload("market_rules", day)
            payload["policy_id"] = (
                f"generic-{board.lower()}-sz-{day}-"
                f"{'st' if is_st else 'nonst'}"
            )
            payload["board"] = board
            payload["is_st"] = is_st
            records.append(
                {
                    "panel_entry": entry,
                    "payload": payload,
                    "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                    "source_receipt_ids": [],
                    "effective_at": f"{day}T00:00:00+08:00",
                    "available_at": f"{day}T08:00:00+08:00",
                }
            )
    records.sort(key=lambda record: (record["panel_entry"], record["record_sha256"]))
    artifact = {
        "schema_version": COMPONENT_SCHEMAS["market_rules"],
        "component": "market_rules",
        "panel": sorted(panel),
        "records": records,
    }
    receipt = _source_receipt("market_rules", artifact)
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    for record in records:
        record["source_receipt_ids"] = [receipt_id]
    return artifact, receipt


def _source_receipt(component: str, artifact: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": "official-calendar",
        "observed_at": "2026-08-13T08:00:00+08:00",
        "response_sha256": hashlib.sha256(b"official-response").hexdigest(),
        "bindings": sorted(
            [
                {
                    "component": component,
                    "panel_entry": record["panel_entry"],
                    "record_sha256": record["record_sha256"],
                }
                for record in artifact["records"]
            ],
            key=lambda value: (
                value["component"], value["panel_entry"], value["record_sha256"]
            ),
        ),
    }


def _envelope(component, artifact, receipt_id, registry, root, signer):
    artifact_reference = {
        "kind": f"stock-data-{component.replace('_', '-')}",
        "identifier": hashlib.sha256(_canonical(artifact)).hexdigest(),
        "schema_version": COMPONENT_SCHEMAS[component],
    }
    payload = {
        "component_role": component,
        "artifact": artifact_reference,
        "source_receipt_ids": [receipt_id],
        "effective_at": "2026-08-13T00:00:00+08:00",
        "available_at": "2026-08-13T08:00:00+08:00",
        "publisher_key_id": _key_id(signer),
        "trust_root_id": _key_id(root),
        "trust_registry_sha256": registry.registry_sha256,
    }
    return {
        "schema_version": AUTHORITY_ENVELOPE_SCHEMA,
        "algorithm": "ed25519",
        "payload": payload,
        "signature_base64": _b64(signer.sign(_canonical(payload))),
    }


def _write_json(path: Path, value: object) -> Path:
    path.write_bytes(_canonical(value))
    return path


def _future_registration_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], object]:
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-15T12:00:00+08:00"),
    )
    symbols = [
        "000001.SZ",
        "000002.SZ",
        "000063.SZ",
        "000100.SZ",
        "000157.SZ",
        "000166.SZ",
        "000333.SZ",
        "000568.SZ",
        "000725.SZ",
        "000858.SZ",
        "002415.SZ",
        "300750.SZ",
    ]
    sessions = ["2026-08-17", "2026-08-18", "2026-08-19"]
    panel = sorted(f"{symbol}@{day}" for symbol in symbols for day in sessions)
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    registry = _registry(
        tmp_path, root, signer, tuple(AUTHORITY_COMPONENT_ROLES)
    )
    receipts: list[Path] = []
    component_files: dict[str, Path] = {}
    envelope_files: dict[str, Path] = {}
    for component in ("trading_calendar", "market_rules"):
        if component == "market_rules":
            artifact, receipt = _generic_market_rules_artifact(panel)
            for record in artifact["records"]:
                record["available_at"] = "2026-08-14T08:00:00+08:00"
            receipt = _source_receipt(component, artifact)
        else:
            artifact = _artifact(
                component,
                panel,
                "0" * 64,
                available_at="2026-08-14T08:00:00+08:00",
            )
            receipt = _source_receipt(component, artifact)
        receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
        for record in artifact["records"]:
            record["source_receipt_ids"] = [receipt_id]
        envelope = _envelope(
            component, artifact, receipt_id, registry, root, signer
        )
        receipts.append(_write_json(tmp_path / f"{component}-receipt.json", receipt))
        component_files[component] = _write_json(
            tmp_path / f"{component}.json", artifact
        )
        envelope_files[component] = _write_json(
            tmp_path / f"{component}-authority.json", envelope
        )

    panel_file = _write_json(tmp_path / "panel.json", panel)
    database = tmp_path / "future.sqlite"
    prepare_future_collector_database(
        database_file=database, panel_file=panel_file
    )
    values: dict[str, object] = {
        "output_file": tmp_path / "registration.json",
        "database_file": database,
        "panel_file": panel_file,
        "source_receipt_files": receipts,
        "calendar_file": component_files["trading_calendar"],
        "calendar_authority_file": envelope_files["trading_calendar"],
        "market_rules_file": component_files["market_rules"],
        "market_rules_authority_file": envelope_files["market_rules"],
    }
    return values, registry


@pytest.fixture
def authority(tmp_path):
    component = "trading_calendar"
    panel = ["000001.SZ@2026-08-13", "600519.SH@2026-08-13"]
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    registry = _registry(tmp_path, root, signer, component)
    artifact = _artifact(component, panel, "0" * 64)
    receipt = _source_receipt(component, artifact)
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    for record in artifact["records"]:
        record["source_receipt_ids"] = [receipt_id]
    envelope = _envelope(component, artifact, receipt_id, registry, root, signer)
    return component, panel, receipt_id, registry, artifact, envelope, receipt


def test_admits_registered_signed_exact_panel_authority(authority) -> None:
    component, panel, receipt_id, registry, artifact, envelope, receipt = authority

    admitted = admit_signed_component_authority(
        component=component,
        artifact_value=artifact,
        authority_envelope=envelope,
        expected_panel=panel,
        bound_source_receipts={receipt_id: receipt},
        registry=registry,
        decision_cutoff_by_panel={
            panel[0]: "2026-08-13T09:25:00+08:00"
        },
    )

    assert admitted.readiness_evidence()["ready"] is True
    assert admitted.source_receipt_ids == (receipt_id,)


@pytest.mark.parametrize(
    "component",
    ["universe", "instrument_status", "corporate_actions"],
)
def test_admits_each_strict_component_payload(tmp_path, component) -> None:
    panel = ["000001.SZ@2026-08-13"]
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    registry = _registry(tmp_path, root, signer, component)
    artifact = _artifact(component, panel, "0" * 64)
    receipt = _source_receipt(component, artifact)
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    artifact["records"][0]["source_receipt_ids"] = [receipt_id]
    envelope = _envelope(component, artifact, receipt_id, registry, root, signer)

    admitted = admit_signed_component_authority(
        component=component,
        artifact_value=artifact,
        authority_envelope=envelope,
        expected_panel=panel,
        bound_source_receipts={receipt_id: receipt},
        registry=registry,
        decision_cutoff_by_panel={
            panel[0]: "2026-08-13T09:25:00+08:00"
        },
    )

    assert admitted.component == component


@pytest.mark.parametrize(
    "component",
    ["universe", "instrument_status", "corporate_actions"],
)
def test_rejects_unknown_component_payload_fields(tmp_path, component) -> None:
    panel = ["000001.SZ@2026-08-13"]
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    registry = _registry(tmp_path, root, signer, component)
    artifact = _artifact(component, panel, "0" * 64)
    artifact["records"][0]["payload"] = {"x": 1}
    artifact["records"][0]["record_sha256"] = hashlib.sha256(
        _canonical({"x": 1})
    ).hexdigest()
    receipt = _source_receipt(component, artifact)
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    artifact["records"][0]["source_receipt_ids"] = [receipt_id]
    envelope = _envelope(component, artifact, receipt_id, registry, root, signer)

    with pytest.raises(ValueError):
        admit_signed_component_authority(
            component=component,
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=panel,
            bound_source_receipts={receipt_id: receipt},
            registry=registry,
            decision_cutoff_by_panel={
                panel[0]: "2026-08-13T09:25:00+08:00"
            },
        )


def test_rejects_post_cutoff_record_receipt_or_envelope(authority) -> None:
    component, panel, receipt_id, registry, artifact, envelope, receipt = authority
    admitted = admit_signed_component_authority(
        component=component,
        artifact_value=artifact,
        authority_envelope=envelope,
        expected_panel=panel,
        bound_source_receipts={receipt_id: receipt},
        registry=registry,
    )

    with pytest.raises(ValueError, match="post-cutoff"):
        require_predecision_authority(
            admitted,
            decision_cutoff_by_panel={
                entry: "2026-08-13T07:59:59+08:00" for entry in panel
            },
        )


def test_materializer_and_export_reverify_admitted_authority(
    tmp_path, authority, monkeypatch
) -> None:
    from test_verified_provider_readiness import _fixture

    component, *_ = authority
    fixture = _fixture(tmp_path, complete_calendar=True)
    registry = fixture.registry
    monkeypatch.setattr(
        "stockdata.provider_materializer.load_provider_trust_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "stockdata.rqgm_provider_contract.load_provider_trust_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "stockdata.companion_snapshot.load_provider_trust_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "stockdata.companion_snapshot.PROVIDER_TRUST_REGISTRY_SHA256",
        registry.registry_sha256,
    )
    snapshot_staging = fixture.root / "authority-snapshot-staging"
    snapshot_staging.mkdir(mode=0o700)

    result = materialize_provider_bundle(
        output_dir=tmp_path / "closure",
        database_file=fixture.database,
        registration_file=fixture.registration_file,
        snapshot_staging_directory=snapshot_staging,
        panel_file=fixture.panel_file,
        source_receipt_files=fixture.receipt_files,
        execution_adjustment_file=fixture.execution_adjustment_file,
        signal_adjustment_file=fixture.signal_adjustment_file,
        component_files=fixture.component_files,
        component_authority_files={component: fixture.authority_files[component]},
        source="tencent",
    )

    report = result["receipt"]["readiness_report"]
    assert report["ready"] is False
    assert report["components"][component]["ready"] is True
    assert "provider_component_authority_not_attested" not in {
        item["code"] for item in report["components"][component]["blockers"]
    }


@pytest.mark.parametrize(
    "mutation", ["subset", "record_hash", "receipt", "signature", "effective_at"]
)
def test_rejects_incomplete_tampered_or_unbound_authority(authority, mutation) -> None:
    component, panel, receipt_id, registry, artifact, envelope, receipt = authority
    panel = deepcopy(panel)
    artifact = deepcopy(artifact)
    envelope = deepcopy(envelope)
    if mutation == "subset":
        artifact["panel"].pop()
    elif mutation == "record_hash":
        artifact["records"][0]["record_sha256"] = "0" * 64
    elif mutation == "receipt":
        artifact["records"][0]["source_receipt_ids"] = ["0" * 64]
    elif mutation == "effective_at":
        artifact["records"][0]["effective_at"] = "2026-08-13T15:00:00+08:00"
    else:
        envelope["signature_base64"] = base64.b64encode(b"0" * 64).decode("ascii")

    with pytest.raises(ValueError):
        admit_signed_component_authority(
            component=component,
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=panel,
            bound_source_receipts={receipt_id: receipt},
            registry=registry,
        )


def test_every_declared_receipt_must_bind_the_record(authority) -> None:
    component, panel, receipt_id, registry, artifact, envelope, receipt = authority
    other_receipt = deepcopy(receipt)
    other_receipt["bindings"][0]["record_sha256"] = "0" * 64
    other_receipt_id = hashlib.sha256(_canonical(other_receipt)).hexdigest()
    artifact["records"][0]["source_receipt_ids"] = sorted(
        [receipt_id, other_receipt_id]
    )

    with pytest.raises(ValueError, match="does not bind record"):
        admit_signed_component_authority(
            component=component,
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=panel,
            bound_source_receipts={
                receipt_id: receipt,
                other_receipt_id: other_receipt,
            },
            registry=registry,
        )


def test_future_panel_registration_reverifies_all_prerequisites(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-15T12:00:00+08:00"),
    )
    result = register_future_panel(**inputs)

    assert result["schema_version"] == "rqgm-forward-panel-registration/4"
    assert result["workspace_count"] == 36
    assert result["outcome_feedback_used"] is False
    assert Path(inputs["output_file"]).read_bytes() == _canonical(result)
    prerequisites = result["prerequisites"]
    assert set(prerequisites["role_publishers"]) == AUTHORITY_COMPONENT_ROLES
    collector = prerequisites["collector"]
    assert set(collector) == {
        "schema_version",
        "database_path",
        "ledger_path",
        "source",
        "adjustment_mode",
        "adjustment_version",
        "collector_schema_sha256",
        "database_identity",
        "ledger_identity",
        "database_uuid",
        "cohort_sha256",
        "genesis_sha256",
        "ledger_genesis_event_sha256",
    }
    assert collector["schema_version"] == "stockdata-forward-collector-capability/2"
    assert collector["source"] == "tencent"
    assert (
        prerequisites["market_rule_prerequisite"]["schema_version"]
        == "stockdata-preregistered-generic-market-rulebook/1"
    )
    assert "ready" not in prerequisites["market_rule_prerequisite"]
    schedule = collector_continuity.freeze_collector_step_schedule(
        registration_file=inputs["output_file"]
    )
    spec = schedule[0]
    with collector_continuity.open_registered_collector_read_connection(spec) as token:
        with collector_continuity._borrow_registered_collector_read_connection(
            token, spec
        ) as connection:
            row = connection.execute(
                "SELECT spec_json,spec_sha256 "
                "FROM main.forward_capture_cohort WHERE singleton=1"
            ).fetchone()
            assert row is not None
            spec_json = str(row[0])
            cohort_sha256 = str(row[1])
            assert hashlib.sha256(spec_json.encode("ascii")).hexdigest() == cohort_sha256
            cohort_spec = json.loads(spec_json)
            cohort_symbols = tuple(sorted(str(symbol) for symbol in cohort_spec["symbols"]))
    assert cohort_symbols == tuple(result["symbols"])


def test_future_panel_registration_rejects_production_empty_trust(
    tmp_path, monkeypatch
) -> None:
    inputs, _ = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-15T12:00:00+08:00"),
    )

    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**inputs)

    assert not Path(inputs["output_file"]).exists()


def test_role_coverage_rejects_same_key_root_and_publisher(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    registry = _registry(
        tmp_path, key, key, tuple(AUTHORITY_COMPONENT_ROLES)
    )

    with pytest.raises(ValueError, match="independently enrolled"):
        require_enrolled_role_coverage(
            registry,
            roles=tuple(AUTHORITY_COMPONENT_ROLES),
            valid_from=datetime.fromisoformat("2026-08-15T12:00:00+08:00"),
            valid_until=datetime.fromisoformat("2026-08-20T09:25:00+08:00"),
        )


def test_future_panel_registration_rejects_invalid_collector_trigger(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-15T12:00:00+08:00"),
    )
    database = sqlite3.connect(inputs["database_file"])
    database.execute("DROP TRIGGER forward_context_observations_no_update")
    database.close()

    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**inputs)


def test_future_panel_registration_rejects_existing_sync_coverage(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    database = sqlite3.connect(inputs["database_file"])
    database.execute(
        "INSERT INTO sync_coverage VALUES (?,?,?,?,?,?,?)",
        (
            "000001.SZ",
            "tencent",
            "raw",
            "tencent-qt-daily-v1",
            "2026-08-17",
            "2026-08-19",
            "2026-08-15T12:00:00+08:00",
        ),
    )
    database.commit()
    database.close()

    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**inputs)


def test_future_panel_registration_rejects_malformed_empty_cohort(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    database = sqlite3.connect(inputs["database_file"])
    database.execute("DROP TRIGGER forward_capture_cohort_no_update")
    database.execute("DROP TRIGGER forward_capture_cohort_no_delete")
    database.execute("DROP TABLE forward_capture_cohort")
    database.execute("CREATE TABLE forward_capture_cohort (junk TEXT)")
    database.close()

    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**inputs)


def test_capture_reverifies_database_after_registration(tmp_path, monkeypatch) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    register_future_panel(**inputs)
    database = sqlite3.connect(inputs["database_file"])
    database.execute("DROP TRIGGER forward_context_observations_no_update")
    database.close()
    calls = 0

    def provider_must_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("pre-3.3 registered capture must not call the provider")

    monkeypatch.setattr("stockdata.registered_panel_capture.capture_phase", provider_must_not_run)
    with pytest.raises(RegisteredPanelCaptureError):
        capture_registered_panel(
            inputs["output_file"],
            database=inputs["database_file"],
            effective_date="2026-08-17",
            phase="pre_open",
        )
    assert calls == 0


def test_capture_reverifies_static_authority_after_registration(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    register_future_panel(**inputs)
    calendar_path = Path(inputs["calendar_file"])
    calendar = json.loads(calendar_path.read_text(encoding="ascii"))
    calendar["records"][0]["payload"]["is_trading_day"] = False
    calendar_path.write_bytes(_canonical(calendar))
    calls = 0

    def provider_must_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("pre-3.3 registered capture must not call the provider")

    monkeypatch.setattr("stockdata.registered_panel_capture.capture_phase", provider_must_not_run)
    with pytest.raises(RegisteredPanelCaptureError):
        capture_registered_panel(
            inputs["output_file"],
            database=inputs["database_file"],
            effective_date="2026-08-17",
            phase="pre_open",
        )
    assert calls == 0


def test_non_genesis_collector_rejects_before_registration_output(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    genesis_database = Path(inputs["database_file"])
    legacy_database = tmp_path / "legacy.sqlite"
    legacy_database.write_bytes(genesis_database.read_bytes())
    shutil.copy2(
        collector_continuity.default_collector_ledger_path(genesis_database),
        collector_continuity.default_collector_ledger_path(legacy_database),
    )
    with sqlite3.connect(legacy_database) as connection:
        connection.execute("DROP TRIGGER forward_collector_genesis_no_update")
        connection.execute("DROP TRIGGER forward_collector_genesis_no_delete")
        connection.execute("DROP TABLE forward_collector_genesis")
        connection.commit()
    inputs["database_file"] = legacy_database
    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**inputs)
    assert not Path(inputs["output_file"]).exists()


def test_future_panel_registration_never_overwrites_registration(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-15T12:00:00+08:00"),
    )
    first = register_future_panel(**inputs)
    registration_path = Path(inputs["output_file"])
    ledger_path = Path(
        collector_continuity.default_collector_ledger_path(inputs["database_file"])
    )
    before_registration = registration_path.read_bytes()
    before_ledger = ledger_path.read_bytes()

    second = register_future_panel(**inputs)

    assert second == first
    assert registration_path.read_bytes() == before_registration
    assert ledger_path.read_bytes() == before_ledger


def test_clean_prepared_registration_writes_one_binding_event(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )

    result = register_future_panel(**inputs)
    registration_bytes = Path(inputs["output_file"]).read_bytes()
    events = parse_collector_ledger(
        Path(collector_continuity.default_collector_ledger_path(inputs["database_file"]))
    )

    assert registration_bytes == _canonical(result)
    bindings = [event for event in events if event["event_type"] == "REGISTRATION_BOUND"]
    assert len(bindings) == 1
    assert bindings[0]["event"]["registration_sha256"] == hashlib.sha256(
        registration_bytes
    ).hexdigest()
    assert bindings[0]["event"]["panel_sha256"] == result["panel_sha256"]
    assert bindings[0]["event"]["sessions"] == result["sessions"]
    assert bindings[0]["event"]["sessions_sha256"] == hashlib.sha256(
        _canonical(result["sessions"])
    ).hexdigest()
    assert bindings[0]["event"]["prerequisites_sha256"] == result[
        "prerequisites_sha256"
    ]


def test_registration_retry_after_file_fsync_preserves_bytes_and_inode(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    registration_module = __import__(
        "stockdata.future_panel_registration", fromlist=["_write_exclusive"]
    )
    original_write = registration_module._write_exclusive
    crashed = False

    def crash_after_exclusive_write(path, payload):
        nonlocal crashed
        opened = original_write(path, payload)
        try:
            crashed = True
            raise RuntimeError("simulated crash after registration fsync")
        finally:
            opened.close()

    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    monkeypatch.setattr(registration_module, "_write_exclusive", crash_after_exclusive_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        register_future_panel(**inputs)

    registration_path = Path(inputs["output_file"])
    before_bytes = registration_path.read_bytes()
    before_inode = registration_path.stat().st_ino
    monkeypatch.setattr(registration_module, "_write_exclusive", original_write)
    monkeypatch.setattr(
        "stockdata.future_panel_registration._now",
        lambda: datetime.fromisoformat("2026-08-16T12:00:00+08:00"),
    )

    retry = register_future_panel(**inputs)

    assert crashed
    assert registration_path.read_bytes() == before_bytes
    assert registration_path.stat().st_ino == before_inode
    assert retry["registered_at"] == json.loads(before_bytes)["registered_at"]
    events = parse_collector_ledger(
        Path(collector_continuity.default_collector_ledger_path(inputs["database_file"]))
    )
    assert sum(event["event_type"] == "REGISTRATION_BOUND" for event in events) == 1


def test_registration_retry_after_binding_is_ledger_byte_idempotent(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    register_future_panel(**inputs)
    ledger = Path(collector_continuity.default_collector_ledger_path(inputs["database_file"]))
    before = ledger.read_bytes()

    retry = register_future_panel(**inputs)

    assert retry["schema_version"] == "rqgm-forward-panel-registration/4"
    assert ledger.read_bytes() == before
    assert sum(
        event["event_type"] == "REGISTRATION_BOUND"
        for event in parse_collector_ledger(ledger)
    ) == 1


def test_bound_registration_file_deletion_cannot_rebuild_authority(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    register_future_panel(**inputs)
    registration_path = Path(inputs["output_file"])
    ledger = Path(collector_continuity.default_collector_ledger_path(inputs["database_file"]))
    ledger_before = ledger.read_bytes()
    registration_path.unlink()

    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**inputs)

    assert not registration_path.exists()
    assert ledger.read_bytes() == ledger_before


def test_registration_replacement_during_binding_fails_closed(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    registration_module = __import__(
        "stockdata.future_panel_registration", fromlist=["append_collector_ledger_event"]
    )
    original_append = registration_module.append_collector_ledger_event
    registration_path = Path(inputs["output_file"])
    def replace_before_binding(opened, *, event_type, event):
        replacement = json.loads(registration_path.read_bytes())
        replacement["status"] = "REPLACED_DURING_BINDING"
        replacement_path = registration_path.with_name("registration.replacement.json")
        replacement_path.write_bytes(_canonical(replacement))
        replacement_path.replace(registration_path)
        return original_append(opened, event_type=event_type, event=event)

    monkeypatch.setattr(
        registration_module,
        "append_collector_ledger_event",
        replace_before_binding,
    )
    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**inputs)

    assert json.loads(registration_path.read_bytes())["status"] == "REPLACED_DURING_BINDING"


def _registration_lock_worker(
    inputs, role, barrier, holder_ready, release_holder, results
) -> None:
    module = __import__(
        "stockdata.future_panel_registration", fromlist=["acquire_collector_registration_lock"]
    )
    original_lock = module.acquire_collector_registration_lock

    @contextmanager
    def gated_lock(*, database_path, ledger_path):
        if role == "holder":
            manager = original_lock(database_path=database_path, ledger_path=ledger_path)
            opened = manager.__enter__()
            holder_ready.set()
            barrier.wait(timeout=10)
            release_holder.wait(timeout=10)
            try:
                yield opened
            finally:
                manager.__exit__(None, None, None)
            return

        barrier.wait(timeout=10)
        with original_lock(database_path=database_path, ledger_path=ledger_path) as opened:
            yield opened

    module.acquire_collector_registration_lock = gated_lock
    try:
        result = module.register_future_panel(**inputs)
    except Exception as exc:
        results.put(("contention" if role == "contender" else "error", type(exc).__name__))
    else:
        results.put(("success", result["schema_version"]))


def test_registration_flock_overlap_is_one_success_one_contention_then_retry_idempotent(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    holder_ready = context.Event()
    release_holder = context.Event()
    results = context.Queue()
    try:
        holder = context.Process(
            target=_registration_lock_worker,
            args=(inputs, "holder", barrier, holder_ready, release_holder, results),
        )
        holder.start()
        assert holder_ready.wait(timeout=10)
        contender = context.Process(
            target=_registration_lock_worker,
            args=(inputs, "contender", barrier, holder_ready, release_holder, results),
        )
        contender.start()
        contender_result = results.get(timeout=10)
        assert contender_result[0] == "contention"
        contender.join(timeout=10)
        assert contender.exitcode == 0
        release_holder.set()
        holder_result = results.get(timeout=10)
        holder.join(timeout=10)
        assert holder.exitcode == 0
        assert holder_result == ("success", "rqgm-forward-panel-registration/4")

        retry = register_future_panel(**inputs)
        assert retry["schema_version"] == "rqgm-forward-panel-registration/4"
        ledger = Path(collector_continuity.default_collector_ledger_path(inputs["database_file"]))
        assert sum(
            event["event_type"] == "REGISTRATION_BOUND"
            for event in parse_collector_ledger(ledger)
        ) == 1
    finally:
        results.close()
        results.join_thread()


def test_different_existing_registration_bytes_are_rejected_unchanged(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    register_future_panel(**inputs)
    registration_path = Path(inputs["output_file"])
    ledger = Path(collector_continuity.default_collector_ledger_path(inputs["database_file"]))
    ledger_before = ledger.read_bytes()
    forged = json.loads(registration_path.read_bytes())
    forged["status"] = "FORGED"
    forged_bytes = _canonical(forged)
    registration_path.write_bytes(forged_bytes)

    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**inputs)

    assert registration_path.read_bytes() == forged_bytes
    assert ledger.read_bytes() == ledger_before


def test_duplicate_registration_calls_never_append_two_bindings(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )

    def register_once() -> object:
        try:
            return register_future_panel(**inputs)
        except FuturePanelRegistrationError as exc:
            return exc

    first = register_once()
    second = register_once()
    successful = [result for result in (first, second) if isinstance(result, dict)]
    assert len(successful) == 2
    assert all(result["schema_version"] == "rqgm-forward-panel-registration/4" for result in successful)
    ledger = Path(collector_continuity.default_collector_ledger_path(inputs["database_file"]))
    assert sum(
        event["event_type"] == "REGISTRATION_BOUND"
        for event in parse_collector_ledger(ledger)
    ) == 1


def test_non_genesis_collector_is_rejected_before_registration_output(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    legacy_database = tmp_path / "legacy.sqlite"
    shutil.copy2(inputs["database_file"], legacy_database)
    shutil.copy2(
        collector_continuity.default_collector_ledger_path(inputs["database_file"]),
        collector_continuity.default_collector_ledger_path(legacy_database),
    )
    with sqlite3.connect(legacy_database) as connection:
        connection.execute("DROP TRIGGER forward_collector_genesis_no_update")
        connection.execute("DROP TRIGGER forward_collector_genesis_no_delete")
        connection.execute("DROP TABLE forward_collector_genesis")
    legacy_inputs = {**inputs, "database_file": legacy_database}
    legacy_inputs["output_file"] = tmp_path / "legacy-registration.json"

    with pytest.raises(FuturePanelRegistrationError):
        register_future_panel(**legacy_inputs)

    assert not Path(legacy_inputs["output_file"]).exists()


def test_snapshot_rejects_registration_replacement_after_freeze(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    register_future_panel(**inputs)
    registration_path = Path(inputs["output_file"])
    schedule = collector_continuity.freeze_collector_step_schedule(
        registration_file=registration_path
    )
    replacement = json.loads(registration_path.read_bytes())
    replacement["status"] = "REPLACED"
    registration_path.write_bytes(_canonical(replacement))

    with collector_continuity.open_exact_collector_sqlite(
        database_path=inputs["database_file"],
        ledger_path=collector_continuity.default_collector_ledger_path(
            inputs["database_file"]
        ),
    ) as (connection, _):
        with pytest.raises(collector_continuity.CollectorContinuityError):
            collector_continuity.snapshot_collector_step_state(connection, schedule[0])


def test_registered_capture_rejects_legacy_schema_before_provider_call(
    tmp_path, monkeypatch
) -> None:
    inputs, registry = _future_registration_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stockdata.future_panel_registration.load_provider_trust_registry",
        lambda: registry,
    )
    registration = {
        "schema_version": "rqgm-forward-panel-registration/3",
        "registered_at": "2026-08-15T12:00:00+08:00",
    }
    legacy = Path(inputs["output_file"])
    legacy.write_bytes(_canonical(registration))
    calls = 0

    def provider_must_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not run for legacy registration")

    monkeypatch.setattr("stockdata.registered_panel_capture.capture_phase", provider_must_not_run)
    with pytest.raises(RegisteredPanelCaptureError):
        capture_registered_panel(
            legacy,
            database=inputs["database_file"],
            effective_date="2026-08-17",
            phase="pre_open",
        )
    assert calls == 0
