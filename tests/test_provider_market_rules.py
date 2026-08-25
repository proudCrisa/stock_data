from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stockdata.authority import (
    AUTHORITY_ENVELOPE_SCHEMA,
    SIGNER_ENROLLMENT_SCHEMA,
    TRUST_REGISTRY_SCHEMA,
    load_enrolled_trust_registry,
)
from stockdata.market_rules import (
    MARKET_RULE_PAYLOAD_SCHEMA,
    validate_market_rule_payload,
)
from stockdata.provider_authority_admission import (
    GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA,
    PreregisteredGenericMarketRulebook,
    SOURCE_RECEIPT_SCHEMA,
    admit_signed_component_authority,
    preregister_generic_market_rulebook,
)
from stockdata.rqgm_provider_contract import COMPONENT_SCHEMAS

DAY = "2026-08-13"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _public(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _key_id(private_key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_public(private_key)).hexdigest()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _rule(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": MARKET_RULE_PAYLOAD_SCHEMA,
        "policy_id": "cn-a-share-main-sz-2026-v1",
        "source": "official-exchange-rulebook",
        "source_sha256": hashlib.sha256(b"dated rulebook bytes").hexdigest(),
        "security_type": "A_SHARE",
        "board": "MAIN",
        "exchange": "SZ",
        "effective_from": "2026-08-13",
        "effective_until": "2026-08-13",
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
    value.update(overrides)
    return value


def _registry(tmp_path, root: Ed25519PrivateKey, signer: Ed25519PrivateKey):
    authorization = {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": 1,
        "publisher_key_id": _key_id(signer),
        "publisher_public_key_base64": _b64(_public(signer)),
        "trust_root_id": _key_id(root),
        "component_roles": ["instrument_status", "market_rules"],
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
                "component_roles": ["instrument_status", "market_rules"],
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
        path,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _signed_fixture(
    tmp_path,
    payload: dict[str, object] | None = None,
    *,
    entries: list[tuple[str, dict[str, object]]] | None = None,
    status_by_panel: dict[str, dict[str, object]] | None = None,
    receipt_source: str | None = None,
    receipt_response_sha256: str | None = None,
    receipt_observed_at: str = "2026-08-13T08:00:00+08:00",
    envelope_effective_at: str = "2026-08-13T00:00:00+08:00",
    envelope_available_at: str = "2026-08-13T08:00:00+08:00",
):
    if entries is None:
        if payload is None:
            raise ValueError("payload or entries is required")
        entries = [("000001.SZ@2026-08-13", payload)]
    panel = sorted(entry for entry, _ in entries)
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    registry = _registry(tmp_path, root, signer)
    first_payload = entries[0][1]
    source = receipt_source or str(
        first_payload.get("source", "official-exchange-rulebook")
    )
    response_sha256 = receipt_response_sha256 or str(
        first_payload.get(
            "source_sha256",
            hashlib.sha256(b"official response").hexdigest(),
        )
    )
    records = []
    bindings = []
    for entry, entry_payload in sorted(entries):
        day = entry.split("@")[1]
        record_hash = hashlib.sha256(_canonical(entry_payload)).hexdigest()
        records.append(
            {
                "panel_entry": entry,
                "payload": entry_payload,
                "record_sha256": record_hash,
                "source_receipt_ids": [],
                "effective_at": f"{day}T00:00:00+08:00",
                "available_at": f"{day}T08:00:00+08:00",
            }
        )
        bindings.append(
            {
                "component": "market_rules",
                "panel_entry": entry,
                "record_sha256": record_hash,
            }
        )
    receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": source,
        "observed_at": receipt_observed_at,
        "response_sha256": response_sha256,
        "bindings": bindings,
    }
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    for record in records:
        record["source_receipt_ids"] = [receipt_id]
    artifact = {
        "schema_version": COMPONENT_SCHEMAS["market_rules"],
        "component": "market_rules",
        "panel": panel,
        "records": records,
    }
    artifact_reference = {
        "kind": "stock-data-market-rules",
        "identifier": hashlib.sha256(_canonical(artifact)).hexdigest(),
        "schema_version": COMPONENT_SCHEMAS["market_rules"],
    }
    envelope_payload = {
        "component_role": "market_rules",
        "artifact": artifact_reference,
        "source_receipt_ids": [receipt_id],
        "effective_at": envelope_effective_at,
        "available_at": envelope_available_at,
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
    status_source = "official-instrument-status"
    status_response_sha256 = hashlib.sha256(b"instrument status response").hexdigest()
    status_records = []
    status_bindings = []
    statuses = status_by_panel or {}
    for entry, _ in sorted(entries):
        status_payload = deepcopy(
            statuses.get(
                entry,
                {
                    "is_st": False,
                    "is_suspended": False,
                    "listing_status": "listed",
                },
            )
        )
        status_record_hash = hashlib.sha256(_canonical(status_payload)).hexdigest()
        status_records.append(
            {
                "panel_entry": entry,
                "payload": status_payload,
                "record_sha256": status_record_hash,
                "source_receipt_ids": [],
                "effective_at": f"{entry.split('@')[1]}T00:00:00+08:00",
                "available_at": f"{entry.split('@')[1]}T08:00:00+08:00",
            }
        )
        status_bindings.append(
            {
                "component": "instrument_status",
                "panel_entry": entry,
                "record_sha256": status_record_hash,
            }
        )
    status_receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": status_source,
        "observed_at": "2026-08-13T08:00:00+08:00",
        "response_sha256": status_response_sha256,
        "bindings": status_bindings,
    }
    status_receipt_id = hashlib.sha256(_canonical(status_receipt)).hexdigest()
    for record in status_records:
        record["source_receipt_ids"] = [status_receipt_id]
    status_artifact = {
        "schema_version": COMPONENT_SCHEMAS["instrument_status"],
        "component": "instrument_status",
        "panel": panel,
        "records": status_records,
    }
    status_reference = {
        "kind": "stock-data-instrument-status",
        "identifier": hashlib.sha256(_canonical(status_artifact)).hexdigest(),
        "schema_version": COMPONENT_SCHEMAS["instrument_status"],
    }
    status_envelope_payload = {
        "component_role": "instrument_status",
        "artifact": status_reference,
        "source_receipt_ids": [status_receipt_id],
        "effective_at": "2026-08-13T00:00:00+08:00",
        "available_at": "2026-08-13T08:00:00+08:00",
        "publisher_key_id": _key_id(signer),
        "trust_root_id": _key_id(root),
        "trust_registry_sha256": registry.registry_sha256,
    }
    status_envelope = {
        "schema_version": AUTHORITY_ENVELOPE_SCHEMA,
        "algorithm": "ed25519",
        "payload": status_envelope_payload,
        "signature_base64": _b64(
            signer.sign(_canonical(status_envelope_payload))
        ),
    }
    return (
        panel,
        registry,
        artifact,
        envelope,
        receipt_id,
        receipt,
        (status_artifact, status_envelope, status_receipt_id, status_receipt),
    )


def _admit(fixture):
    panel, registry, artifact, envelope, receipt_id, receipt, status = fixture
    status_artifact, status_envelope, status_receipt_id, status_receipt = status
    status_admitted = admit_signed_component_authority(
        component="instrument_status",
        artifact_value=status_artifact,
        authority_envelope=status_envelope,
        expected_panel=panel,
        bound_source_receipts={status_receipt_id: status_receipt},
        registry=registry,
        decision_cutoff_by_panel={
            entry: f"{entry.split('@')[1]}T09:25:00+08:00" for entry in panel
        },
    )
    return admit_signed_component_authority(
        component="market_rules",
        artifact_value=artifact,
        authority_envelope=envelope,
        expected_panel=panel,
        bound_source_receipts={receipt_id: receipt},
        registry=registry,
        decision_cutoff_by_panel={
            entry: f"{entry.split('@')[1]}T09:25:00+08:00" for entry in panel
        },
        instrument_status_authority=status_admitted,
    )


def _generic_fixture(
    tmp_path,
    *,
    ranges: dict[bool, list[tuple[int, int | None]]] | None = None,
    payload_source: str = "official-exchange-rulebook",
    receipt_source: str | None = None,
    payload_response_sha256: str | None = None,
    receipt_response_sha256: str | None = None,
    payload_overrides: dict[str, object] | None = None,
):
    panel = ["000001.SZ@2026-08-13"]
    root = Ed25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    registry = _registry(tmp_path, root, signer)
    ranges = ranges or {False: [(0, None)], True: [(0, None)]}
    response_sha256 = payload_response_sha256 or hashlib.sha256(
        b"generic rulebook response"
    ).hexdigest()
    records = []
    for is_st in (False, True):
        for listing_age_min, listing_age_max in ranges.get(is_st, []):
            payload = _rule(
                policy_id=(
                    f"generic-{DAY}-{'st' if is_st else 'nonst'}-"
                    f"{listing_age_min}-{listing_age_max or 'open'}"
                ),
                source=payload_source,
                source_sha256=response_sha256,
                is_st=is_st,
                listing_age_min=listing_age_min,
                listing_age_max=listing_age_max,
            )
            if payload_overrides:
                payload.update(payload_overrides)
            records.append(
                {
                    "panel_entry": panel[0],
                    "payload": payload,
                    "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
                    "source_receipt_ids": [],
                    "effective_at": f"{DAY}T00:00:00+08:00",
                    "available_at": f"{DAY}T08:00:00+08:00",
                }
            )
    records.sort(key=lambda record: (record["panel_entry"], record["record_sha256"]))
    artifact = {
        "schema_version": COMPONENT_SCHEMAS["market_rules"],
        "component": "market_rules",
        "panel": panel,
        "records": records,
    }
    receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": receipt_source or payload_source,
        "observed_at": f"{DAY}T08:00:00+08:00",
        "response_sha256": receipt_response_sha256 or response_sha256,
        "bindings": [
            {
                "component": "market_rules",
                "panel_entry": record["panel_entry"],
                "record_sha256": record["record_sha256"],
            }
            for record in records
        ],
    }
    receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    for record in records:
        record["source_receipt_ids"] = [receipt_id]
    artifact_reference = {
        "kind": "stock-data-market-rules",
        "identifier": hashlib.sha256(_canonical(artifact)).hexdigest(),
        "schema_version": COMPONENT_SCHEMAS["market_rules"],
    }
    envelope_payload = {
        "component_role": "market_rules",
        "artifact": artifact_reference,
        "source_receipt_ids": [receipt_id],
        "effective_at": f"{DAY}T00:00:00+08:00",
        "available_at": f"{DAY}T08:00:00+08:00",
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
    return {
        "panel": panel,
        "registry": registry,
        "artifact": artifact,
        "envelope": envelope,
        "receipt_id": receipt_id,
        "receipt": receipt,
        "signer": signer,
        "cutoffs": {panel[0]: f"{DAY}T09:25:00+08:00"},
    }


def _admit_generic(fixture):
    return preregister_generic_market_rulebook(
        artifact_value=fixture["artifact"],
        authority_envelope=fixture["envelope"],
        expected_panel=fixture["panel"],
        bound_source_receipts={fixture["receipt_id"]: fixture["receipt"]},
        registry=fixture["registry"],
        decision_cutoff_by_panel=fixture["cutoffs"],
    )


def test_generic_rulebook_is_a_registration_prerequisite_only(tmp_path) -> None:
    fixture = _generic_fixture(tmp_path)
    prerequisite = _admit_generic(fixture)

    assert isinstance(prerequisite, PreregisteredGenericMarketRulebook)
    evidence = prerequisite.prerequisite_evidence()
    assert evidence["schema_version"] == GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA
    assert "ready" not in evidence
    assert "execution_rule_selection" not in evidence


@pytest.mark.parametrize(
    ("ranges", "message"),
    [
        ({False: [], True: [(0, None)]}, "omit the is_st=False branch"),
        ({False: [(0, None)], True: []}, "omit the is_st=True branch"),
        (
            {False: [(0, 4), (6, None)], True: [(0, None)]},
            "listing-age coverage is incomplete",
        ),
        (
            {False: [(0, None), (1, None)], True: [(0, None)]},
            "listing-age coverage overlaps",
        ),
    ],
)
def test_generic_rulebook_requires_complete_non_overlapping_state_coverage(
    tmp_path, ranges, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _admit_generic(_generic_fixture(tmp_path, ranges=ranges))


@pytest.mark.parametrize(
    "case",
    ["signature", "receipt", "source", "board"],
)
def test_generic_rulebook_rejects_authority_and_semantic_drift(tmp_path, case: str) -> None:
    if case == "source":
        fixture = _generic_fixture(tmp_path, receipt_source="different-rulebook")
    elif case == "board":
        fixture = _generic_fixture(
            tmp_path,
            payload_overrides={"board": "CHINEXT", "exchange": "SZ"},
        )
    else:
        fixture = _generic_fixture(tmp_path)
    if case == "signature":
        fixture["envelope"]["signature_base64"] = _b64(b"0" * 64)
    elif case == "receipt":
        fixture["artifact"]["records"][0]["source_receipt_ids"] = ["0" * 64]

    with pytest.raises(ValueError):
        _admit_generic(fixture)


def test_admits_complete_rule_and_binds_full_canonical_artifact(tmp_path) -> None:
    fixture = _signed_fixture(tmp_path, _rule())

    admitted = _admit(fixture)
    artifact = fixture[2]

    assert (
        admitted.artifact.identifier == hashlib.sha256(_canonical(artifact)).hexdigest()
    )
    changed = deepcopy(artifact)
    changed["records"][0]["payload"]["minimum_commission"] = 4.0
    assert (
        hashlib.sha256(_canonical(changed)).hexdigest() != admitted.artifact.identifier
    )


def test_market_rules_require_admitted_instrument_status(tmp_path) -> None:
    panel, registry, artifact, envelope, receipt_id, receipt, _ = _signed_fixture(
        tmp_path, _rule()
    )

    with pytest.raises(ValueError, match="requires exact admitted instrument_status"):
        admit_signed_component_authority(
            component="market_rules",
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=panel,
            bound_source_receipts={receipt_id: receipt},
            registry=registry,
            decision_cutoff_by_panel={panel[0]: f"{DAY}T09:25:00+08:00"},
        )


@pytest.mark.parametrize(
    ("panel_entry", "overrides"),
    [
        ("300750.SZ@2026-08-13", {"board": "MAIN", "exchange": "SZ"}),
        ("688981.SH@2026-08-13", {"board": "MAIN", "exchange": "SH"}),
        ("000001.SZ@2026-08-13", {"board": "MAIN", "exchange": "SH"}),
    ],
)
def test_market_rules_reject_wrong_board_or_exchange(
    tmp_path, panel_entry: str, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="board|exchange"):
        _admit(
            _signed_fixture(
                tmp_path,
                entries=[(panel_entry, _rule(**overrides))],
            )
        )


@pytest.mark.parametrize(
    ("panel_entry", "overrides"),
    [
        ("300750.SZ@2026-08-13", {"board": "CHINEXT", "exchange": "SZ"}),
        ("688981.SH@2026-08-13", {"board": "STAR", "exchange": "SH"}),
    ],
)
def test_market_rules_admit_exact_board_with_signed_status(
    tmp_path, panel_entry: str, overrides: dict[str, object]
) -> None:
    assert (
        _admit(
            _signed_fixture(
                tmp_path,
                entries=[(panel_entry, _rule(**overrides))],
            )
        ).component
        == "market_rules"
    )


def test_market_rules_reject_st_exception_mismatch(tmp_path) -> None:
    entry = "000001.SZ@2026-08-13"

    with pytest.raises(ValueError, match="ST exception differs"):
        _admit(
            _signed_fixture(
                tmp_path,
                entries=[(entry, _rule(is_st=False))],
                status_by_panel={
                    entry: {
                        "is_st": True,
                        "is_suspended": False,
                        "listing_status": "listed",
                    }
                },
            )
        )


@pytest.mark.parametrize(
    "status",
    [
        {"is_st": False, "is_suspended": True, "listing_status": "suspended"},
        {"is_st": False, "is_suspended": False, "listing_status": "delisted"},
    ],
)
def test_market_rules_reject_suspended_or_nonlisted_status(
    tmp_path, status: dict[str, object]
) -> None:
    entry = "000001.SZ@2026-08-13"

    with pytest.raises(ValueError, match="non-tradable instrument status"):
        _admit(
            _signed_fixture(
                tmp_path,
                entries=[(entry, _rule())],
                status_by_panel={entry: status},
            )
        )


def test_fully_signed_policy_id_only_payload_is_rejected(tmp_path) -> None:
    fixture = _signed_fixture(
        tmp_path,
        {
            "schema_version": MARKET_RULE_PAYLOAD_SCHEMA,
            "policy_id": "self-reported-policy",
        },
    )

    with pytest.raises(ValueError, match="payload is incomplete"):
        _admit(fixture)


@pytest.mark.parametrize(
    ("receipt_source", "receipt_response_sha256"),
    [
        ("different-rulebook", None),
        (None, hashlib.sha256(b"different response").hexdigest()),
    ],
)
def test_payload_source_must_match_each_bound_receipt(
    tmp_path,
    receipt_source: str | None,
    receipt_response_sha256: str | None,
) -> None:
    fixture = _signed_fixture(
        tmp_path,
        _rule(),
        receipt_source=receipt_source,
        receipt_response_sha256=receipt_response_sha256,
    )

    with pytest.raises(ValueError, match="source differs"):
        _admit(fixture)


def test_signed_artifact_byte_drift_is_rejected_by_admission(tmp_path) -> None:
    fixture = list(_signed_fixture(tmp_path, _rule()))
    envelope = deepcopy(fixture[3])
    envelope["signature_base64"] = _b64(b"0" * 64)
    fixture[3] = envelope

    with pytest.raises(ValueError, match="signature"):
        _admit(tuple(fixture))


@pytest.mark.parametrize(
    "fixture_kwargs",
    [
        {"receipt_observed_at": "2026-08-13T09:25:00+08:00"},
        {"envelope_available_at": "2026-08-13T09:25:00+08:00"},
        {"envelope_effective_at": "2026-08-13T09:25:00+08:00"},
    ],
)
def test_receipt_and_envelope_times_must_precede_cutoff(
    tmp_path,
    fixture_kwargs: dict[str, str],
) -> None:
    fixture = _signed_fixture(tmp_path, _rule(), **fixture_kwargs)

    with pytest.raises(ValueError, match="post-cutoff"):
        _admit(fixture)


def test_same_policy_id_cannot_name_different_canonical_rules(tmp_path) -> None:
    entries = [
        ("000001.SZ@2026-08-13", _rule()),
        ("000002.SZ@2026-08-13", _rule(minimum_commission=4.0)),
    ]

    with pytest.raises(ValueError, match="policy_id maps"):
        _admit(_signed_fixture(tmp_path, entries=entries))


@pytest.mark.parametrize(
    ("second_from", "expected_error"),
    [
        ("2026-08-14", "overlap"),
        ("2026-08-16", "gap"),
    ],
)
def test_signed_policy_regime_overlap_or_gap_is_rejected(
    tmp_path,
    second_from: str,
    expected_error: str,
) -> None:
    entries = [
        (
            "000001.SZ@2026-08-13",
            _rule(
                policy_id="policy-a",
                effective_from="2026-08-13",
                effective_until="2026-08-14",
            ),
        ),
        (
            f"000002.SZ@{second_from}",
            _rule(
                policy_id="policy-b",
                effective_from=second_from,
                effective_until=second_from,
            ),
        ),
    ]

    with pytest.raises(ValueError, match=expected_error):
        _admit(_signed_fixture(tmp_path, entries=entries))


def test_signed_policy_selector_overlap_is_rejected(tmp_path) -> None:
    entries = [
        (
            "000001.SZ@2026-08-13",
            _rule(
                policy_id="policy-a",
                listing_age_min=0,
                listing_age_max=10,
                is_st=False,
            ),
        ),
        (
            "000002.SZ@2026-08-13",
            _rule(
                policy_id="policy-b",
                listing_age_min=5,
                listing_age_max=20,
                is_st=False,
            ),
        ),
    ]

    with pytest.raises(ValueError, match="overlap"):
        _admit(
            _signed_fixture(
                tmp_path,
                entries=entries,
            )
        )


def test_adjacent_signed_policy_regimes_are_admitted(tmp_path) -> None:
    entries = [
        (
            "000001.SZ@2026-08-13",
            _rule(
                policy_id="policy-a",
                effective_from="2026-08-13",
                effective_until="2026-08-14",
            ),
        ),
        (
            "000002.SZ@2026-08-15",
            _rule(
                policy_id="policy-b",
                effective_from="2026-08-15",
                effective_until="2026-08-15",
            ),
        ),
    ]

    assert (
        _admit(_signed_fixture(tmp_path, entries=entries)).component == "market_rules"
    )


def test_signed_rule_outside_its_regime_is_rejected(tmp_path) -> None:
    fixture = _signed_fixture(
        tmp_path,
        _rule(effective_from="2026-08-14", effective_until="2026-08-14"),
    )

    with pytest.raises(ValueError, match="does not cover panel date"):
        _admit(fixture)


@pytest.mark.parametrize("field", sorted(_rule()))
def test_every_rule_payload_field_is_required(field: str) -> None:
    payload = _rule()
    payload.pop(field)

    with pytest.raises(ValueError, match="payload is incomplete"):
        validate_market_rule_payload(
            payload,
            panel_entry="000001.SZ@2026-08-13",
        )


def test_unknown_rule_payload_field_is_rejected() -> None:
    payload = _rule(undeclared_policy_hint="ignored")

    with pytest.raises(ValueError, match="payload is incomplete"):
        validate_market_rule_payload(
            payload,
            panel_entry="000001.SZ@2026-08-13",
        )


@pytest.mark.parametrize(
    ("overrides", "panel_entry"),
    [
        ({}, "000001.SZ@2026-08-13"),
        (
            {
                "effective_from": "2026-08-12",
                "effective_until": "2026-08-13",
            },
            "000001.SZ@2026-08-13",
        ),
        (
            {
                "effective_from": "2026-08-13",
                "effective_until": "2026-08-14",
            },
            "000001.SZ@2026-08-13",
        ),
        (
            {"board": "STAR", "exchange": "SH"},
            "688981.SH@2026-08-13",
        ),
        (
            {"board": "CHINEXT", "exchange": "SZ"},
            "300750.SZ@2026-08-13",
        ),
        (
            {"board": "BSE", "exchange": "BJ"},
            "920001.BJ@2026-08-13",
        ),
        (
            {
                "listing_age_min": 0,
                "listing_age_max": 4,
                "is_st": None,
                "price_limit_up": None,
                "price_limit_down": None,
            },
            "000001.SZ@2026-08-13",
        ),
        (
            {
                "is_st": True,
                "price_limit_up": 0.05,
                "price_limit_down": 0.05,
            },
            "000001.SZ@2026-08-13",
        ),
        (
            {"price_limit_up": 0.0, "price_limit_down": 1.0},
            "000001.SZ@2026-08-13",
        ),
    ],
)
def test_rule_boundaries_and_exception_regimes_are_accepted(
    overrides: dict[str, object],
    panel_entry: str,
) -> None:
    payload = _rule(**overrides)

    assert validate_market_rule_payload(payload, panel_entry=panel_entry) is payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": "stockdata-market-rule-payload/0"},
        {"policy_id": ""},
        {"source": ""},
        {"source_sha256": "0"},
        {"security_type": "ETF"},
        {"board": "STAR", "exchange": "SZ"},
        {"effective_from": "2026-08-14"},
        {"effective_until": "2026-08-12"},
        {"listing_age_min": -1},
        {"listing_age_min": 5, "listing_age_max": 4},
        {"is_st": "false"},
        {"lot_size": 99},
        {"t_plus_one": False},
        {"reject_suspended": False},
        {"reject_zero_volume": False},
        {"price_limit_up": None},
        {"price_limit_up": -0.01, "price_limit_down": -0.01},
        {"price_limit_up": 1.01, "price_limit_down": 1.01},
        {"price_limit_reference": "PREVIOUS_CLOSE"},
        {"price_tick": 0.02},
        {"price_rounding": "RAW"},
        {"locked_limit_order_policy": "FILL"},
        {"commission_rate": -0.01},
        {"minimum_commission": float("nan")},
        {"transfer_fee_rate": float("inf")},
        {"stamp_duty_sell_rate": True},
        {"slippage_model": "CLOSE_BPS"},
        {"slippage_bps": -1.0},
        {"slippage_bounds": "BAR_ONLY"},
        {"time_in_force": "GTC"},
        {"cancel_unfilled_at_close": False},
    ],
)
def test_invalid_rule_boundaries_fail_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_market_rule_payload(
            _rule(**overrides),
            panel_entry="000001.SZ@2026-08-13",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"effective_from": "2026-08-14", "effective_until": "2026-08-14"},
        {"effective_from": "2026-08-12", "effective_until": "2026-08-12"},
        {"board": "MAIN", "exchange": "SH"},
    ],
)
def test_rule_must_cover_panel_date_and_exchange(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        validate_market_rule_payload(
            _rule(**overrides),
            panel_entry="000001.SZ@2026-08-13",
        )
