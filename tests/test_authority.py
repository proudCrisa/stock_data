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
    AUTHORITY_COMPONENT_ROLES,
    EnrolledTrustRegistry,
    PROVIDER_TRUST_REGISTRY_SHA256,
    SIGNER_ENROLLMENT_SCHEMA,
    TRUST_REGISTRY_SCHEMA,
    load_enrolled_trust_registry,
    load_provider_trust_registry,
    verify_authority_envelope,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _public(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _key_id(private_key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_public(private_key)).hexdigest()


def _registry_value(
    root: Ed25519PrivateKey,
    signer: Ed25519PrivateKey,
    *,
    registry_version: int = 1,
    roles: list[str] | None = None,
    valid_from: str = "2025-01-01T00:00:00+00:00",
    valid_until: str = "2027-01-01T00:00:00+00:00",
) -> dict[str, object]:
    root_id = _key_id(root)
    signer_id = _key_id(signer)
    roles = roles or ["trading_calendar", "universe"]
    authorization = {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": registry_version,
        "publisher_key_id": signer_id,
        "publisher_public_key_base64": _b64(_public(signer)),
        "trust_root_id": root_id,
        "component_roles": roles,
        "valid_from": valid_from,
        "valid_until": valid_until,
    }
    return {
        "schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": registry_version,
        "trust_roots": [
            {"trust_root_id": root_id, "public_key_base64": _b64(_public(root))}
        ],
        "signer_enrollments": [
            {
                "publisher_key_id": signer_id,
                "trust_root_id": root_id,
                "public_key_base64": _b64(_public(signer)),
                "component_roles": roles,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "authorization_signature_base64": _b64(
                    root.sign(_canonical(authorization))
                ),
            }
        ],
    }


def _write_registry(tmp_path, value: dict[str, object]):
    path = tmp_path / "trust-registry.json"
    raw = _canonical(value)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _artifact(component: str = "trading_calendar") -> dict[str, object]:
    return {
        "kind": f"stock-data-{component.replace('_', '-')}",
        "identifier": hashlib.sha256(component.encode("ascii")).hexdigest(),
        "schema_version": f"stockdata-{component.replace('_', '-')}/1",
    }


def _envelope(
    signer: Ed25519PrivateKey,
    root: Ed25519PrivateKey,
    registry_sha256: str,
    *,
    component: str = "trading_calendar",
    artifact: dict[str, object] | None = None,
    effective_at: str = "2026-01-02T00:00:00+00:00",
    available_at: str = "2026-01-02T08:00:00+08:00",
) -> dict[str, object]:
    payload = {
        "component_role": component,
        "artifact": artifact or _artifact(component),
        "source_receipt_ids": [hashlib.sha256(b"receipt").hexdigest()],
        "effective_at": effective_at,
        "available_at": available_at,
        "publisher_key_id": _key_id(signer),
        "trust_root_id": _key_id(root),
        "trust_registry_sha256": registry_sha256,
    }
    return {
        "schema_version": AUTHORITY_ENVELOPE_SCHEMA,
        "algorithm": "ed25519",
        "payload": payload,
        "signature_base64": _b64(signer.sign(_canonical(payload))),
    }


@pytest.fixture
def authority_fixture(tmp_path):
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    path, pin = _write_registry(tmp_path, _registry_value(root, signer))
    registry = load_enrolled_trust_registry(path, expected_sha256=pin)
    envelope = _envelope(signer, root, pin)
    receipts = envelope["payload"]["source_receipt_ids"]
    return root, signer, path, pin, registry, envelope, receipts


def _verify(registry, envelope, receipts, *, component="trading_calendar", artifact=None):
    return verify_authority_envelope(
        envelope,
        registry=registry,
        expected_component=component,
        expected_artifact=artifact or _artifact(component),
        expected_source_receipt_ids=receipts,
    )


def test_load_and_verify_authority_envelope(authority_fixture):
    root, signer, _, pin, registry, envelope, receipts = authority_fixture

    verified = _verify(registry, envelope, receipts)

    assert registry.registry_sha256 == pin
    assert verified.publisher_key_id == _key_id(signer)
    assert verified.trust_root_id == _key_id(root)
    assert verified.signature_id == hashlib.sha256(
        base64.b64decode(envelope["signature_base64"])
    ).hexdigest()
    assert verified.effective_at == "2026-01-02T00:00:00+00:00"
    assert verified.available_at == "2026-01-02T08:00:00+08:00"


@pytest.mark.parametrize("component", sorted(AUTHORITY_COMPONENT_ROLES))
def test_one_envelope_contract_covers_every_signed_component(tmp_path, component):
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    roles = sorted(AUTHORITY_COMPONENT_ROLES)
    path, pin = _write_registry(
        tmp_path, _registry_value(root, signer, roles=roles)
    )
    registry = load_enrolled_trust_registry(path, expected_sha256=pin)
    envelope = _envelope(signer, root, pin, component=component)

    assert (
        _verify(
            registry,
            envelope,
            envelope["payload"]["source_receipt_ids"],
            component=component,
        ).publisher_key_id
        == _key_id(signer)
    )


def test_provider_registry_is_pinned_and_has_no_placeholder_enrollment():
    registry = load_provider_trust_registry()
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    envelope = _envelope(
        signer, root, PROVIDER_TRUST_REGISTRY_SHA256
    )

    assert registry.registry_sha256 == PROVIDER_TRUST_REGISTRY_SHA256
    with pytest.raises(ValueError, match="unknown trust root"):
        _verify(
            registry,
            envelope,
            envelope["payload"]["source_receipt_ids"],
        )


def test_registry_requires_the_callers_exact_sha256_pin(authority_fixture):
    _, _, path, pin, _, _, _ = authority_fixture
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="expected pin"):
        load_enrolled_trust_registry(path, expected_sha256=pin)


