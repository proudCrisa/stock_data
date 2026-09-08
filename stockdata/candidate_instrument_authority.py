"""Reviewed issuer and rule sources for dynamic candidate ETFs."""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from .authority import verify_authority_envelope
from .local_daily_snapshot import verify_candidate_profile
from .market_rules import ETF_RULE_SCOPES
from .ticker import normalize


SCHEMA_VERSION = "stockdata-candidate-instrument-authority/1"
REVIEW_RECEIPT_SCHEMA = "stockdata-candidate-instrument-review-receipt/1"
_SCOPE_FIELDS = {
    "fund_type", "classification_source", "rule_source", "effective_from",
    "exchange", "t_plus_one", "price_limit_up", "price_limit_down",
    "lot_size", "price_tick",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _source(value, *, url, cutoff):
    if not isinstance(value, dict) or set(value) != {"url", "raw_base64", "receipt"}:
        raise ValueError("candidate instrument source closure is incomplete")
    parsed = urlparse(value["url"])
    if value["url"] != url or parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("candidate instrument source URL differs")
    try:
        raw = base64.b64decode(value["raw_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate instrument source bytes are invalid") from exc
    receipt = value["receipt"]
    if not isinstance(receipt, dict) or set(receipt) != {
            "observed_at", "request", "response"}:
        raise ValueError("candidate instrument source receipt is incomplete")
    try:
        observed = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
        frozen = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("candidate instrument source timestamp is invalid") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if observed.utcoffset() is None or frozen.utcoffset() is None or observed >= frozen \
            or receipt["request"] != {"method": "GET", "url": url} \
            or receipt["response"] != {
                "status_code": 200, "sha256": digest, "bytes": len(raw)}:
        raise ValueError("candidate instrument source receipt differs")
    return digest


def verify_candidate_instrument_authority(value, *, decision_cutoff, registry=None):
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "review_attestation", "candidate_profile", "instruments",
            "review_receipts", "review_envelope", "authority_sha256"}:
        raise ValueError("candidate instrument authority schema differs")
    unsigned = {key: item for key, item in value.items()
                if key != "authority_sha256"}
    if value["schema_version"] != SCHEMA_VERSION \
            or value["review_attestation"] != \
            "enrolled-publisher-reviewed-issuer-and-rule-sources" \
            or value["authority_sha256"] != _hash(unsigned):
        raise ValueError("candidate instrument authority identity differs")
    profile = value["candidate_profile"]
    verify_candidate_profile(
        profile, expected_sha256=profile["profile_sha256"],
        symbols=profile["required_symbols"], asof=profile["asof"])
    candidates = {item["symbol"] for item in profile["candidates"]}
    expected = candidates - set(ETF_RULE_SCOPES)
    instruments = value["instruments"]
    if not isinstance(instruments, list) or [row.get("symbol") for row in instruments] \
            != sorted(expected):
        raise ValueError("candidate instrument authority differs exact dynamic candidates")
    scopes = {}
    expected_receipts = {}
    for row in instruments:
        if not isinstance(row, dict) or set(row) != {
                "symbol", "instrument_class", "rule_scope",
                "classification_evidence", "rule_evidence"}:
            raise ValueError("candidate instrument authority row differs")
        symbol = row["symbol"]
        scope = row["rule_scope"]
        if normalize(symbol) != symbol or row["instrument_class"] != "etf" \
                or not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS \
                or scope["exchange"] != symbol[-2:] \
                or scope["lot_size"] != 100 or scope["price_tick"] != .001 \
                or type(scope["t_plus_one"]) is not bool \
                or scope["price_limit_up"] not in {.1, .2} \
                or scope["price_limit_down"] != scope["price_limit_up"]:
            raise ValueError("candidate instrument rule scope is invalid")
        for kind, evidence, url in (
                ("classification", row["classification_evidence"],
                 scope["classification_source"]),
                ("rule", row["rule_evidence"], scope["rule_source"])):
            digest = _source(evidence, url=url, cutoff=decision_cutoff)
            receipt = {
                "schema_version": REVIEW_RECEIPT_SCHEMA, "symbol": symbol,
                "source_kind": kind, "source_url": url,
                "observed_at": evidence["receipt"]["observed_at"],
                "response_sha256": digest,
                "rule_scope_sha256": _hash(scope),
            }
            expected_receipts[_hash(receipt)] = receipt
        scopes[symbol] = deepcopy(scope)
    if value["review_receipts"] != expected_receipts:
        raise ValueError("candidate instrument review receipt closure differs")
    if registry is not None:
        reviewed = {key: item for key, item in value.items()
                    if key not in {"review_envelope", "authority_sha256"}}
        accepted = verify_authority_envelope(
            value["review_envelope"], registry=registry,
            expected_component="market_rules",
            expected_artifact={
                "kind": "stock-data-candidate-instrument-authority",
                "identifier": _hash(reviewed), "schema_version": SCHEMA_VERSION,
            }, expected_source_receipt_ids=sorted(expected_receipts))
        effective = datetime.fromisoformat(accepted.effective_at)
        available = datetime.fromisoformat(accepted.available_at)
        cutoff = datetime.fromisoformat(decision_cutoff.replace("Z", "+00:00"))
        source_observed = max(datetime.fromisoformat(receipt["observed_at"])
                              for receipt in expected_receipts.values())
        if min(effective, available) < source_observed:
            raise ValueError("candidate instrument review predates source evidence")
        if max(effective, available) >= cutoff:
            raise ValueError("candidate instrument review is not pre-decision")
    return {"profile": deepcopy(profile), "scopes": scopes,
            "authority_sha256": value["authority_sha256"],
            "reviewer_key_id": (accepted.publisher_key_id if registry is not None else None)}


def load_candidate_instrument_authority(path, *, decision_cutoff, registry):
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("candidate instrument authority is not JSON") from exc
    if raw != _canonical(value):
        raise ValueError("candidate instrument authority bytes are not canonical")
    verify_candidate_instrument_authority(
        value, decision_cutoff=decision_cutoff, registry=registry)
    return value
