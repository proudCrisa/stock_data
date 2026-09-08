from copy import deepcopy
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stockdata.authority import ALGORITHM, AUTHORITY_ENVELOPE_SCHEMA, TRUST_REGISTRY_SCHEMA, load_enrolled_trust_registry_bytes
from stockdata.candidate_instrument_authority import (
    SCHEMA_VERSION, verify_candidate_instrument_authority,
)
from stockdata.liquidity_amount_product import (
    _canonical, _hash, build_liquidity_amounts_product, verify_liquidity_amounts_product,
)
from test_liquidity_amount_product import _receipt, _sessions
from test_provider_authority_publisher import _b64, _enrollment, _key_id, _root_entry


ASOF = _sessions()[-1]
CUTOFF = "2026-08-31T09:25:00+08:00"


def candidate_profile():
    body = {
        "schema_version": "trading-candidate-local-daily-profile/1",
        "asof": ASOF, "decision_authority": False,
        "purpose": "formal-validation-input",
        "required_symbols": ["000300.SH", "512480.SH", "561980.SH"],
        "benchmark_symbols": ["000300.SH"],
        "candidates": [{
            "code": code, "symbol": symbol, "instrument_class": "etf",
            "promoted_asof": ASOF,
            "observation_contract": "candidate-observation-v1",
            "promotion_rule_version": "ma20-observation-window/2",
            "promotion_evidence_id": f"promotion_{code}",
            "promotion_content_hash": char * 64,
        } for code, symbol, char in (
            ("sh512480", "512480.SH", "b"),
            ("sh561980", "561980.SH", "c"),
        )],
        "source_authorization": {
            "producer": "trading-agent", "code_revision": "a" * 40,
            "candidate_state_sha256": "d" * 64, "scan_asof": ASOF,
            "scan_sha256": "e" * 64, "universe_sha256": "f" * 64,
        },
    }
    return {**body, "profile_sha256": _hash(body)}


def _source(url, raw):
    encoded = base64.b64encode(raw).decode("ascii")
    return {"url": url, "raw_base64": encoded, "receipt": {
        "observed_at": "2026-08-28T16:00:00+08:00",
        "request": {"method": "GET", "url": url},
        "response": {"status_code": 200,
                     "sha256": hashlib.sha256(raw).hexdigest(),
                     "bytes": len(raw)},
    }}


def candidate_registry():
    root = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    signer = Ed25519PrivateKey.from_private_bytes(bytes([2]) * 32)
    reviewer = Ed25519PrivateKey.from_private_bytes(bytes([3]) * 32)
    roles = sorted(["liquidity_amounts", "global_signals", "trading_calendar",
                    "instrument_status", "market_rules", "universe",
                    "corporate_actions"])
    value = {"schema_version": TRUST_REGISTRY_SCHEMA, "registry_version": 1,
             "trust_roots": [_root_entry(root)],
             "signer_enrollments": [
                 _enrollment(root, signer, roles=roles),
                 _enrollment(root, reviewer, roles=["market_rules"])]}
    value["signer_enrollments"].sort(key=lambda row: row["publisher_key_id"])
    return value, _hash(value), root, signer, reviewer


def candidate_authority(*, registry_sha=None, root=None, reviewer=None, profile=None):
    if registry_sha is None:
        _, registry_sha, root, _, reviewer = candidate_registry()
    classification = "https://issuer.example/512480/profile"
    rules = "https://www.sse.com.cn/rules/etf"
    reviewed = {
        "schema_version": SCHEMA_VERSION,
        "review_attestation": "enrolled-publisher-reviewed-issuer-and-rule-sources",
        "candidate_profile": deepcopy(profile or candidate_profile()),
        "instruments": [{
            "symbol": "512480.SH", "instrument_class": "etf",
            "rule_scope": {
                "fund_type": "DOMESTIC_EQUITY",
                "classification_source": classification,
                "rule_source": rules, "effective_from": "2026-07-06",
                "exchange": "SH", "t_plus_one": True,
                "price_limit_up": .1, "price_limit_down": .1,
                "lot_size": 100, "price_tick": .001,
            },
            "classification_evidence": _source(classification, b"reviewed issuer fixture"),
            "rule_evidence": _source(rules, b"reviewed exchange rule fixture"),
        }],
    }
    scope_hash = _hash(reviewed["instruments"][0]["rule_scope"])
    receipts = {}
    for kind, evidence in (
            ("classification", reviewed["instruments"][0]["classification_evidence"]),
            ("rule", reviewed["instruments"][0]["rule_evidence"])):
        receipt = {"schema_version": "stockdata-candidate-instrument-review-receipt/1",
                   "symbol": "512480.SH", "source_kind": kind,
                   "source_url": evidence["url"],
                   "observed_at": evidence["receipt"]["observed_at"],
                   "response_sha256": evidence["receipt"]["response"]["sha256"],
                   "rule_scope_sha256": scope_hash}
        receipts[_hash(receipt)] = receipt
    reviewed["review_receipts"] = receipts
    artifact = {"kind": "stock-data-candidate-instrument-authority",
                "identifier": _hash(reviewed), "schema_version": SCHEMA_VERSION}
    envelope_payload = {
        "component_role": "market_rules", "artifact": artifact,
        "source_receipt_ids": sorted(receipts),
        "effective_at": "2026-08-28T16:00:00+08:00",
        "available_at": "2026-08-28T16:00:00+08:00",
        "publisher_key_id": _key_id(reviewer), "trust_root_id": _key_id(root),
        "trust_registry_sha256": registry_sha,
    }
    body = {**reviewed, "review_envelope": {
        "schema_version": AUTHORITY_ENVELOPE_SCHEMA, "algorithm": ALGORITHM,
        "payload": envelope_payload,
        "signature_base64": _b64(reviewer.sign(_canonical(envelope_payload))),
    }}
    return {**body, "authority_sha256": _hash(body)}