def test_registry_cannot_be_constructed_without_the_pinned_loader():
    with pytest.raises(ValueError, match="pinned loader"):
        EnrolledTrustRegistry(
            schema_version=TRUST_REGISTRY_SCHEMA,
            registry_version=1,
            registry_sha256="0" * 64,
            _trust_roots={},
            _signers={},
            _verification_token=object(),
        )


def test_registry_rejects_tampered_root_authorization(tmp_path):
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    value = _registry_value(root, signer)
    value["signer_enrollments"][0]["component_roles"] = ["market_rules"]
    path, pin = _write_registry(tmp_path, value)

    with pytest.raises(ValueError, match="root authorization signature"):
        load_enrolled_trust_registry(path, expected_sha256=pin)


def test_unknown_signer_is_rejected(authority_fixture):
    root, _, _, pin, registry, _, receipts = authority_fixture
    attacker = Ed25519PrivateKey.generate()
    envelope = _envelope(attacker, root, pin)

    with pytest.raises(ValueError, match="unknown signer"):
        _verify(registry, envelope, receipts)


def test_unknown_self_generated_root_is_rejected(authority_fixture):
    _, signer, _, pin, registry, _, receipts = authority_fixture
    attacker_root = Ed25519PrivateKey.generate()
    envelope = _envelope(signer, attacker_root, pin)

    with pytest.raises(ValueError, match="unknown trust root"):
        _verify(registry, envelope, receipts)


def test_registry_identity_drift_is_bound_into_envelope(tmp_path):
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    old_path, old_pin = _write_registry(
        tmp_path, _registry_value(root, signer, registry_version=1)
    )
    old_envelope = _envelope(signer, root, old_pin)
    old_path.unlink()
    new_path, new_pin = _write_registry(
        tmp_path, _registry_value(root, signer, registry_version=2)
    )
    registry = load_enrolled_trust_registry(new_path, expected_sha256=new_pin)

    with pytest.raises(ValueError, match="different trust registry"):
        _verify(
            registry,
            old_envelope,
            old_envelope["payload"]["source_receipt_ids"],
        )


def test_envelope_signature_tampering_is_rejected(authority_fixture):
    _, _, _, _, registry, envelope, receipts = authority_fixture
    signature = bytearray(base64.b64decode(envelope["signature_base64"]))
    signature[0] ^= 1
    envelope["signature_base64"] = _b64(bytes(signature))

    with pytest.raises(ValueError, match="envelope signature"):
        _verify(registry, envelope, receipts)


def test_envelope_content_tampering_is_rejected_by_signature(authority_fixture):
    _, _, _, _, registry, envelope, receipts = authority_fixture
    tampered_artifact = deepcopy(_artifact())
    tampered_artifact["identifier"] = hashlib.sha256(b"changed").hexdigest()
    envelope["payload"]["artifact"] = tampered_artifact

    with pytest.raises(ValueError, match="envelope signature"):
        _verify(registry, envelope, receipts, artifact=tampered_artifact)


@pytest.mark.parametrize(
    ("effective_at", "available_at", "message"),
    [
        (
            "2027-01-01T00:00:00+00:00",
            "2027-01-01T00:00:01+00:00",
            "authorization interval",
        ),
        (
            "2026-01-02T00:00:00",
            "2026-01-02T01:00:00+00:00",
            "timezone-aware",
        ),
        (
            "2026-01-02T00:00:00Z",
            "2026-01-02T01:00:00+00:00",
            "canonical",
        ),
    ],
)
def test_invalid_or_noncanonical_envelope_times_are_rejected(
    authority_fixture, effective_at, available_at, message
):
    root, signer, _, pin, registry, _, receipts = authority_fixture
    envelope = _envelope(
        signer,
        root,
        pin,
        effective_at=effective_at,
        available_at=available_at,
    )

    with pytest.raises(ValueError, match=message):
        _verify(registry, envelope, receipts)


@pytest.mark.parametrize(
    "available_at",
    ["2025-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"],
)
def test_signer_authorization_interval_includes_both_boundaries(
    authority_fixture, available_at
):
    root, signer, _, pin, registry, _, receipts = authority_fixture
    envelope = _envelope(
        signer,
        root,
        pin,
        effective_at="2025-01-01T00:00:00+00:00",
        available_at=available_at,
    )

    assert _verify(registry, envelope, receipts).available_at == available_at


def test_preannounced_authority_may_be_available_before_it_becomes_effective(
    authority_fixture,
):
    root, signer, _, pin, registry, _, receipts = authority_fixture
    envelope = _envelope(
        signer,
        root,
        pin,
        effective_at="2026-01-03T00:00:00+00:00",
        available_at="2026-01-02T00:00:00+00:00",
    )

    assert _verify(registry, envelope, receipts).effective_at.startswith(
        "2026-01-03"
    )


def test_wrong_component_role_is_rejected(authority_fixture):
    root, signer, _, pin, registry, _, receipts = authority_fixture
    envelope = _envelope(signer, root, pin, component="market_rules")

    with pytest.raises(ValueError, match="not authorized"):
        _verify(registry, envelope, receipts, component="market_rules")


def test_expected_artifact_and_receipts_are_independently_bound(authority_fixture):
    _, _, _, _, registry, envelope, receipts = authority_fixture
    wrong_artifact = deepcopy(_artifact())
    wrong_artifact["identifier"] = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(ValueError, match="expected reference"):
        _verify(registry, envelope, receipts, artifact=wrong_artifact)

    with pytest.raises(ValueError, match="expected identities"):
        _verify(registry, envelope, [hashlib.sha256(b"other").hexdigest()])
