from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from stockdata.authority import (
    SIGNER_ENROLLMENT_SCHEMA,
    TRUST_REGISTRY_SCHEMA,
    load_enrolled_trust_registry,
)
from stockdata.market_rules import MARKET_RULE_PAYLOAD_SCHEMA
from stockdata.provider_authority_admission import (
    GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA,
    PreregisteredGenericMarketRulebook,
    SOURCE_RECEIPT_SCHEMA,
)
from stockdata.provider_authority_publisher import (
    build_canonical_registry,
    main,
    publish_authority_envelope,
)
from stockdata.rqgm_provider_contract import COMPONENT_SCHEMAS


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _write_json(path, value: object) -> None:
    path.write_bytes(_canonical(value))


def _public(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _private_raw(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        Encoding.Raw,
        PrivateFormat.Raw,
        NoEncryption(),
    )


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _key_id(private_key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_public(private_key)).hexdigest()


def _root_entry(root: Ed25519PrivateKey) -> dict[str, object]:
    return {
        "trust_root_id": _key_id(root),
        "public_key_base64": _b64(_public(root)),
    }


def _enrollment(
    root: Ed25519PrivateKey,
    signer: Ed25519PrivateKey,
    *,
    roles: list[str] | None = None,
    trust_root_id: str | None = None,
    publisher_key_id: str | None = None,
) -> dict[str, object]:
    component_roles = ["trading_calendar"] if roles is None else roles
    value = {
        "publisher_key_id": publisher_key_id or _key_id(signer),
        "trust_root_id": trust_root_id or _key_id(root),
        "public_key_base64": _b64(_public(signer)),
        "component_roles": component_roles,
        "valid_from": "2026-01-01T00:00:00+08:00",
        "valid_until": "2027-01-01T00:00:00+08:00",
    }
    payload = {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "publisher_key_id": value["publisher_key_id"],
        "publisher_public_key_base64": value["public_key_base64"],
        "trust_root_id": value["trust_root_id"],
        "component_roles": value["component_roles"],
        "valid_from": value["valid_from"],
        "valid_until": value["valid_until"],
    }
    value["authorization_signature_base64"] = _b64(root.sign(_canonical(payload)))
    return value


def _calendar_artifact(panel: list[str]) -> dict[str, object]:
    records = []
    for entry in panel:
        day = entry.split("@")[1]
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        payload = {
            "is_trading_day": True,
            "decision_cutoff_at": f"{day}T09:25:00+08:00",
            "session_close_at": f"{day}T15:00:00+08:00",
            "next_session_decision_cutoff_at": f"{next_day}T09:25:00+08:00",
        }
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
    return {
        "schema_version": COMPONENT_SCHEMAS["trading_calendar"],
        "component": "trading_calendar",
        "panel": sorted(panel),
        "records": records,
    }


def _generic_market_rules_artifact(panel: list[str]) -> dict[str, object]:
    source_sha256 = hashlib.sha256(b"provider-response").hexdigest()
    records = []
    for entry in sorted(panel):
        symbol, day = entry.split("@")
        exchange = symbol.rsplit(".", 1)[1]
        if exchange == "SZ" and symbol.startswith(("300", "301")):
            board = "CHINEXT"
        elif exchange == "SH" and symbol.startswith(("688", "689")):
            board = "STAR"
        elif exchange == "BJ":
            board = "BSE"
        else:
            board = "MAIN"
        for is_st in (False, True):
            payload = {
                "schema_version": MARKET_RULE_PAYLOAD_SCHEMA,
                "policy_id": (
                    f"generic-{board.lower()}-{exchange.lower()}-{day}-"
                    f"{'st' if is_st else 'nonst'}"
                ),
                "source": "external-fixture",
                "source_sha256": source_sha256,
                "security_type": "A_SHARE",
                "board": board,
                "exchange": exchange,
                "effective_from": day,
                "effective_until": day,
                "listing_age_min": 0,
                "listing_age_max": None,
                "is_st": is_st,
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
    return {
        "schema_version": COMPONENT_SCHEMAS["market_rules"],
        "component": "market_rules",
        "panel": sorted(panel),
        "records": sorted(
            records,
            key=lambda record: (record["panel_entry"], record["record_sha256"]),
        ),
    }


def _receipt(component: str, artifact: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": "external-fixture",
        "observed_at": "2026-08-13T08:00:00+08:00",
        "response_sha256": hashlib.sha256(b"provider-response").hexdigest(),
        "bindings": [
            {
                "component": component,
                "panel_entry": record["panel_entry"],
                "record_sha256": record["record_sha256"],
            }
            for record in artifact["records"]
        ],
    }


@pytest.fixture
def publisher_fixture(tmp_path, monkeypatch):
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    root_file = tmp_path / "root.json"
    enrollment_file = tmp_path / "enrollment.json"
    registry_file = tmp_path / "registry.json"
    _write_json(root_file, _root_entry(root))
    _write_json(enrollment_file, _enrollment(root, signer))
    registry = build_canonical_registry(
        root_public_key_file=root_file,
        enrollment_file=enrollment_file,
        output_file=registry_file,
    )
    artifact = _calendar_artifact(
        ["000001.SZ@2026-08-13", "600519.SH@2026-08-13"]
    )
    receipt = _receipt("trading_calendar", artifact)
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    for record in artifact["records"]:
        record["source_receipt_ids"] = [receipt_id]
    artifact_file = tmp_path / "artifact.json"
    receipt_file = tmp_path / "receipt.json"
    _write_json(artifact_file, artifact)
    _write_json(receipt_file, receipt)
    monkeypatch.setenv("SIGNER_PRIVATE_KEY_B64", _b64(_private_raw(signer)))
    return {
        "root": root,
        "signer": signer,
        "root_file": root_file,
        "enrollment_file": enrollment_file,
        "registry_file": registry_file,
        "registry": registry,
        "artifact": artifact,
        "artifact_file": artifact_file,
        "receipt": receipt,
        "receipt_file": receipt_file,
        "receipt_id": receipt_id,
    }


@pytest.fixture
def generic_publisher_fixture(tmp_path, monkeypatch):
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    root_file = tmp_path / "generic-root.json"
    enrollment_file = tmp_path / "generic-enrollment.json"
    registry_file = tmp_path / "generic-registry.json"
    _write_json(root_file, _root_entry(root))
    _write_json(
        enrollment_file,
        _enrollment(root, signer, roles=["market_rules"]),
    )
    registry = build_canonical_registry(
        root_public_key_file=root_file,
        enrollment_file=enrollment_file,
        output_file=registry_file,
    )
    panel = ["000001.SZ@2026-08-13", "600519.SH@2026-08-13"]
    artifact = _generic_market_rules_artifact(panel)
    receipt = _receipt("market_rules", artifact)
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    for record in artifact["records"]:
        record["source_receipt_ids"] = [receipt_id]
    artifact_file = tmp_path / "generic-artifact.json"
    receipt_file = tmp_path / "generic-receipt.json"
    _write_json(artifact_file, artifact)
    _write_json(receipt_file, receipt)
    monkeypatch.setenv("GENERIC_SIGNER_PRIVATE_KEY_B64", _b64(_private_raw(signer)))
    return {
        "root": root,
        "signer": signer,
        "registry_file": registry_file,
        "registry": registry,
        "artifact_file": artifact_file,
        "receipt_file": receipt_file,
        "receipt_id": receipt_id,
        "panel": panel,
    }


def test_builds_canonical_registry_and_production_loads_it(publisher_fixture):
    registry_file = publisher_fixture["registry_file"]
    registry_sha256 = hashlib.sha256(registry_file.read_bytes()).hexdigest()

    registry = load_enrolled_trust_registry(
        registry_file, expected_sha256=registry_sha256
    )

    assert registry.registry_sha256 == registry_sha256
    assert publisher_fixture["signer"].public_key()


def test_publishes_authority_envelope_and_production_admits_it(
    publisher_fixture, tmp_path
):
    output_file = tmp_path / "envelope.json"

    published = publish_authority_envelope(
        component="trading_calendar",
        registry_file=publisher_fixture["registry_file"],
        registry_sha256=publisher_fixture["registry"].registry_sha256,
        artifact_file=publisher_fixture["artifact_file"],
        source_receipt_files=[publisher_fixture["receipt_file"]],
        signer_private_key_env="SIGNER_PRIVATE_KEY_B64",
        output_file=output_file,
        effective_at="2026-08-13T00:00:00+08:00",
        available_at="2026-08-13T08:00:00+08:00",
    )

    assert output_file.read_bytes() == _canonical(published.envelope)
    assert published.admitted.source_receipt_ids == (publisher_fixture["receipt_id"],)
    assert published.admitted.readiness_evidence()["ready"] is True


def test_publishes_generic_market_rulebook_as_prerequisite(
    generic_publisher_fixture, tmp_path
):
    output_file = tmp_path / "generic-envelope.json"

    published = publish_authority_envelope(
        component="market_rules",
        registry_file=generic_publisher_fixture["registry_file"],
        registry_sha256=generic_publisher_fixture["registry"].registry_sha256,
        artifact_file=generic_publisher_fixture["artifact_file"],
        source_receipt_files=[generic_publisher_fixture["receipt_file"]],
        signer_private_key_env="GENERIC_SIGNER_PRIVATE_KEY_B64",
        output_file=output_file,
        effective_at="2026-08-13T00:00:00+08:00",
        available_at="2026-08-13T08:00:00+08:00",
        decision_cutoff_by_panel={
            entry: "2026-08-13T09:25:00+08:00"
            for entry in generic_publisher_fixture["panel"]
        },
    )

    assert output_file.read_bytes() == _canonical(published.envelope)
    assert published.admitted is None
    assert isinstance(published.preregistered, PreregisteredGenericMarketRulebook)
    assert (
        published.preregistered.prerequisite_evidence()["schema_version"]
        == GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA
    )
    assert "ready" not in published.preregistered.prerequisite_evidence()


def test_cli_builds_registry_and_publishes_envelope(publisher_fixture, tmp_path):
    registry_file = tmp_path / "cli-registry.json"
    envelope_file = tmp_path / "cli-envelope.json"

    assert (
        main(
            [
                "build-registry",
                "--root-public-key",
                str(publisher_fixture["root_file"]),
                "--enrollment",
                str(publisher_fixture["enrollment_file"]),
                "--output",
                str(registry_file),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "publish-envelope",
                "--component",
                "trading_calendar",
                "--registry",
                str(registry_file),
                "--registry-sha256",
                hashlib.sha256(registry_file.read_bytes()).hexdigest(),
                "--artifact",
                str(publisher_fixture["artifact_file"]),
                "--source-receipt",
                str(publisher_fixture["receipt_file"]),
                "--signer-private-key-env",
                "SIGNER_PRIVATE_KEY_B64",
                "--output",
                str(envelope_file),
                "--effective-at",
                "2026-08-13T00:00:00+08:00",
                "--available-at",
                "2026-08-13T08:00:00+08:00",
            ]
        )
        == 0
    )
    assert envelope_file.exists()


def test_cli_requires_explicit_registry_sha256(publisher_fixture, tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "publish-envelope",
                "--component",
                "trading_calendar",
                "--registry",
                str(publisher_fixture["registry_file"]),
                "--artifact",
                str(publisher_fixture["artifact_file"]),
                "--source-receipt",
                str(publisher_fixture["receipt_file"]),
                "--signer-private-key-env",
                "SIGNER_PRIVATE_KEY_B64",
                "--output",
                str(tmp_path / "envelope.json"),
                "--effective-at",
                "2026-08-13T00:00:00+08:00",
                "--available-at",
                "2026-08-13T08:00:00+08:00",
            ]
        )

    assert exc_info.value.code == 2


def test_rejects_non_canonical_json(publisher_fixture, tmp_path):
    bad_root_file = tmp_path / "bad-root.json"
    bad_root_file.write_text(
        json.dumps(_root_entry(publisher_fixture["root"]), indent=2),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="canonical JSON"):
        build_canonical_registry(
            root_public_key_file=bad_root_file,
            enrollment_file=publisher_fixture["enrollment_file"],
            output_file=tmp_path / "registry.json",
        )


def test_rejects_role_mismatch(publisher_fixture, tmp_path):
    output_file = tmp_path / "envelope.json"

    with pytest.raises(ValueError, match="role"):
        publish_authority_envelope(
            component="universe",
            registry_file=publisher_fixture["registry_file"],
            registry_sha256=publisher_fixture["registry"].registry_sha256,
            artifact_file=publisher_fixture["artifact_file"],
            source_receipt_files=[publisher_fixture["receipt_file"]],
            signer_private_key_env="SIGNER_PRIVATE_KEY_B64",
            output_file=output_file,
            effective_at="2026-08-13T00:00:00+08:00",
            available_at="2026-08-13T08:00:00+08:00",
        )


def test_rejects_key_id_mismatch(publisher_fixture, tmp_path):
    other = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError, match="publisher_key_id"):
        publish_authority_envelope(
            component="trading_calendar",
            registry_file=publisher_fixture["registry_file"],
            registry_sha256=publisher_fixture["registry"].registry_sha256,
            artifact_file=publisher_fixture["artifact_file"],
            source_receipt_files=[publisher_fixture["receipt_file"]],
            signer_private_key_env="SIGNER_PRIVATE_KEY_B64",
            output_file=tmp_path / "envelope.json",
            effective_at="2026-08-13T00:00:00+08:00",
            available_at="2026-08-13T08:00:00+08:00",
            publisher_key_id=_key_id(other),
        )


