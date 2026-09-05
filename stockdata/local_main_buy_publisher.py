"""Publish reviewed current-observation facts for the configured main ETFs."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .authority import ALGORITHM, AUTHORITY_ENVELOPE_SCHEMA, load_enrolled_trust_registry_bytes
from .liquidity_amount_product import (
    _canonical, _hash, _timestamp, build_liquidity_amounts_product, build_liquidity_authority_inputs,
)
from .local_publisher import _write_private
from .main_buy_supplement import (
    SCHEMA_VERSION, build_global_signals_authority_inputs, verify_main_buy_supplement,
)
from .market_rules import ETF_MARKET_RULE_PAYLOAD_SCHEMA, ETF_RULE_SCOPES
from .provider_authority_admission import SOURCE_RECEIPT_SCHEMA
from .provider_authority_publisher import _base64, _key_id, _public_key
from .rqgm_provider_contract import COMPONENT_SCHEMAS

SYMBOL = "561980.SH"
_REVIEWED_CASH_DIVIDENDS = (
    ("2025-09-18", "2025-09-22", "2025-09-23", "2025-09-26", 1.45,
     "ddfaac7c7097b0a15666d6e6787553be48613468e221564bccabfceceb5d0d02", "511010_20250918_FU3S.pdf"),
    ("2025-12-23", "2025-12-25", "2025-12-26", "2025-12-31", .6542,
     "733995693eef19c61a55a30d2be7dcc0dcc471980b81b85363977478195c50a8", "511010_20251223_0M89.pdf"),
    ("2026-03-20", "2026-03-24", "2026-03-25", "2026-03-30", .5715,
     "a45a301dd556b3b1f36bad19abf952e7490110fda539a1d716cadb903bd22656", "511010_20260320_Y87H.pdf"),
    ("2026-06-22", "2026-06-24", "2026-06-25", "2026-06-30", .8144,
     "82418fe2913770fd1ab6211869dbc8a83683e73490fcdb277c9c3be226a992a9", "511010_20260622_0EYP.pdf"),
)


def _validate_reviewed_events(facts, symbol):
    identities = []
    if symbol == "511010.SH":
        for announcement, record, ex, pay, amount, digest, filename in _REVIEWED_CASH_DIVIDENDS:
            identities.append({"symbol": symbol, "event_type": "cash_dividend", "announcement_date": announcement,
                "record_date": record, "ex_date": ex, "pay_date": pay, "per_unit_cash_cny": amount,
                "share_units_unchanged": True, "source_sha256": digest,
                "source_url": f"https://www.sse.com.cn/disclosure/fund/announcement/c/new/{announcement}/{filename}"})
    expected = [{"event_type": "cash_dividend", "effective_date": value["ex_date"], "event_id": value["source_sha256"]}
                for value in identities]
    if _canonical(facts["events"]) != _canonical(expected) or _canonical(facts.get("cash_dividend_identities", [])) != _canonical(identities):
        raise ValueError("reviewed exact ETF corporate-action set differs")


def _validate_universe(directory, symbol, source_files):
    names = {"local-main-universe.json", "local-main-config.json"}
    if not names <= set(source_files):
        raise ValueError("additional ETF universe is not receipt-bound")
    raw = (directory / "local-main-config.json").read_bytes()
    config = json.loads(raw)
    symbols = sorted(row["code"][2:] + "." + row["code"][:2].upper() for row in config["holdings"])
    universe = json.loads((directory / "local-main-universe.json").read_bytes())
    if (symbols != sorted(ETF_RULE_SCOPES) or universe["symbols"] != symbols or symbol not in symbols
            or universe["source_sha256"] != hashlib.sha256(raw).hexdigest()):
        raise ValueError("observed configured universe identity or source hash differs")
    return universe


def _evidence(directory, index, names, assessment):
    entries = {item["file"]: item for item in index["files"]}
    files = []
    for name in names:
        raw = (directory / name).read_bytes()
        entry = entries[name]
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"retained source evidence hash drifted: {name}")
        files.append({**entry, "raw_base64": base64.b64encode(raw).decode("ascii")})
    return {"schema_version": "stockdata-local-reference-source-evidence/1",
            "assessment": assessment, "files": files}


def _reference(component, rows, evidence, observed):
    records = [{"panel_entry": entry, "payload": value, "record_sha256": _hash(value),
                "source_receipt_ids": [], "effective_at": f"{entry.split('@')[1]}T00:00:00+08:00",
                "available_at": observed} for entry, value in sorted(rows.items())]
    receipt = {"schema_version": SOURCE_RECEIPT_SCHEMA, "source": "local-reviewed-official-facts/1",
               "observed_at": observed, "response_sha256": _hash(evidence),
               "bindings": [{"component": component, "panel_entry": row["panel_entry"],
                             "record_sha256": row["record_sha256"]} for row in records]}
    receipt_id = _hash(receipt)
    for row in records:
        row["source_receipt_ids"] = [receipt_id]
    return {"artifact": {"schema_version": COMPONENT_SCHEMAS[component], "component": component,
                         "panel": sorted(rows), "records": records},
            "source_receipts": {receipt_id: receipt}, "source_evidence": evidence}


def _validate_status_capture(captures, symbol, asof, observed):
    if len(captures) < 2:
        raise ValueError("additional ETF status captures are incomplete")
    vendor = symbol[-2:].lower() + "." + symbol[:6]
    expected = [
        {"method": "query_stock_basic", "code": vendor},
        {"method": "query_history_k_data_plus", "code": vendor, "fields": "date,tradestatus,isST",
         "start_date": asof, "end_date": asof, "frequency": "d", "adjustflag": "3"},
    ]
    for capture, request in zip(captures[:2], expected):
        if capture["request"] != request or _timestamp(capture["observed_at"]) >= _timestamp(observed):
            raise ValueError("additional ETF status request identity or observation differs")
    basic, trading = [value["response"] for value in captures[:2]]
    basics = [dict(zip(basic["fields"], row)) for row in basic["rows"]]
    states = [dict(zip(trading["fields"], row)) for row in trading["rows"]]
    if (basic["error_code"] != "0" or trading["error_code"] != "0" or len(basics) != 1 or len(states) != 1
            or basics[0]["code"] != vendor or basics[0]["type"] != "5" or basics[0]["status"] != "1"
            or states[0].get("code", vendor) != vendor or states[0]["date"] != asof or states[0]["tradestatus"] != "1"):
        raise ValueError("additional ETF source trading state is unavailable")
    return states[0]


def _validate_retained_times(evidence, observed):
    cutoff = _timestamp(observed)
    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"observed_at", "retained_at"} and child is not None and _timestamp(child) >= cutoff:
                    raise ValueError("retained source was not available before current observation")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    for entry in evidence["files"]:
        check(entry)
        if entry["file"].endswith(".json"):
            check(json.loads(base64.b64decode(entry["raw_base64"])))


def _validate_announcement_capture(directory, symbol, source_files, observed):
    sh = symbol.endswith(".SH")
    name = symbol + ("-sse-announcements.json" if sh else "-szse-announcements.raw.json")
    if name not in source_files or name + ".receipt.json" not in source_files:
        raise ValueError("official announcement window is not receipt-bound")
    raw = (directory / name).read_bytes()
    data = json.loads(raw)
    receipt = json.loads((directory / (name + ".receipt.json")).read_bytes())
    request, response = receipt["request"], receipt["response"]
    if (response["status_code"] != 200 or response["sha256"] != hashlib.sha256(raw).hexdigest()
            or response["bytes"] != len(raw) or _timestamp(receipt["observed_at"]) >= _timestamp(observed)):
        raise ValueError("official announcement receipt is invalid")
    if sh:
        expected = {"END_DATE": "20260905", "SECURITY_CODE": symbol[:6], "START_DATE": "20250711",
            "isPagination": "true", "pageHelp.pageSize": "1000", "sqlId": "COMMON_PL_JJXX_JJGG_L"}
        rows, page = data["result"], data["pageHelp"]
        valid = (request == {"method": "GET", "url": "https://query.sse.com.cn/commonQuery.do", "params": expected}
                 and page["pageCount"] == 1 and page["pageNo"] == 1 and page["total"] == len(rows)
                 and all(row["SECURITY_CODE"] == symbol[:6] for row in rows))
    else:
        rows = data["data"]
        valid = (request["method"] == "POST" and request["url"] == "https://www.szse.cn/api/disc/announcement/annList"
                 and request["body"] == {"stock": [symbol[:6]], "channelCode": ["fundinfoNotice_disc"],
                    "seDate": ["2025-07-11", "2026-09-05"], "pageSize": 50, "pageNum": 1}
                 and data["announceCount"] == len(rows) and len(rows) <= 50
                 and all(row["secCode"] == [symbol[:6]] for row in rows))
    if not valid:
        raise ValueError("official announcement symbol/window/page coverage differs")


def prepare_reference_inputs(*, evidence_dir, liquidity_product, asof, global_snapshot):
    directory = Path(evidence_dir)
    index = json.loads((directory / "evidence-index.json").read_bytes())
    if index["asof"] != asof or index["symbol"] != SYMBOL:
        raise ValueError("reviewed reference evidence identity differs")
    def read(name):
        return json.loads((directory / name).read_bytes())
    evidence_names = {
        "trading_calendar": ["baostock-calendar-20260810-20260907.json", "sse-trading-rules-2026.docx"],
        "instrument_status": ["baostock-status-561980-20260904.json", "sse-fund-list-561980.json", "sse-trading-rules-2026.docx"],
        "universe": ["local-universe-561980.json"],
        "corporate_actions": ["sse-announcements-561980-20250101-20260905.json", "sse-561980-split-plan-20260622.pdf",
                              "sse-561980-split-result-20260626.pdf", "baostock-corporate-actions-561980-2025-2026.json"],
        "market_rules": ["cmf-561980-fund.html", "sse-trading-rules-2026.docx", "sse-etf-faq.html",
                         "trading-fee-policy-manifest.json", "trading-fee-policy-observation.json"],
        "global_signals": ["global_quotes.json", "global-quotes-local-observation.json"],
    }
    assessments = {"trading_calendar": "calendar", "instrument_status": "status", "global_signals": "global"}
    evidence = {component: _evidence(directory, index, names,
                index["assessments"][assessments.get(component, component)]) for component, names in evidence_names.items()}
    calendar = read(evidence_names["trading_calendar"][0])
    if calendar["response"]["error_code"] != "0":
        raise ValueError("calendar source query failed")
    days = [row[0] for row in calendar["response"]["rows"] if row[1] == "1"]
    history = [entry.split("@")[1] for entry in liquidity_product["panel"] if entry.startswith(SYMBOL + "@")]
    if days[:-1] != history or history[-1] != asof:
        raise ValueError("calendar does not cover the exact amount session chain")
    status = read(evidence_names["instrument_status"][0])
    basic, trading = status["responses"]
    basics = [dict(zip(basic["fields"], row)) for row in basic["rows"]]
    states = [dict(zip(trading["fields"], row)) for row in trading["rows"]]
    listed = read("sse-fund-list-561980.json")["result"]
    if (basic["error_code"] != "0" or trading["error_code"] != "0" or len(basics) != 1 or len(states) != 1
            or basics[0]["code"] != "sh.561980" or basics[0]["type"] != "5" or basics[0]["status"] != "1"
            or states[0]["date"] != asof or states[0]["tradestatus"] != "1"
            or len(listed) != 1 or listed[0]["FUND_CODE"] != "561980" or listed[0]["CATEGORY"] != "F112"):
        raise ValueError("reviewed exact ETF listing or trading state is unavailable")
    evidence["instrument_status"]["derivation"] = {
        "is_st": "not applicable to this SSE-listed ETF; false is the non-ST rule branch",
        "raw_baostock_isST": states[0]["isST"],
        "conflict": "BaoStock ETF isST is not interpreted as a listed-company stock risk-warning flag",
    }
    universe = read("local-universe-561980.json")
    if universe["is_member"] is not True or universe["query"]["canonical_symbol"] != SYMBOL:
        raise ValueError("ETF is outside the observed configured universe")
    fees = read("trading-fee-policy-manifest.json")
    if fees["policy_version"] != "broker-fee-v1":
        raise ValueError("frozen fee policy version differs")
    observed = datetime.now(timezone.utc).isoformat()
    entry = f"{SYMBOL}@{asof}"
    calendar_rows = {f"{SYMBOL}@{day}": {"is_trading_day": True,
        "decision_cutoff_at": f"{day}T09:25:00+08:00", "session_close_at": f"{day}T15:00:00+08:00",
        "next_session_decision_cutoff_at": f"{next_day}T09:25:00+08:00"} for day, next_day in zip(days, days[1:])}
    rule = {"schema_version": ETF_MARKET_RULE_PAYLOAD_SCHEMA, "policy_id": "sse-561980-current-local-v1",
        "source": "local-reviewed-official-facts/1", "source_sha256": _hash(evidence["market_rules"]),
        "security_type": "ETF", "board": "ETF", "exchange": "SH", "instrument_id": SYMBOL,
        **ETF_RULE_SCOPES[SYMBOL], "effective_until": asof, "listing_age_min": 0, "listing_age_max": None,
        "is_st": False, "lot_size": 100, "t_plus_one": True, "reject_suspended": True, "reject_zero_volume": True,
        "price_limit_up": 0.1, "price_limit_down": 0.1, "price_limit_reference": "RECORD_OR_PREVIOUS_CLOSE",
        "price_tick": 0.001, "price_rounding": "HALF_UP", "locked_limit_order_policy": "REJECT_SIDE",
        "commission_rate": fees["commission_rate"], "minimum_commission": fees["min_commission_cny"],
        "transfer_fee_rate": 0., "stamp_duty_sell_rate": fees["etf_stamp_tax"],
        "slippage_model": "OPEN_BPS", "slippage_bps": 0., "slippage_bounds": "BAR_AND_PRICE_LIMITS",
        "time_in_force": "DAY", "cancel_unfilled_at_close": True}
    evidence["corporate_actions"]["announcement_timestamp_basis"] = (
        "announcement_at conservatively records current local first-observed knowledge; original publication date is 2026-06-22"
    )
    event = {"event_type": "split", "effective_date": "2026-06-26", "announcement_at": observed,
             "event_id": next(item["sha256"] for item in index["files"] if item["file"] == "sse-561980-split-result-20260626.pdf")}
    evidence["corporate_actions"]["split_identity"] = {
        "symbol": SYMBOL, "effective_date": event["effective_date"], "old_units": 1, "new_units": 5,
        "source_sha256": event["event_id"],
    }
    rows = {"trading_calendar": calendar_rows, "instrument_status": {entry: {"is_st": False, "is_suspended": False, "listing_status": "listed"}},
            "universe": {entry: {"is_member": True, "universe_id": universe["source"]["sha256"]}},
            "corporate_actions": {entry: {"events": [event]}}, "market_rules": {entry: rule}}
    references = {component: _reference(component, values, evidence[component], observed) for component, values in rows.items()}
    original_quotes = read("global_quotes.json")
    for name, quote in global_snapshot["quotes"].items():
        original = original_quotes[name]
        if quote != {"price": original["price"], "chg_pct": original["chg_pct"],
                     "chg_20d": original["chg_20d"], "date": original["last_date"]}:
            raise ValueError("global snapshot differs retained local automation facts")
    global_inputs = build_global_signals_authority_inputs(global_snapshot, panel=[entry],
        available_at=observed, source_evidence=evidence["global_signals"])
    return references, global_inputs


def extend_reference_inputs(references, global_inputs, *, evidence_dir, symbols, asof):
    directory = Path(evidence_dir)
    index = json.loads((directory / "evidence-index.json").read_bytes())
    expected = set(symbols) - {SYMBOL}
    if (asof != "2026-09-04" or index["schema_version"] != "stockdata-main-etf-reviewed-facts/1" or index["asof"] != asof
            or set(index["instruments"]) != expected or not expected <= set(ETF_RULE_SCOPES)):
        raise ValueError("additional reviewed ETF facts differ from the exact amount panel")
    observed = datetime.now(timezone.utc).isoformat()
    rows = {component: {r["panel_entry"]: r["payload"] for r in value["artifact"]["records"]}
            for component, value in references.items()}
    evidence = {component: {**value["source_evidence"], "additional_instruments": {}}
                for component, value in references.items()}
    evidence["corporate_actions"]["cash_dividend_identities"] = []
    base_entry = f"{SYMBOL}@{asof}"
    for symbol in sorted(expected):
        facts = index["instruments"][symbol]
        if facts["corporate_actions_coverage"] != {"start_date": "2025-07-11", "end_date": asof, "complete": True}:
            raise ValueError("additional ETF corporate-action coverage is incomplete")
        _validate_reviewed_events(facts, symbol)
        bound = {component: _evidence(directory, index, facts["source_files"][component], facts["assessments"][component])
                 for component in references}
        for item in bound.values():
            _validate_retained_times(item, observed)
        captures = json.loads((directory / f"{symbol}-status-ca.json").read_bytes())
        state = _validate_status_capture(captures, symbol, asof, observed)
        _validate_announcement_capture(directory, symbol, facts["source_files"]["corporate_actions"], observed)
        if f"{symbol}-status-ca.json" not in facts["source_files"]["instrument_status"]:
            raise ValueError("additional ETF status facts are not receipt-bound")
        universe = _validate_universe(directory, symbol, facts["source_files"]["universe"])
        bound["instrument_status"]["derivation"] = {
            "is_st": "not applicable to an exchange-listed ETF; false selects the non-stock-ST rule branch",
            "raw_baostock_isST": state["isST"],
            "conflict": "ETF isST is not interpreted as a listed-company stock risk-warning flag",
        }
        events = []
        event_hashes = {item["sha256"] for item in bound["corporate_actions"]["files"]}
        for item in facts["events"]:
            if item["event_id"] not in event_hashes:
                raise ValueError("additional ETF corporate action lacks its retained official source")
            events.append({**item, "announcement_at": observed})
        bound["corporate_actions"]["cash_dividend_identities"] = facts.get("cash_dividend_identities", [])
        evidence["corporate_actions"]["cash_dividend_identities"].extend(facts.get("cash_dividend_identities", []))
        bound["corporate_actions"]["coverage"] = facts["corporate_actions_coverage"]
        entry = f"{symbol}@{asof}"
        rule = {**rows["market_rules"][base_entry], **ETF_RULE_SCOPES[symbol], "instrument_id": symbol,
                "policy_id": f"{symbol}-current-local-v1", "source_sha256": _hash(bound["market_rules"])}
        rows["market_rules"][entry] = rule
        rows["instrument_status"][entry] = {"is_st": False, "is_suspended": False, "listing_status": "listed"}
        rows["universe"][entry] = {"is_member": True, "universe_id": universe["source_sha256"]}
        rows["corporate_actions"][entry] = {"events": events}
        for panel_entry, value in list(rows["trading_calendar"].items()):
            if panel_entry.startswith(SYMBOL + "@"):
                rows["trading_calendar"][symbol + "@" + panel_entry.split("@")[1]] = value
        for component in references:
            evidence[component]["additional_instruments"][symbol] = bound[component]
    references = {component: _reference(component, values, evidence[component], observed) for component, values in rows.items()}
    global_inputs = build_global_signals_authority_inputs(global_inputs["snapshot"],
        panel=[f"{symbol}@{asof}" for symbol in symbols], available_at=observed,
        source_evidence=global_inputs["source_evidence"])
    return references, global_inputs


def publish_main_buy_supplement(*, evidence_dir, liquidity_capture_file, global_snapshot_file,
                               publisher_dir, registry_sha256, provider_manifest_sha256, output_dir,
                               additional_evidence_dir=None):
    directory = Path(publisher_dir)
    registry_raw = (directory / "registry.json").read_bytes()
    registry = load_enrolled_trust_registry_bytes(registry_raw, expected_sha256=registry_sha256)
    keyfile = directory / "publisher.key"
    if keyfile.stat().st_mode & 0o077:
        raise ValueError("publisher key must be private")
    key = Ed25519PrivateKey.from_private_bytes(keyfile.read_bytes())
    publisher_id = _key_id(_public_key(key))
    signer = registry._signers[publisher_id]
    capture = json.loads(Path(liquidity_capture_file).read_bytes())
    asof = capture["request"]["end_date"]
    fields = capture["response"]["fields"].split(",")
    dates = sorted(row[fields.index("date")] for row in capture["response"]["rows"])[-20:]
    captures = [capture]
    symbols = [SYMBOL]
    if additional_evidence_dir is not None:
        symbols = sorted(ETF_RULE_SCOPES)
        captures.extend(json.loads((Path(additional_evidence_dir) / f"{symbol}-amount.json").read_bytes())
                        for symbol in symbols if symbol != SYMBOL)
    product = build_liquidity_amounts_product(captures, panel=[(symbol, day) for symbol in symbols for day in dates],
        decision_cutoff=datetime.now(timezone.utc).isoformat(), expected_watermark=asof)
    references, global_inputs = prepare_reference_inputs(evidence_dir=evidence_dir, liquidity_product=product,
        asof=asof, global_snapshot=json.loads(Path(global_snapshot_file).read_bytes()))
    if additional_evidence_dir is not None:
        references, global_inputs = extend_reference_inputs(references, global_inputs,
            evidence_dir=additional_evidence_dir, symbols=symbols, asof=asof)
    def sign(inputs):
        artifact = inputs["artifact"]
        observed = datetime.now(timezone.utc).isoformat()
        value = {"component_role": artifact["component"], "artifact": {
            "kind": f"stock-data-{artifact['component'].replace('_', '-')}", "schema_version": artifact["schema_version"],
            "identifier": _hash(artifact)}, "source_receipt_ids": sorted(inputs["source_receipts"]),
            "effective_at": observed, "available_at": observed, "publisher_key_id": publisher_id,
            "trust_root_id": signer.trust_root_id, "trust_registry_sha256": registry_sha256}
        return {**inputs, "authority_envelope": {"schema_version": AUTHORITY_ENVELOPE_SCHEMA,
                "algorithm": ALGORITHM, "payload": value, "signature_base64": _base64(key.sign(_canonical(value)))}}
    liquidity = sign(build_liquidity_authority_inputs(product))
    global_inputs = sign(global_inputs)
    references = {component: sign(inputs) for component, inputs in references.items()}
    cutoff = datetime.now(timezone.utc).isoformat()
    payload = {"schema_version": SCHEMA_VERSION, "provider_manifest_sha256": provider_manifest_sha256,
               "asof": asof, "decision_cutoff": cutoff, "symbols": symbols, "registry": json.loads(registry_raw),
               "liquidity": liquidity, "global_signals": global_inputs, "references": references}
    verify_main_buy_supplement(payload, expected_registry_sha256=registry_sha256,
        provider_manifest_sha256=provider_manifest_sha256, asof=asof, decision_cutoff=cutoff)
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    _write_private(destination / "supplement.json", _canonical(payload))
    return {"supplement_file": str(destination / "supplement.json"), "asof": asof,
            "decision_cutoff": cutoff, "sha256": _hash(payload)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("evidence-dir", "liquidity-capture-file", "global-snapshot-file", "publisher-dir",
                 "registry-sha256", "provider-manifest-sha256", "output-dir"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--additional-evidence-dir")
    result = publish_main_buy_supplement(**vars(parser.parse_args(argv)))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
