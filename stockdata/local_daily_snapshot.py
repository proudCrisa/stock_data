"""Signed current-observation prices for the Trading daily input profile."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .authority import (
    ALGORITHM, AUTHORITY_ENVELOPE_SCHEMA, load_enrolled_trust_registry_bytes,
    verify_authority_envelope,
)
from .daily_bar_product import (
    MANIFEST_SCHEMA, PERMITTED_USES, PRODUCT_SCHEMA, QUALITY_STATUS, ROW_SCHEMA,
    _canonical, _hash, _validate_bar,
)
from .fetch_baostock import fetch_baostock
from .fetch_tencent_history import _parse_rows as parse_tencent_rows
from .liquidity_amount_product import _timestamp
from .local_publisher import _write_private
from .provider_authority_publisher import _base64, _key_id, _public_key
from .ticker import to_tencent

SCHEMA_VERSION = "stockdata-trading-daily-snapshot/1"
COMPONENT = "local_daily_prices"
ARTIFACT_SCHEMA = "stockdata-local-daily-prices/1"
PROFILE = "trading-current-local-prices/1"
ETF_SYMBOLS = frozenset({"588730.SH", "518880.SH", "561980.SH", "159980.SZ",
                         "560900.SH", "511010.SH", "513650.SH", "159350.SZ"})
INDEX_SYMBOLS = frozenset({"000300.SH", "000688.SH", "399811.SZ", "399395.SZ", "000933.SH"})
TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
FIELDS = ("date", "open", "high", "low", "close", "volume")


def _route(symbol, role):
    if symbol not in ETF_SYMBOLS | INDEX_SYMBOLS or role not in {"execution", "signal"}:
        raise ValueError("symbol or price role is outside the local daily profile")
    adjustment = "qfq" if symbol in ETF_SYMBOLS and role == "signal" else "raw"
    sources = ["tencent.ifzq"] if symbol in ETF_SYMBOLS else ["baostock", "tencent.ifzq"]
    return [(source, adjustment) for source in sources]


def _capture(source, symbol, start, asof, adjustment):
    if source == "baostock":
        return fetch_baostock(symbol, start, asof, adjustment_mode=adjustment).capture_receipt
    import requests

    vendor_symbol = to_tencent(symbol).replace(".", "")
    pages = []
    for count in (800, 320):
        params = {"param": f"{vendor_symbol},day,{start},{asof},{count},{'qfq' if adjustment == 'qfq' else ''}"}
        response = requests.get(TENCENT_URL, params=params,
                                headers={"Referer": "https://gu.qq.com/"}, timeout=20)
        pages.append({"observed_at": datetime.now(timezone.utc).isoformat(),
                      "request": {"url": TENCENT_URL, "params": params},
                      "response": {"status_code": response.status_code, "raw": response.text}})
        if response.status_code == 200:
            try:
                body = response.json()["data"][vendor_symbol]
                rows = body.get("qfqday") or body.get("day") or []
                if rows and max(str(row[0]) for row in rows) >= asof:
                    break
            except (KeyError, ValueError, TypeError):
                pass
    return {"source": source, "observed_at": datetime.now(timezone.utc).isoformat(),
            "request": {"code": symbol, "start_date": start, "end_date": asof,
                        "adjustment_mode": adjustment}, "response": {"pages": pages}}


def _replay(capture, *, source, symbol, start, asof, adjustment, cutoff):
    if capture["source"] != source or _timestamp(capture["observed_at"]) >= cutoff:
        raise ValueError("price capture source or availability differs")
    request, response = capture["request"], capture["response"]
    if request["start_date"] != start or request["end_date"] != asof:
        raise ValueError("price capture does not match exact requested range")
    actual_adjustment = adjustment
    if source == "baostock":
        code = f"{symbol[-2:].lower()}.{symbol[:6]}"
        if (request["code"] != code or request["method"] != "query_history_k_data_plus"
                or request["frequency"] != "d"
                or request["adjustflag"] != {"raw": "3", "qfq": "2"}[adjustment]):
            raise ValueError("baostock price request identity differs")
        fields = response["fields"].split(",") if isinstance(response["fields"], str) else response["fields"]
        if fields != request["fields"].split(",") or len(fields) != len(set(fields)):
            raise ValueError("baostock response fields differ request")
        rows = []
        for raw in response["rows"]:
            if len(raw) != len(fields):
                raise ValueError("baostock price row is malformed")
            row = dict(zip(fields, raw))
            rows.append({key: row[key] if key == "date" else float(row[key]) for key in FIELDS})
    else:
        if request["code"] != symbol or request["adjustment_mode"] != adjustment:
            raise ValueError("Tencent price request identity differs")
        vendor_symbol = to_tencent(symbol).replace(".", "")
        by_date, modes = {}, set()
        for page in response["pages"]:
            if _timestamp(page["observed_at"]) > _timestamp(capture["observed_at"]):
                raise ValueError("Tencent page observed after enclosing capture")
            params = page["request"]["params"]
            valid_params = [{"param": f"{vendor_symbol},day,{start},{asof},{count},{'qfq' if adjustment == 'qfq' else ''}"}
                            for count in (800, 320)]
            if page["request"]["url"] != TENCENT_URL or params not in valid_params:
                raise ValueError("Tencent request URL or exact scope differs")
            if page["response"]["status_code"] != 200:
                continue
            body = json.loads(page["response"]["raw"])
            if body.get("code") != 0:
                continue
            symbol_data = body["data"][vendor_symbol]
            mode = "qfq" if adjustment == "qfq" and symbol_data.get("qfqday") else "raw"
            modes.add(mode)
            for row in parse_tencent_rows(body, vendor_symbol, mode):
                by_date.setdefault(row["date"], row)
        if len(modes) != 1:
            raise ValueError("Tencent pages have missing or mixed adjustment identity")
        actual_adjustment = modes.pop()
        rows = list(by_date.values())
    selected = sorted((row for row in rows if start <= row["date"] <= asof), key=lambda row: row["date"])
    if len({row["date"] for row in selected}) != len(selected):
        raise ValueError("duplicate daily price dates")
    for row in selected:
        _validate_bar(row)
        if _timestamp(f"{row['date']}T15:00:00+08:00") >= _timestamp(capture["observed_at"]):
            raise ValueError("daily price was observed before final session close")
    if len(selected) < 20 or selected[-1]["date"] != asof:
        raise ValueError("daily price lacks history or exact final watermark")
    return selected, actual_adjustment


def build_price_manifest(attempts, *, symbol, role, start, asof, decision_cutoff):
    cutoff = _timestamp(decision_cutoff)
    route = _route(symbol, role)
    if not attempts or len(attempts) > len(route):
        raise ValueError("price source attempts do not match the fixed route")
    selected = None
    for index, attempt in enumerate(attempts):
        source, adjustment = route[index]
        if set(attempt) != {"source", "observed_at", "capture", "error"} or attempt["source"] != source:
            raise ValueError("price fallback attempt identity differs")
        if _timestamp(attempt["observed_at"]) >= cutoff:
            raise ValueError("price attempt is post-cutoff")
        if attempt["capture"] is None:
            if not isinstance(attempt["error"], str) or not attempt["error"]:
                raise ValueError("failed price attempt lacks observed error")
            continue
        try:
            rows, mode = _replay(attempt["capture"], source=source, symbol=symbol,
                                 start=start, asof=asof, adjustment=adjustment, cutoff=cutoff)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            if index == len(attempts) - 1:
                raise ValueError("selected price source cannot replay") from exc
            continue
        if attempt["error"] is not None or index != len(attempts) - 1:
            raise ValueError("fallback replaced an eligible higher-priority price source")
        selected = (source, rows, mode, attempt["capture"])
    if selected is None:
        raise ValueError("no eligible local price source")
    source, rows, mode, capture = selected
    receipt_id = _hash(capture)
    product_rows = [{**row, "retrieved_at": capture["observed_at"], "is_final": True,
                     "source_receipt_id": receipt_id} for row in rows]
    product = {
        "schema_version": PRODUCT_SCHEMA, "data_product_id": f"daily-bars:{symbol}:{source}:{mode}",
        "version": _hash(product_rows), "schema_id": ROW_SCHEMA,
        "authority_grade": "shadow", "decision_eligible": False, "source_authentication": "unverified",
        "reference_binding_status": "declared_unverified",
        "instrument_scope": {"codes": [symbol], "start": start, "end": asof},
        "event_time_range": {"start": rows[0]["date"], "end": asof}, "content_hash": _hash(product_rows),
        "source_receipt_ids": [receipt_id], "available_at": capture["observed_at"],
        "finality": {"status": "source_marked_final", "watermark": asof}, "pit_mode": "current_observation",
        "corporate_action_version": "not_bound", "universe_version": PROFILE, "trading_calendar_version": "not_bound",
        "quality_grade": QUALITY_STATUS, "permitted_uses": PERMITTED_USES, "lineage_ids": [],
        "price_identity": {"source": source, "adjustment_mode": mode,
                           "adjustment_version": f"{source}-{mode}", "volume_unit": "share"},
        "rows": product_rows, "source_receipts": [{"source_receipt_id": receipt_id, **capture}],
    }
    product["product_sha256"] = _hash(product)
    manifest = {
        "schema_version": MANIFEST_SCHEMA, "authority_grade": "shadow", "decision_eligible": False,
        "source_authentication": "unverified", "reference_binding_status": "declared_unverified",
        "created_at": decision_cutoff, "decision_cutoff": decision_cutoff,
        "dataset_ids": [product["data_product_id"]], "receipt_ids": [receipt_id],
        "content_hashes": [product["product_sha256"]], "provider_authorities": [], "claimed_sources": [source],
        "instrument_universe_version": PROFILE, "trading_calendar_version": "not_bound",
        "corporate_action_version": "not_bound", "available_at": capture["observed_at"],
        "finality": "source_marked_final", "quality_status": QUALITY_STATUS,
        "fallback_status": "used" if len(attempts) > 1 else "not_used", "permitted_uses": PERMITTED_USES,
        "products": [product], "source_attempts": attempts,
    }
    digest = _hash(manifest)
    return {**manifest, "manifest_id": f"shadow-{digest}", "manifest_sha256": digest}


def verify_local_daily_snapshot(payload, *, expected_registry_sha256, expected_symbols, asof, decision_cutoff):
    if set(payload) != {"schema_version", "asof", "decision_cutoff", "symbols", "registry", "artifact",
                        "source_receipts", "authority_envelope", "snapshot_sha256"} or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("local daily snapshot schema differs")
    if payload["snapshot_sha256"] != _hash({k: v for k, v in payload.items() if k != "snapshot_sha256"}):
        raise ValueError("local daily snapshot hash differs")
    symbols = sorted(expected_symbols)
    if (payload["asof"] != asof or payload["symbols"] != symbols or len(symbols) != len(set(symbols))
            or not symbols or _timestamp(payload["decision_cutoff"]) > _timestamp(decision_cutoff)):
        raise ValueError("local daily snapshot differs external decision identity")
    cutoff = _timestamp(payload["decision_cutoff"])
    if cutoff <= _timestamp(f"{asof}T15:00:00+08:00"):
        raise ValueError("local daily price session is not final")
    registry = load_enrolled_trust_registry_bytes(_canonical(payload["registry"]), expected_sha256=expected_registry_sha256)
    artifact = payload["artifact"]
    panel = [f"{symbol}@{asof}" for symbol in symbols]
    if (set(artifact) != {"schema_version", "component", "panel", "records"}
            or artifact["schema_version"] != ARTIFACT_SCHEMA or artifact["component"] != COMPONENT
            or artifact["panel"] != panel or len(artifact["records"]) != len(panel)):
        raise ValueError("local daily price artifact differs exact panel")
    receipt_ids = []
    for entry, record in zip(panel, artifact["records"]):
        if set(record) != {"panel_entry", "payload", "record_sha256", "source_receipt_ids", "effective_at", "available_at"}:
            raise ValueError("local daily price record is incomplete")
        if record["panel_entry"] != entry or set(record["payload"]) != {"execution", "signal"}:
            raise ValueError("local daily price role or symbol differs")
        for role, manifest in record["payload"].items():
            start = (date.fromisoformat(asof) - timedelta(days=420)).isoformat()
            if _timestamp(manifest["decision_cutoff"]) > cutoff:
                raise ValueError("price manifest is later than snapshot freeze")
            rebuilt = build_price_manifest(manifest["source_attempts"], symbol=entry.split("@")[0], role=role,
                                           start=start, asof=asof, decision_cutoff=manifest["decision_cutoff"])
            if manifest != rebuilt:
                raise ValueError("local daily price manifest cannot replay")
        if record["record_sha256"] != _hash(record["payload"]):
            raise ValueError("local daily price record hash differs")
        observed = max((m["available_at"] for m in record["payload"].values()), key=_timestamp)
        if record["available_at"] != observed or record["effective_at"] != f"{asof}T15:00:00+08:00":
            raise ValueError("local daily price record time differs")
        receipt = {"schema_version": "stockdata-source-receipt/1", "observed_at": observed,
                   "source": PROFILE, "response_sha256": _hash(record["payload"]),
                   "bindings": [{"component": COMPONENT, "panel_entry": entry, "record_sha256": record["record_sha256"]}]}
        receipt_id = _hash(receipt)
        if record["source_receipt_ids"] != [receipt_id] or payload["source_receipts"].get(receipt_id) != receipt:
            raise ValueError("local daily price receipt binding differs")
        receipt_ids.append(receipt_id)
    if set(payload["source_receipts"]) != set(receipt_ids):
        raise ValueError("local daily price has unbound receipts")
    verified = verify_authority_envelope(payload["authority_envelope"], registry=registry,
        expected_component=COMPONENT, expected_artifact={"kind": "stock-data-local-daily-prices",
        "schema_version": ARTIFACT_SCHEMA, "identifier": _hash(artifact)}, expected_source_receipt_ids=sorted(receipt_ids))
    if max(_timestamp(verified.available_at), _timestamp(verified.effective_at)) >= cutoff:
        raise ValueError("local daily price authority is post-cutoff")
    return payload


def capture_local_daily_snapshot(*, symbols, asof, publisher_dir, expected_registry_sha256, output_dir):
    symbols = sorted(symbols)
    if not symbols or len(symbols) != len(set(symbols)) or not set(symbols) <= ETF_SYMBOLS | INDEX_SYMBOLS:
        raise ValueError("local daily capture symbols are outside the approved exact profile")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    start = (date.fromisoformat(asof) - timedelta(days=420)).isoformat()
    captures, memo = {}, {}
    for symbol in symbols:
        captures[symbol] = {}
        for role in ("execution", "signal"):
            attempts = []
            for source, adjustment in _route(symbol, role):
                identity = (source, symbol, adjustment)
                if identity not in memo:
                    try:
                        capture = _capture(source, symbol, start, asof, adjustment)
                        attempt = {"source": source, "observed_at": datetime.now(timezone.utc).isoformat(),
                                   "capture": capture, "error": None}
                    except Exception as exc:
                        attempt = {"source": source, "observed_at": datetime.now(timezone.utc).isoformat(),
                                   "capture": None, "error": f"{type(exc).__name__}: {exc}"}
                    memo[identity] = attempt
                    _write_private(destination / f"{symbol}-{source}-{adjustment}.json", _canonical(attempt))
                attempts.append(memo[identity])
                try:
                    build_price_manifest(attempts, symbol=symbol, role=role, start=start, asof=asof,
                                         decision_cutoff=datetime.now(timezone.utc).isoformat())
                    break
                except ValueError:
                    pass
            captures[symbol][role] = attempts
        print(json.dumps({"captured_symbol": symbol}), flush=True)
    publisher = Path(publisher_dir)
    registry_raw = (publisher / "registry.json").read_bytes()
    registry = load_enrolled_trust_registry_bytes(registry_raw, expected_sha256=expected_registry_sha256)
    keyfile = publisher / "publisher.key"
    if keyfile.stat().st_mode & 0o077:
        raise ValueError("publisher key must be private")
    key = Ed25519PrivateKey.from_private_bytes(keyfile.read_bytes())
    publisher_id = _key_id(_public_key(key))
    signer = registry._signers[publisher_id]
    cutoff = datetime.now(timezone.utc).isoformat()
    records, receipts = [], {}
    for symbol, roles in captures.items():
        entry = f"{symbol}@{asof}"
        values = {role: build_price_manifest(attempts, symbol=symbol, role=role, start=start, asof=asof,
                                             decision_cutoff=cutoff) for role, attempts in roles.items()}
        observed = max((m["available_at"] for m in values.values()), key=_timestamp)
        receipt = {"schema_version": "stockdata-source-receipt/1", "observed_at": observed,
                   "source": PROFILE, "response_sha256": _hash(values),
                   "bindings": [{"component": COMPONENT, "panel_entry": entry, "record_sha256": _hash(values)}]}
        receipt_id = _hash(receipt)
        receipts[receipt_id] = receipt
        records.append({"panel_entry": entry, "payload": values, "record_sha256": _hash(values),
                        "source_receipt_ids": [receipt_id], "effective_at": f"{asof}T15:00:00+08:00", "available_at": observed})
    artifact = {"schema_version": ARTIFACT_SCHEMA, "component": COMPONENT,
                "panel": [record["panel_entry"] for record in records], "records": records}
    signed_at = datetime.now(timezone.utc).isoformat()
    envelope_payload = {"component_role": COMPONENT, "artifact": {"kind": "stock-data-local-daily-prices",
                        "schema_version": ARTIFACT_SCHEMA, "identifier": _hash(artifact)},
                        "source_receipt_ids": sorted(receipts), "effective_at": signed_at, "available_at": signed_at,
                        "publisher_key_id": publisher_id, "trust_root_id": signer.trust_root_id,
                        "trust_registry_sha256": expected_registry_sha256}
    envelope = {"schema_version": AUTHORITY_ENVELOPE_SCHEMA, "algorithm": ALGORITHM,
                "payload": envelope_payload, "signature_base64": _base64(key.sign(_canonical(envelope_payload)))}
    cutoff = datetime.now(timezone.utc).isoformat()
    snapshot = {"schema_version": SCHEMA_VERSION, "asof": asof, "decision_cutoff": cutoff,
                "symbols": symbols, "registry": json.loads(registry_raw), "artifact": artifact,
                "source_receipts": receipts, "authority_envelope": envelope}
    snapshot["snapshot_sha256"] = _hash(snapshot)
    verify_local_daily_snapshot(snapshot, expected_registry_sha256=expected_registry_sha256,
                                expected_symbols=symbols, asof=asof, decision_cutoff=cutoff)
    _write_private(destination / "snapshot.json", _canonical(snapshot))
    return {"snapshot_file": str(destination / "snapshot.json"), "snapshot_sha256": snapshot["snapshot_sha256"],
            "asof": asof, "decision_cutoff": cutoff, "symbols": symbols}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--publisher-dir", required=True)
    parser.add_argument("--registry-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    result = capture_local_daily_snapshot(symbols=args.symbol, asof=args.asof, publisher_dir=args.publisher_dir,
                                          expected_registry_sha256=args.registry_sha256, output_dir=args.output_dir)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