def test_rejects_receipt_not_binding_artifact(publisher_fixture, tmp_path):
    receipt = deepcopy(publisher_fixture["receipt"])
    receipt["bindings"][0]["record_sha256"] = "0" * 64
    receipt_file = tmp_path / "bad-receipt.json"
    _write_json(receipt_file, receipt)

    with pytest.raises(ValueError, match="receipt"):
        publish_authority_envelope(
            component="trading_calendar",
            registry_file=publisher_fixture["registry_file"],
            registry_sha256=publisher_fixture["registry"].registry_sha256,
            artifact_file=publisher_fixture["artifact_file"],
            source_receipt_files=[receipt_file],
            signer_private_key_env="SIGNER_PRIVATE_KEY_B64",
            output_file=tmp_path / "envelope.json",
            effective_at="2026-08-13T00:00:00+08:00",
            available_at="2026-08-13T08:00:00+08:00",
        )


def test_rejects_empty_roles(tmp_path):
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    root_file = tmp_path / "root.json"
    enrollment_file = tmp_path / "enrollment.json"
    _write_json(root_file, _root_entry(root))
    _write_json(enrollment_file, _enrollment(root, signer, roles=[]))

    with pytest.raises(ValueError, match="roles"):
        build_canonical_registry(
            root_public_key_file=root_file,
            enrollment_file=enrollment_file,
            output_file=tmp_path / "registry.json",
        )


