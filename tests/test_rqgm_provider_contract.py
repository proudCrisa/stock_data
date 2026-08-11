from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stockdata.authority import (
    AUTHORITY_ENVELOPE_SCHEMA,
    SIGNER_ENROLLMENT_SCHEMA,
    TRUST_REGISTRY_SCHEMA,
)
from stockdata.component_availability import (
    AVAILABILITY_RECORDS_SCHEMA,
    EVIDENCE_COMPONENTS,
)

from stockdata.rqgm_provider_contract import (
    CHECKOUT_SCHEMA,
    COMPONENT_SCHEMAS,
    COMPANION_SNAPSHOT_SCHEMA,
    DATABASE_SCHEMA,
    EXACT_PANEL_SCHEMA,
    EXECUTION_ADJUSTMENT_SCHEMA,
    ProviderArtifactReference,
    READINESS_REPORT_SCHEMA,
    REQUIRED_COMPONENTS,
    SOURCE_RECEIPT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
    build_rqgm_provider_contract,
    verify_readiness_report,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _hash_payload(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _key_id(private_key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_public_key(private_key)).hexdigest()


def _base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _ref(kind: str, schema: str, value: str) -> ProviderArtifactReference:
    return ProviderArtifactReference(kind, _sha(value), schema)


def _base_refs() -> dict[str, object]:
    return {
        "checkout": _ref("stock-data-checkout", CHECKOUT_SCHEMA, "checkout"),
        "database": _ref("stock-data-database", DATABASE_SCHEMA, "database"),
        "source_receipts": (
            _ref("stock-data-source-receipt", SOURCE_RECEIPT_SCHEMA, "receipt-b"),
            _ref("stock-data-source-receipt", SOURCE_RECEIPT_SCHEMA, "receipt-a"),
        ),
        "execution_adjustment_identity": _ref(
            "stock-data-execution-adjustment",
            EXECUTION_ADJUSTMENT_SCHEMA,
            "execution-adjustment",
        ),
        "signal_adjustment_identity": _ref(
            "stock-data-signal-adjustment",
            SIGNAL_ADJUSTMENT_SCHEMA,
            "signal-adjustment",
        ),
        "exact_panel": _ref("stock-data-exact-panel", EXACT_PANEL_SCHEMA, "panel"),
        "companion_snapshot": _ref(
            "stock-data-companion-snapshot",
            COMPANION_SNAPSHOT_SCHEMA,
            "snapshot",
        ),
    }


def _component(component: str, receipt_id: str) -> dict[str, object]:
    value = {
        "ready": True,
        "blockers": [],
        "artifact": {
            "kind": f"stock-data-{component.replace('_', '-')}",
            "identifier": _sha(f"artifact:{component}"),
            "schema_version": COMPONENT_SCHEMAS[component],
        },
        "source_receipt_ids": [receipt_id],
    }
    if component in {
        "trading_calendar",
        "universe",
        "instrument_status",
        "corporate_actions",
        "market_rules",
    }:
        value.update(
            {
                "publisher_key_id": _sha(f"publisher:{component}"),
                "trust_root_id": _sha(f"trust:{component}"),
                "signature_id": _sha(f"signature:{component}"),
            }
        )
    return value


def _report(refs: dict[str, object]) -> dict[str, object]:
    receipt_id = refs["source_receipts"][0].identifier
    return {
        "schema_version": READINESS_REPORT_SCHEMA,
        "ready": True,
        "request": {
            "database_sha256": refs["database"].identifier,
            "execution_adjustment_sha256": refs[
                "execution_adjustment_identity"
            ].identifier,
            "signal_adjustment_sha256": refs[
                "signal_adjustment_identity"
            ].identifier,
            "panel_sha256": refs["exact_panel"].identifier,
            "panel_size": 1,
            "companion_snapshot_sha256": refs["companion_snapshot"].identifier,
        },
        "blockers": [],
        "components": {
            component: _component(component, receipt_id)
            for component in REQUIRED_COMPONENTS
        },
    }


def _availability_artifact(
    panel: list[str], receipt_id: str
) -> dict[str, object]:
    return {
        "schema_version": AVAILABILITY_RECORDS_SCHEMA,
        "panel": panel,
        "records": [
            {
                "component": component,
                "panel_entry": panel_entry,
                "record_sha256": _sha(f"{component}:{panel_entry}"),
                "source_receipt_ids": [receipt_id],
                "effective_at": f"{panel_entry.split('@')[1]}T00:00:00+08:00",
                "available_at": f"{panel_entry.split('@')[1]}T09:00:00+08:00",
                "decision_cutoff_at": f"{panel_entry.split('@')[1]}T09:25:00+08:00",
            }
            for component in EVIDENCE_COMPONENTS
            for panel_entry in panel
        ],
    }


def _block_signed_components(report: dict[str, object]) -> None:
    signed_components = {
        "trading_calendar",
        "universe",
        "instrument_status",
        "corporate_actions",
        "market_rules",
    }
    for component in signed_components:
        report["components"][component] = {
            "ready": False,
            "blockers": [{"code": f"{component}_authority_not_enrolled"}],
        }
    report["ready"] = False
    report["blockers"] = [
        {
            "component": component,
            "code": f"{component}_authority_not_enrolled",
        }
        for component in sorted(signed_components)
    ]


def _attach_self_enrolled_authorities(report: dict[str, object]) -> None:
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    root_id = _key_id(root)
    signer_id = _key_id(signer)
    roles = sorted(
        {
            "trading_calendar",
            "universe",
            "instrument_status",
            "corporate_actions",
            "market_rules",
        }
    )
    authorization = {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "publisher_key_id": signer_id,
        "publisher_public_key_base64": _base64(_public_key(signer)),
        "trust_root_id": root_id,
        "component_roles": roles,
        "valid_from": "2025-01-01T00:00:00+00:00",
        "valid_until": "2027-01-01T00:00:00+00:00",
    }
    registry_value = {
        "schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "trust_roots": [
            {
                "trust_root_id": root_id,
                "public_key_base64": _base64(_public_key(root)),
            }
        ],
        "signer_enrollments": [
            {
                "publisher_key_id": signer_id,
                "trust_root_id": root_id,
                "public_key_base64": _base64(_public_key(signer)),
                "component_roles": roles,
                "valid_from": authorization["valid_from"],
                "valid_until": authorization["valid_until"],
                "authorization_signature_base64": _base64(
                    root.sign(_canonical(authorization))
                ),
            }
        ],
    }
    registry_bytes = _canonical(registry_value)
    registry_sha256 = hashlib.sha256(registry_bytes).hexdigest()
    for component in roles:
        evidence = report["components"][component]
        if evidence["ready"] is not True:
            continue
        payload = {
            "component_role": component,
            "artifact": evidence["artifact"],
            "source_receipt_ids": sorted(evidence["source_receipt_ids"]),
            "effective_at": "2026-01-02T00:00:00+00:00",
            "available_at": "2026-01-02T08:00:00+08:00",
            "publisher_key_id": signer_id,
            "trust_root_id": root_id,
            "trust_registry_sha256": registry_sha256,
        }
        signature = signer.sign(_canonical(payload))
        evidence.update(
            {
                "publisher_key_id": signer_id,
                "trust_root_id": root_id,
                "signature_id": hashlib.sha256(signature).hexdigest(),
                "authority_envelope": {
                    "schema_version": AUTHORITY_ENVELOPE_SCHEMA,
                    "algorithm": "ed25519",
                    "payload": payload,
                    "signature_base64": _base64(signature),
                },
            }
        )


def _contract_and_report():
    refs = _base_refs()
    report = _report(refs)
    refs["readiness_report"] = ProviderArtifactReference(
        "stock-data-readiness-report",
        _hash_payload(report),
        READINESS_REPORT_SCHEMA,
    )
    return build_rqgm_provider_contract(**refs), report


def test_provider_contract_is_owned_content_addressed_and_deterministic() -> None:
    contract, _ = _contract_and_report()
    refs = _base_refs()
    refs["readiness_report"] = contract.readiness_report
    replay = build_rqgm_provider_contract(
        **{**refs, "source_receipts": tuple(reversed(refs["source_receipts"]))}
    )

    assert replay == contract
    assert contract.repository_owner == "stock_data"
    assert contract.required_components == REQUIRED_COMPONENTS
    assert contract.contract_sha256 == _hash_payload(contract.identity_payload())


def test_provider_contract_rejects_aliases_duplicates_and_tampering() -> None:
    contract, _ = _contract_and_report()
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(
            contract,
            database=ProviderArtifactReference(
                "stock-data-database", "latest.sqlite", DATABASE_SCHEMA
            ),
        )
    with pytest.raises(ValueError, match="unique and canonically ordered"):
        replace(
            contract,
            source_receipts=(contract.source_receipts[0], contract.source_receipts[0]),
        )
    with pytest.raises(ValueError, match="content identity mismatch"):
        replace(contract, exact_panel=_ref("stock-data-exact-panel", EXACT_PANEL_SCHEMA, "other"))


def test_self_enrolled_complete_report_cannot_replace_provider_trust() -> None:
    refs = _base_refs()
    report = _report(refs)
    _attach_self_enrolled_authorities(report)
    refs["readiness_report"] = ProviderArtifactReference(
        "stock-data-readiness-report",
        _hash_payload(report),
        READINESS_REPORT_SCHEMA,
    )
    contract = build_rqgm_provider_contract(**refs)

    with pytest.raises(ValueError, match="different trust registry"):
        verify_readiness_report(report, contract)


def test_ready_signed_component_requires_a_verified_envelope() -> None:
    contract, report = _contract_and_report()

    with pytest.raises(ValueError, match="envelope schema is incomplete"):
        verify_readiness_report(report, contract)


@pytest.mark.parametrize("missing", REQUIRED_COMPONENTS)
def test_missing_component_is_contract_invalid(missing: str) -> None:
    refs = _base_refs()
    report = _report(refs)
    report["components"].pop(missing)
    refs["readiness_report"] = ProviderArtifactReference(
        "stock-data-readiness-report", _hash_payload(report), READINESS_REPORT_SCHEMA
    )
    contract = build_rqgm_provider_contract(**refs)

    with pytest.raises(ValueError, match="component set is incomplete"):
        verify_readiness_report(report, contract)


def test_price_only_report_cannot_unlock_full_readiness() -> None:
    refs = _base_refs()
    report = _report(refs)
    report["components"] = {
        key: report["components"][key]
        for key in ("execution_prices", "signal_prices")
    }
    refs["readiness_report"] = ProviderArtifactReference(
        "stock-data-readiness-report", _hash_payload(report), READINESS_REPORT_SCHEMA
    )
    contract = build_rqgm_provider_contract(**refs)

    with pytest.raises(ValueError, match="component set is incomplete"):
        verify_readiness_report(report, contract)


def test_signed_component_requires_enrolled_trust_identity() -> None:
    refs = _base_refs()
    report = _report(refs)
    report["components"]["trading_calendar"].pop("trust_root_id")
    refs["readiness_report"] = ProviderArtifactReference(
        "stock-data-readiness-report", _hash_payload(report), READINESS_REPORT_SCHEMA
    )
    contract = build_rqgm_provider_contract(**refs)

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        verify_readiness_report(report, contract)


def test_explicit_blockers_are_valid_but_never_ready() -> None:
    refs = _base_refs()
    report = _report(refs)
    _block_signed_components(report)
    report["components"]["availability_records"] = {
        "ready": False,
        "blockers": [{"code": "availability_records_not_bound"}],
    }
    report["blockers"].append(
        {
            "component": "availability_records",
            "code": "availability_records_not_bound",
        }
    )
    refs["readiness_report"] = ProviderArtifactReference(
        "stock-data-readiness-report", _hash_payload(report), READINESS_REPORT_SCHEMA
    )
    contract = build_rqgm_provider_contract(**refs)

    assert verify_readiness_report(report, contract) is False


def test_availability_component_stays_blocked_until_calendar_cutoffs_are_verified() -> None:
    refs = _base_refs()
    panel = ["000001.SZ@2026-01-02"]
    panel_sha256 = hashlib.sha256(
        json.dumps(panel, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()
    refs["exact_panel"] = ProviderArtifactReference(
        "stock-data-exact-panel", panel_sha256, EXACT_PANEL_SCHEMA
    )
    report = _report(refs)
    _block_signed_components(report)
    receipt_id = refs["source_receipts"][0].identifier
    availability = _availability_artifact(panel, receipt_id)
    report["components"]["availability_records"] = {
        "ready": True,
        "blockers": [],
        "artifact": {
            "kind": "stock-data-availability-records",
            "identifier": _hash_payload(availability),
            "schema_version": COMPONENT_SCHEMAS["availability_records"],
        },
        "source_receipt_ids": [receipt_id],
        "availability_artifact": availability,
    }
    refs["readiness_report"] = ProviderArtifactReference(
        "stock-data-readiness-report",
        _hash_payload(report),
        READINESS_REPORT_SCHEMA,
    )
    contract = build_rqgm_provider_contract(**refs)

    with pytest.raises(ValueError, match="verified companion calendar cutoffs"):
        verify_readiness_report(report, contract)


def test_report_tampering_is_rejected_before_component_use() -> None:
    contract, report = _contract_and_report()
    report["request"]["panel_size"] = 2

    with pytest.raises(ValueError, match="content identity mismatch"):
        verify_readiness_report(report, contract)
