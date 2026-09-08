from copy import deepcopy
import base64
import hashlib

import pytest

from stockdata.candidate_instrument_authority import (
    SCHEMA_VERSION, verify_candidate_instrument_authority,
)
from stockdata.liquidity_amount_product import (
    _hash, build_liquidity_amounts_product, verify_liquidity_amounts_product,
)
from test_liquidity_amount_product import _receipt, _sessions


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


def candidate_authority():
    classification = "https://issuer.example/512480/profile"
    rules = "https://www.sse.com.cn/rules/etf"
    body = {
        "schema_version": SCHEMA_VERSION,
        "review_attestation": "enrolled-publisher-reviewed-issuer-and-rule-sources",
        "candidate_profile": candidate_profile(),
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
    return {**body, "authority_sha256": _hash(body)}


def _reseal(value):
    value["authority_sha256"] = _hash({key: item for key, item in value.items()
                                       if key != "authority_sha256"})


def test_dynamic_authority_and_liquidity_replay_exact_two_candidate_panel():
    authority = candidate_authority()
    verified = verify_candidate_instrument_authority(
        authority, decision_cutoff=CUTOFF)
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