def _reseal(value):
    value["authority_sha256"] = _hash({key: item for key, item in value.items()
                                       if key != "authority_sha256"})


def test_dynamic_authority_and_liquidity_replay_exact_two_candidate_panel():
    authority = candidate_authority()
    registry_value, pin, _, _, _ = candidate_registry()
    registry = load_enrolled_trust_registry_bytes(_canonical(registry_value), expected_sha256=pin)
    verified = verify_candidate_instrument_authority(
        authority, decision_cutoff=CUTOFF, registry=registry)
    assert set(verified["scopes"]) == {"512480.SH"}
    captures = [_receipt(symbol, _sessions())
                for symbol in ("512480.SH", "561980.SH")]
    panel = [(symbol, day) for symbol in ("512480.SH", "561980.SH")
             for day in _sessions()]
    product = build_liquidity_amounts_product(
        captures, panel=panel, decision_cutoff=CUTOFF,
        expected_watermark=ASOF,
        candidate_instrument_authority=authority)
    assert product["instrument_scope"]["codes"] == ["512480.SH", "561980.SH"]
    assert product["candidate_instrument_authority"] == authority
    assert verify_liquidity_amounts_product(product) == product


@pytest.mark.parametrize("mutation", ["extra", "unknown", "type", "source", "receipt"])
def test_dynamic_authority_rejects_resealed_scope_or_source_drift(mutation):
    authority = candidate_authority()
    row = authority["instruments"][0]
    if mutation == "extra":
        authority["instruments"].append(deepcopy(row))
    elif mutation == "unknown":
        row["symbol"] = "512500.SH"
    elif mutation == "type":
        row["instrument_class"] = "stock"
    elif mutation == "source":
        row["rule_scope"]["classification_source"] = "https://evil.example/fund"
    else:
        row["classification_evidence"]["receipt"]["response"]["sha256"] = "0" * 64
    _reseal(authority)
    with pytest.raises(ValueError):
        verify_candidate_instrument_authority(authority, decision_cutoff=CUTOFF)


@pytest.mark.parametrize("mutation", ["signature", "unenrolled_resign",
                                      "future_effective_at", "predates_source"])
def test_dynamic_authority_requires_enrolled_independent_review_signature(mutation):
    authority = candidate_authority()
    registry_value, pin, root, _, _ = candidate_registry()
    registry = load_enrolled_trust_registry_bytes(
        _canonical(registry_value), expected_sha256=pin)
    if mutation == "signature":
        authority["review_envelope"]["signature_base64"] = _b64(bytes(64))
    elif mutation == "unenrolled_resign":
        unknown = Ed25519PrivateKey.from_private_bytes(bytes([4]) * 32)
        envelope = authority["review_envelope"]
        envelope["payload"]["publisher_key_id"] = _key_id(unknown)
        envelope["signature_base64"] = _b64(
            unknown.sign(_canonical(envelope["payload"])))
    elif mutation == "future_effective_at":
        reviewer = Ed25519PrivateKey.from_private_bytes(bytes([3]) * 32)
        envelope = authority["review_envelope"]
        envelope["payload"]["effective_at"] = CUTOFF
        envelope["signature_base64"] = _b64(
            reviewer.sign(_canonical(envelope["payload"])))
    else:
        reviewer = Ed25519PrivateKey.from_private_bytes(bytes([3]) * 32)
        envelope = authority["review_envelope"]
        envelope["payload"]["effective_at"] = "2026-08-28T15:59:00+08:00"
        envelope["payload"]["available_at"] = "2026-08-28T15:59:00+08:00"
        envelope["signature_base64"] = _b64(
            reviewer.sign(_canonical(envelope["payload"])))
    _reseal(authority)
    with pytest.raises(ValueError):
        verify_candidate_instrument_authority(
            authority, decision_cutoff=CUTOFF, registry=registry)
