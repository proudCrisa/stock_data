from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stockdata.authority import (
    AUTHORITY_ENVELOPE_SCHEMA,
    SIGNER_ENROLLMENT_SCHEMA,
    TRUST_REGISTRY_SCHEMA,
    load_enrolled_trust_registry,
)
from stockdata.provider_authority_admission import (
    SOURCE_RECEIPT_SCHEMA,
    admit_signed_component_authority,
    require_predecision_authority,
)
from stockdata.provider_materializer import materialize_provider_bundle
from stockdata.rqgm_provider_contract import (
    COMPONENT_SCHEMAS,
    EXECUTION_ADJUSTMENT_SCHEMA,
    REQUIRED_COMPONENTS,
    SIGNAL_ADJUSTMENT_SCHEMA,
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
    authorization = {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "publisher_key_id": _key_id(signer),
        "publisher_public_key_base64": _b64(_public(signer)),
        "trust_root_id": _key_id(root),
        "component_roles": [component],
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
                "component_roles": [component],
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
        return {"decision_cutoff_at": f"{day}T09:25:00+08:00", "is_trading_day": True}
    if component == "universe":
        return {"is_member": True, "universe_id": hashlib.sha256(b"universe").hexdigest()}
    if component == "instrument_status":
        return {"is_st": False, "is_suspended": False, "listing_status": "listed"}
    if component == "corporate_actions":
        return {"events": []}
    return {
        "board": "main",
        "lot_size": 100,
        "price_limit_policy_id": hashlib.sha256(b"price-limit").hexdigest(),
        "t_plus_one": True,
    }


def _artifact(component: str, panel: list[str], receipt_id: str):
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
                "available_at": f"{day}T08:00:00+08:00",
            }
        )
    return {
        "schema_version": COMPONENT_SCHEMAS[component],
        "component": component,
        "panel": panel,
        "records": records,
    }


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
    ["universe", "instrument_status", "corporate_actions", "market_rules"],
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
    ["universe", "instrument_status", "corporate_actions", "market_rules"],
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
    component, panel, _, registry, artifact, envelope, receipt = authority
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    database = inputs / "cache.sqlite"
    database.write_bytes(b"not-a-sqlite-database")
    panel_file = inputs / "panel.json"
    panel_file.write_bytes(_canonical(panel))
    receipt_file = inputs / "receipt.json"
    receipt_file.write_bytes(_canonical(receipt))
    execution = inputs / "execution.json"
    execution.write_bytes(
        _canonical(
            {
                "schema_version": EXECUTION_ADJUSTMENT_SCHEMA,
                "price_role": "execution",
                "source": "fixture",
                "adjustment_mode": "raw",
                "adjustment_version": "fixture-raw-v1",
            }
        )
    )
    signal = inputs / "signal.json"
    signal.write_bytes(
        _canonical(
            {
                "schema_version": SIGNAL_ADJUSTMENT_SCHEMA,
                "price_role": "signal",
                "source": "fixture",
                "adjustment_mode": "raw",
                "adjustment_version": "fixture-raw-v1",
            }
        )
    )
    component_files = {}
    for name in REQUIRED_COMPONENTS:
        path = inputs / f"{name}.json"
        value = artifact if name == component else {"component": name}
        path.write_bytes(_canonical(value))
        component_files[name] = path
    envelope_file = inputs / "calendar-authority.json"
    envelope_file.write_bytes(_canonical(envelope))
    monkeypatch.setattr(
        "stockdata.provider_materializer.load_provider_trust_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "stockdata.rqgm_provider_contract.load_provider_trust_registry",
        lambda: registry,
    )

    result = materialize_provider_bundle(
        output_dir=tmp_path / "closure",
        database_file=database,
        panel_file=panel_file,
        source_receipt_files=[receipt_file],
        execution_adjustment_file=execution,
        signal_adjustment_file=signal,
        component_files=component_files,
        component_authority_files={component: envelope_file},
        source="fixture",
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