def test_rejects_unknown_signer(publisher_fixture, tmp_path, monkeypatch):
    unknown = Ed25519PrivateKey.generate()
    monkeypatch.setenv("UNKNOWN_SIGNER_B64", _b64(_private_raw(unknown)))

    with pytest.raises(ValueError, match="unknown signer"):
        publish_authority_envelope(
            component="trading_calendar",
            registry_file=publisher_fixture["registry_file"],
            registry_sha256=publisher_fixture["registry"].registry_sha256,
            artifact_file=publisher_fixture["artifact_file"],
            source_receipt_files=[publisher_fixture["receipt_file"]],
            signer_private_key_env="UNKNOWN_SIGNER_B64",
            output_file=tmp_path / "envelope.json",
            effective_at="2026-08-13T00:00:00+08:00",
            available_at="2026-08-13T08:00:00+08:00",
        )


def test_cli_rejects_legacy_sign_component_subcommand() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["sign-component"])

    assert exc_info.value.code == 2


def test_cli_rejects_plural_source_receipts_flag(
    publisher_fixture, tmp_path
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "publish-envelope",
                "--component",
                "trading_calendar",
                "--registry",
                str(publisher_fixture["registry_file"]),
                "--artifact",
                str(publisher_fixture["artifact_file"]),
                "--source-receipts",
                str(publisher_fixture["receipt_file"]),
                "--signer-private-key-env",
                "SIGNER_PRIVATE_KEY_B64",
                "--output",
                str(tmp_path / "envelope.json"),
                "--effective-at",
                "2026-08-13T00:00:00+08:00",
                "--available-at",
                "2026-08-13T08:00:00+08:00",
            ]
        )

    assert exc_info.value.code == 2


def test_cli_rejects_private_key_shaped_build_registry_arguments(
    tmp_path,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "build-registry",
                "--publisher-public-key-base64",
                "unused",
                "--component-roles-json",
                "[]",
                "--valid-from",
                "2026-01-01T00:00:00+08:00",
                "--valid-until",
                "2027-01-01T00:00:00+08:00",
                "--output",
                str(tmp_path / "registry.json"),
            ]
        )

    assert exc_info.value.code == 2
