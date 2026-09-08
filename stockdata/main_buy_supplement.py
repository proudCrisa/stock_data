"""Portable signed BUY inputs alongside the unchanged v1 price manifest."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math

from .authority import load_enrolled_trust_registry_bytes
from .liquidity_amount_product import (
    _canonical, _day, _hash, _timestamp, admit_liquidity_amounts_authority,
)
from .candidate_instrument_authority import verify_candidate_instrument_authority
from .provider_authority_admission import (
    SOURCE_RECEIPT_SCHEMA, admit_signed_component_authority,
)
from .ticker import normalize

SCHEMA_VERSION = "stockdata-main-buy-supplement/1"
REFERENCE_COMPONENTS = (
    "trading_calendar", "instrument_status", "market_rules", "universe", "corporate_actions",
)


def validate_global_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "schema_version", "asof", "quotes", "risk", "snapshot_sha256",
    } or snapshot["schema_version"] != "trading-global-snapshot/1":
        raise ValueError("global snapshot schema is invalid")
    _day(snapshot["asof"])
    unsigned = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    if snapshot["snapshot_sha256"] != _hash(unsigned):
        raise ValueError("global snapshot hash mismatch")
    if not isinstance(snapshot["quotes"], dict) or not snapshot["quotes"]:
        raise ValueError("global snapshot quotes are missing")
    for quote in snapshot["quotes"].values():
        if not isinstance(quote, dict) or not {"price", "date"} <= set(quote):
            raise ValueError("global snapshot quote is incomplete")
        if _day(quote["date"]) > snapshot["asof"]:
            raise ValueError("global snapshot quote is after asof")
        for field in ("price", "chg_pct", "chg_20d"):
            if field not in quote or (field != "price" and quote[field] is None):
                continue
            value = quote[field]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or (field == "price" and value <= 0)):
                raise ValueError("global snapshot quote has invalid numeric values")
    if not isinstance(snapshot["risk"], dict):
        raise ValueError("global snapshot risk is invalid")
    return snapshot


def build_global_signals_authority_inputs(
    snapshot: dict, *, panel: list[str], available_at: str,
    source_evidence: dict | None = None,
) -> dict:
    """Retain the complete Trading snapshot under the existing signing protocol."""
    validate_global_snapshot(snapshot)
    observed = _timestamp(available_at)
    if not panel or panel != sorted(set(panel)):
        raise ValueError("global panel must be sorted and unique")
    for entry in panel:
        symbol, day = entry.split("@")
        if normalize(symbol) != symbol or day != snapshot["asof"]:
            raise ValueError("global panel identity differs snapshot asof")
    receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": "trading-global-snapshot/1",
        "observed_at": observed.isoformat(),
        "response_sha256": _hash(snapshot),
        "bindings": [{"component": "global_signals", "panel_entry": entry,
                      "record_sha256": _hash(snapshot)} for entry in panel],
    }
    receipt_id = _hash(receipt)
    inputs = {
        "snapshot": deepcopy(snapshot),
        "artifact": {
            "schema_version": "stockdata-global-signals/1", "component": "global_signals",
            "panel": panel,
            "records": [{"panel_entry": entry, "payload": deepcopy(snapshot),
                         "record_sha256": _hash(snapshot), "source_receipt_ids": [receipt_id],
                         "effective_at": f"{snapshot['asof']}T00:00:00+08:00",
                         "available_at": observed.isoformat()} for entry in panel],
        },
        "source_receipts": {receipt_id: receipt},
    }
    if source_evidence is not None:
        evidence_receipt = {**receipt, "source": "local-publisher-source-evidence/1",
                            "response_sha256": _hash(source_evidence)}
        evidence_id = _hash(evidence_receipt)
        inputs["source_receipts"][evidence_id] = evidence_receipt
        for record in inputs["artifact"]["records"]:
            record["source_receipt_ids"] = sorted([receipt_id, evidence_id])
        inputs["source_evidence"] = deepcopy(source_evidence)
    return inputs


def _admit(component, inputs, *, panel, registry, cutoffs=None, status=None,
           current_decision_observation_cutoff=None, trusted_etf_scopes=None):
    return admit_signed_component_authority(
        component=component, artifact_value=inputs["artifact"],
        authority_envelope=inputs["authority_envelope"], expected_panel=panel,
        bound_source_receipts=inputs["source_receipts"], registry=registry,
        decision_cutoff_by_panel=cutoffs, instrument_status_authority=status,
        current_decision_observation_cutoff=current_decision_observation_cutoff,
        trusted_etf_scopes=trusted_etf_scopes,
    )


def verify_main_buy_supplement(
    payload: dict, *, expected_registry_sha256: str, provider_manifest_sha256: str,
    asof: str, decision_cutoff: str,
) -> dict:
    """Verify all embedded inputs without filesystem, network, or self-chosen pins.

    Trading must separately compare symbols to its requested price universe and
    replay the global risk calculation using its own policy implementation.
    """
    fixed_fields = {
        "schema_version", "provider_manifest_sha256", "asof", "decision_cutoff",
        "symbols", "registry", "liquidity", "global_signals", "references",
    }
    fields = set(payload) if isinstance(payload, dict) else set()
    dynamic = fields == fixed_fields | {"candidate_instrument_authority"}
    if not isinstance(payload, dict) or frozenset(fields) not in {frozenset(fixed_fields),
            frozenset(fixed_fields | {"candidate_instrument_authority"})} \
            or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("main BUY supplement schema is invalid")
    if (payload["provider_manifest_sha256"] != provider_manifest_sha256
            or payload["asof"] != _day(asof)
            or _timestamp(payload["decision_cutoff"]) > _timestamp(decision_cutoff)):
        raise ValueError("main BUY supplement differs external decision identity")
    decision_cutoff = payload["decision_cutoff"]
    cutoff = _timestamp(decision_cutoff)
    symbols = payload["symbols"]
    if (not isinstance(symbols, list) or not symbols or symbols != sorted(set(symbols))
            or any(normalize(symbol) != symbol for symbol in symbols)):
        raise ValueError("main BUY symbols must be canonical, sorted, and unique")
    trusted_etf_scopes = None
    authority_hash = None
    if dynamic:
        authority = verify_candidate_instrument_authority(
            payload["candidate_instrument_authority"],
            decision_cutoff=payload["decision_cutoff"])
        profile_symbols = sorted(item["symbol"]
                                 for item in authority["profile"]["candidates"])
        if symbols != profile_symbols:
            raise ValueError("main BUY symbols differ candidate profile")
        trusted_etf_scopes = authority["scopes"]
        authority_hash = authority["authority_sha256"]
    registry = load_enrolled_trust_registry_bytes(
        _canonical(payload["registry"]), expected_sha256=expected_registry_sha256,
    )
    current_panel = [f"{symbol}@{asof}" for symbol in symbols]
    cutoffs = {entry: decision_cutoff for entry in current_panel}
    liquidity = payload["liquidity"]
    if set(liquidity) != {"product", "artifact", "source_receipts", "authority_envelope"}:
        raise ValueError("main BUY liquidity closure is incomplete")
    product = liquidity["product"]
    if product.get("candidate_instrument_authority") != (
            payload.get("candidate_instrument_authority")):
        raise ValueError("main BUY liquidity candidate authority differs")
    if product["instrument_scope"]["codes"] != symbols:
        raise ValueError("main BUY liquidity symbol scope differs")
    liquidity_panel = product["panel"]
    references = payload["references"]
    if set(references) != set(REFERENCE_COMPONENTS):
        raise ValueError("main BUY reference authorities are incomplete")
    for inputs in references.values():
        if set(inputs) - {"source_evidence"} != {"artifact", "source_receipts", "authority_envelope"}:
            raise ValueError("main BUY reference closure is incomplete")
        if "source_evidence" in inputs:
            evidence_ids = {receipt_id for receipt_id, receipt in inputs["source_receipts"].items()
                            if receipt["response_sha256"] == _hash(inputs["source_evidence"])}
            if not all(evidence_ids.intersection(record["source_receipt_ids"])
                       for record in inputs["artifact"]["records"]):
                raise ValueError("main BUY raw reference evidence is not receipt-bound")
        if dynamic and inputs.get("source_evidence", {}).get(
                "candidate_instrument_authority_sha256") != authority_hash:
            raise ValueError("main BUY reference candidate authority is not bound")
    calendar_panel = sorted(set(liquidity_panel) | set(current_panel))
    calendar = _admit("trading_calendar", references["trading_calendar"],
                      panel=calendar_panel, registry=registry,
                      current_decision_observation_cutoff=decision_cutoff)
    for entry in current_panel:
        phases = calendar.signed_calendar_phases_by_panel[entry]
        if not (_timestamp(phases["session_close_at"]) < cutoff
                < _timestamp(phases["next_session_decision_cutoff_at"])):
            raise ValueError("main BUY cutoff is outside signed asof session finality window")
    session_days = sorted({entry.split("@")[1] for entry in calendar_panel})
    history_days = [day for day in session_days if day <= asof]
    if not history_days or history_days[-1] != asof or session_days != history_days:
        raise ValueError("main BUY calendar must end at exact asof")
    if len(history_days) < 20:
        raise ValueError("main BUY calendar requires 20 finalized sessions")
    expected_liquidity_panel = [(symbol, day) for symbol in symbols for day in history_days]
    # The signed next-session links prove contiguity; missing weekdays are not inferred.
    for symbol in symbols:
        for first, second in zip(session_days, session_days[1:]):
            phases = calendar.signed_calendar_phases_by_panel[f"{symbol}@{first}"]
            if datetime.fromisoformat(phases["next_session_decision_cutoff_at"]).date().isoformat() != second:
                raise ValueError("main BUY calendar session chain has a gap")
    admit_liquidity_amounts_authority(
        product=product, artifact_value=liquidity["artifact"],
        authority_envelope=liquidity["authority_envelope"],
        bound_source_receipts=liquidity["source_receipts"], registry=registry,
        expected_panel=expected_liquidity_panel, decision_cutoff=decision_cutoff,
        expected_watermark=history_days[-1],
    )
    status = _admit("instrument_status", references["instrument_status"],
                    panel=current_panel, registry=registry, cutoffs=cutoffs)
    _admit("market_rules", references["market_rules"], panel=current_panel,
           registry=registry, cutoffs=cutoffs, status=status,
           trusted_etf_scopes=trusted_etf_scopes)
    universe = _admit("universe", references["universe"], panel=current_panel,
                      registry=registry, cutoffs=cutoffs)
    if any(row["is_member"] is not True for row in universe.payload_by_panel.values()):
        raise ValueError("main BUY symbol is outside signed universe")
    _admit("corporate_actions", references["corporate_actions"], panel=current_panel,
           registry=registry, cutoffs=cutoffs)
    global_inputs = payload["global_signals"]
    if set(global_inputs) - {"source_evidence"} != {"snapshot", "artifact", "source_receipts", "authority_envelope"}:
        raise ValueError("main BUY global closure is incomplete")
    global_artifact = global_inputs["artifact"]
    if not global_artifact.get("records"):
        raise ValueError("main BUY global records are missing")
    available_at = global_artifact["records"][0]["available_at"]
    rebuilt_global = build_global_signals_authority_inputs(
        global_inputs["snapshot"], panel=current_panel, available_at=available_at,
        source_evidence=global_inputs.get("source_evidence"),
    )
    if any(global_inputs[key] != value for key, value in rebuilt_global.items()):
        raise ValueError("main BUY global snapshot closure drifted")
    _admit("global_signals", global_inputs, panel=current_panel,
           registry=registry, cutoffs=cutoffs)
    return payload
