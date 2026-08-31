"""Complete shadow DataProduct closure derived from QMT transport v2 evidence."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .qmt_transport_capture import (
    FIELDS,
    PERMITTED_USES,
    QmtTransportCaptureError,
    _canonical,
    _duplicate_keys,
    _reject_constant,
    _read_regular_file,
    _verify_capture,
    _write_content_addressed,
    MAX_PRODUCT_BYTES,
)


PRODUCT_SCHEMA_VERSION = "stockdata-qmt-daily-bar-product/2"


class QmtDailyBarProductError(ValueError):
    """A capture lacks the closure required for a shadow DataProduct."""


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _permitted_uses() -> list[str]:
    return list(PERMITTED_USES)


def _product_content(capture: dict, item: dict) -> dict:
    return {
        "symbol": item["symbol"],
        "period": capture["request"]["period"],
        "fields": list(FIELDS),
        "adjustment": capture["request"]["adjustment"],
        "qmt_parameter": capture["request"]["qmt_parameter"],
        "fill_data": False,
        "volume_unit": capture["volume_unit"],
        "amount_unit": capture["amount_unit"],
        "coverage": copy.deepcopy(item["coverage"]),
        "finality": copy.deepcopy(item["finality"]),
        "errors": [],
        "row_projection": {
            "capture_symbol": item["symbol"],
            "rows_sha256": item["rows_sha256"],
        },
    }


def _unsigned_product(capture: dict) -> dict:
    products = []
    for item in capture["symbols"]:
        content = _product_content(capture, item)
        products.append({**content, "content_sha256": _sha256(content)})
    first_dates = [product["coverage"]["start"] for product in products]
    last_dates = [product["coverage"]["end"] for product in products]
    content_hash = _sha256(products)
    request = capture["request"]
    scope = {
        "symbols": sorted(request["symbols"]), "period": request["period"],
        "fields": request["fields"], "adjustment": request["adjustment"],
        "qmt_parameter": request["qmt_parameter"], "fill_data": False,
        "volume_unit": capture["volume_unit"], "amount_unit": capture["amount_unit"],
    }
    scope_hash = _sha256(scope)
    version = _sha256({
        "content_hash": content_hash, "source_receipt_id": capture["snapshot_sha256"],
    })
    return {
        "schema_version": PRODUCT_SCHEMA_VERSION,
        "data_product_id": f"qmt-daily-bars:{scope_hash}",
        "version": version,
        "schema_id": "ohlcv-daily/1",
        "authority_grade": "shadow",
        "decision_eligible": False,
        "decision_authority": False,
        "actions": [],
        "permitted_uses": _permitted_uses(),
        "source_id": "qmt_loopback_transport_v2",
        "source_authentication": "shared_token_unverified",
        "quality_status": "source_marked_final_unverified",
        "content_hash": content_hash,
        "source_receipt_ids": [capture["snapshot_sha256"]],
        "event_time_range": {"start": min(first_dates), "end": max(last_dates)},
        "pit_mode": "current_observation",
        "corporate_action_version": "not_bound",
        "price_treatment": {
            "adjustment": request["adjustment"],
            "qmt_parameter": request["qmt_parameter"],
            "fill_data": False,
        },
        "universe_version": "not_bound",
        "trading_calendar_version": "not_bound",
        "lineage_ids": [],
        "generated_at": capture["generated_at"],
        "available_at": capture["available_at"],
        "request": copy.deepcopy(request),
        "request_sha256": capture["request_sha256"],
        "capture_closure": copy.deepcopy(capture),
        "products": products,
    }


def build_qmt_daily_bar_product(capture: object) -> dict:
    """Create a self-verifying, content-addressed DataProduct in memory."""
    try:
        verified = copy.deepcopy(_verify_capture(capture))
    except QmtTransportCaptureError as exc:
        raise QmtDailyBarProductError("QMT transport capture is invalid") from exc
    unsigned = _unsigned_product(verified)
    product = {**unsigned, "product_sha256": _sha256(unsigned)}
    if len(_canonical(product)) > MAX_PRODUCT_BYTES:
        raise QmtDailyBarProductError("QMT daily-bar product byte cap exceeded")
    return product


def verify_qmt_daily_bar_product(payload: object) -> dict:
    """Verify the product by rebuilding every derived field from its closure."""
    if not isinstance(payload, dict) or "product_sha256" not in payload:
        raise QmtDailyBarProductError("QMT daily-bar product schema is incomplete")
    if len(_canonical(payload)) > MAX_PRODUCT_BYTES:
        raise QmtDailyBarProductError("QMT daily-bar product byte cap exceeded")
    supplied = payload.get("product_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "product_sha256"}
    if not isinstance(supplied, str) or supplied != _sha256(unsigned):
        raise QmtDailyBarProductError("QMT daily-bar product hash is invalid")
    try:
        capture = _verify_capture(unsigned.get("capture_closure"))
    except QmtTransportCaptureError as exc:
        raise QmtDailyBarProductError("QMT daily-bar capture closure is invalid") from exc
    if unsigned != _unsigned_product(capture):
        raise QmtDailyBarProductError("QMT daily-bar product does not match its capture closure")
    return payload


def load_qmt_daily_bar_product(path: str | Path) -> dict:
    """Load canonical JSON while rejecting duplicate keys and noncanonical bytes."""
    raw = _read_regular_file(path, MAX_PRODUCT_BYTES, QmtDailyBarProductError)
    try:
        payload = json.loads(
            raw.decode("ascii"), object_pairs_hook=_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, QmtTransportCaptureError) as exc:
        raise QmtDailyBarProductError("QMT daily-bar product is unreadable") from exc
    if raw != _canonical(payload):
        raise QmtDailyBarProductError("QMT daily-bar product bytes are not canonical")
    return verify_qmt_daily_bar_product(payload)


def write_qmt_daily_bar_product(output_root: str | Path, product: dict) -> Path:
    product = verify_qmt_daily_bar_product(product)
    raw = _canonical(product)
    if len(raw) > MAX_PRODUCT_BYTES:
        raise QmtDailyBarProductError("QMT daily-bar product byte cap exceeded")
    return _write_content_addressed(
        output_root, product["product_sha256"], raw,
        QmtDailyBarProductError,
    )


__all__ = [
    "PRODUCT_SCHEMA_VERSION", "QmtDailyBarProductError", "build_qmt_daily_bar_product",
    "load_qmt_daily_bar_product", "verify_qmt_daily_bar_product",
    "write_qmt_daily_bar_product",
]
