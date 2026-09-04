"""Receipt-replayed CNY amounts; authority requires a trusted signed envelope."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path

from .ticker import normalize


IDENTITY = {
    "source": "baostock", "field": "amount", "amount_unit": "CNY",
    "adjustment_mode": "raw", "adjustment_version": "baostock-adjustflag-3",
}
# Exact issuer-confirmed ETF identities, not security-code prefix inference.
ETF_SOURCES = {
    "159980.SZ": "https://www.dcfund.com.cn/home/working/download/autoupload/1742473215380.pdf",
    "561980.SH": "https://static.cmfchina.com/web/noticedetails/225146/index.html",
}


class LiquidityAmountProductError(ValueError):
    """The captured native amounts cannot support the requested panel."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise LiquidityAmountProductError("product is not canonical JSON") from exc


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _day(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except (TypeError, ValueError):
        pass
    raise LiquidityAmountProductError("date must be canonical ISO date")


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is not None:
            return parsed
    except (AttributeError, TypeError, ValueError):
        pass
    raise LiquidityAmountProductError("timestamp must be timezone aware")


def build_liquidity_amounts_product(
    captures, *, panel, decision_cutoff: str, expected_watermark: str,
) -> dict:
    """Build a complete independently receipted native-amount authority candidate."""
    cutoff = _timestamp(decision_cutoff)
    watermark = _day(expected_watermark)
    pairs = [(normalize(code), _day(day)) for code, day in panel]
    if not pairs or len(set(pairs)) != len(pairs):
        raise LiquidityAmountProductError("exact panel must be nonempty and unique")
    pairs.sort()
    codes = sorted({code for code, _ in pairs})
    if any(code not in ETF_SOURCES for code in codes):
        raise LiquidityAmountProductError("ETF identity lacks an approved issuer source")
    for code in codes:
        days = [day for symbol, day in pairs if symbol == code]
        if len(days) < 20:
            raise LiquidityAmountProductError("each ETF requires 20 observations")
        if max(days) != watermark:
            raise LiquidityAmountProductError("panel does not reach expected watermark")

    receipts, amounts = {}, {}
    for capture in captures:
        capture = deepcopy(capture)
        request, response = capture["request"], capture["response"]
        if (capture.get("source") != "baostock"
                or request.get("method") != "query_history_k_data_plus"
                or request.get("frequency") != "d" or request.get("adjustflag") != "3"):
            raise LiquidityAmountProductError("liquidity identity requires raw baostock")
        observed = _timestamp(capture["observed_at"])
        if observed > cutoff:
            raise LiquidityAmountProductError("receipt observed after decision cutoff")
        exchange, separator, digits = request["code"].partition(".")
        if separator != "." or exchange not in {"sh", "sz"}:
            raise LiquidityAmountProductError("receipt security code is invalid")
        code = normalize(f"{digits}.{exchange}")
        fields = response.get("fields", "")
        fields = fields.split(",") if isinstance(fields, str) else fields
        requested = request.get("fields", "").split(",")
        if (not isinstance(fields, list) or len(fields) != len(set(fields))
                or fields != requested or "amount" not in fields or "date" not in fields):
            raise LiquidityAmountProductError("receipt requires native amount field")
        start, end = _day(request["start_date"]), _day(request["end_date"])
        receipt = {key: capture[key] for key in ("source", "observed_at", "request", "response")}
        receipt["response_sha256"] = _hash(response)
        receipt_id = _hash(receipt)
        if receipt_id in receipts:
            raise LiquidityAmountProductError("duplicate source receipt")
        receipts[receipt_id] = {"source_receipt_id": receipt_id, **receipt}
        for raw in response["rows"]:
            if not isinstance(raw, list) or len(raw) != len(fields):
                raise LiquidityAmountProductError("native amount response row is malformed")
            row = dict(zip(fields, raw))
            day = _day(row["date"])
            if not start <= day <= end:
                raise LiquidityAmountProductError("receipt request excludes native amount row")
            if (code, day) not in pairs:
                continue
            close = datetime.combine(date.fromisoformat(day), time(15),
                                     tzinfo=timezone(timedelta(hours=8)))
            if day > watermark or observed < close or cutoff < close:
                raise LiquidityAmountProductError("native amount is not final at watermark")
            try:
                amount = float(row["amount"])
            except (TypeError, ValueError) as exc:
                raise LiquidityAmountProductError("native amount is invalid") from exc
            if isinstance(row["amount"], bool) or not math.isfinite(amount) or amount <= 0:
                raise LiquidityAmountProductError("native amount must be finite and positive")
            if (code, day) in amounts:
                raise LiquidityAmountProductError("duplicate native amount panel entry")
            payload = {"amount": amount, "amount_unit": "CNY"}
            amounts[(code, day)] = {
                "panel_entry": f"{code}@{day}", "code": code, "date": day,
                "payload": payload, "record_sha256": _hash(payload),
                "source_receipt_ids": [receipt_id],
                "effective_at": close.isoformat(),
                "available_at": capture["observed_at"],
            }
    if set(amounts) != set(pairs):
        raise LiquidityAmountProductError("native amount does not cover exact panel")
    product = {
        "schema_version": "stockdata-liquidity-amounts/1", "component": "liquidity_amounts",
        "authority_grade": "formal", "decision_eligible": True, "decision_authority": True,
        "source_authentication": "requires_trusted_signed_envelope",
        "amount_unit": "CNY", "liquidity_identity": deepcopy(IDENTITY),
        "instrument_scope": {"instrument_type": "ETF", "codes": codes,
                             "minimum_observations_per_code": 20},
        "instrument_sources": {code: ETF_SOURCES[code] for code in codes},
        "panel": [f"{code}@{day}" for code, day in pairs],
        "decision_cutoff": decision_cutoff,
        "finality": {"status": "decision_watermark_bound", "watermark": watermark},
        "records": [amounts[pair] for pair in pairs],
        "source_receipts": [receipts[key] for key in sorted(receipts)],
    }
    return {**product, "product_sha256": _hash(product)}


def verify_liquidity_amounts_product(
    product: dict, *, expected_panel=None, decision_cutoff=None, expected_watermark=None,
) -> dict:
    unsigned = {key: value for key, value in product.items() if key != "product_sha256"}
    if product.get("product_sha256") != _hash(unsigned):
        raise LiquidityAmountProductError("product hash mismatch")
    if product.get("amount_unit") != "CNY":
        raise LiquidityAmountProductError("amount unit must be CNY")
    if product.get("liquidity_identity") != IDENTITY:
        raise LiquidityAmountProductError("liquidity identity mismatch")
    panel = expected_panel if expected_panel is not None else [
        entry.split("@") for entry in product["panel"]
    ]
    rebuilt = build_liquidity_amounts_product(
        product["source_receipts"], panel=panel,
        decision_cutoff=decision_cutoff or product["decision_cutoff"],
        expected_watermark=expected_watermark or product["finality"]["watermark"],
    )
    if product != rebuilt:
        raise LiquidityAmountProductError("native amount product does not replay receipts")
    return product


def write_liquidity_amounts_product(output_root: str | Path, product: dict) -> Path:
    verify_liquidity_amounts_product(product)
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{product['product_sha256']}.json"
    content = _canonical(product)
    try:
        with target.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if target.read_bytes() != content:
            raise LiquidityAmountProductError("existing product bytes differ")
    return target


def load_liquidity_amounts_product(path: str | Path) -> dict:
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LiquidityAmountProductError("product is not JSON") from exc
    if raw != _canonical(value):
        raise LiquidityAmountProductError("product bytes are not canonical")
    return verify_liquidity_amounts_product(value)
