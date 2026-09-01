"""Strict local adapter for receipted QmtExport frozen research snapshots."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .qmt_transport_capture import _read_regular_file, _write_content_addressed


SCHEMA_VERSION = "stockdata-qmt-frozen-research-dataset/1"
RECEIPT_SCHEMA = "stockdata-qmt-raw-capture/1"
FIELDS = ("open", "high", "low", "close", "volume", "amount")
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DATASET_BYTES = 64 * 1024 * 1024
_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_KEYS = {
    "schema", "capture_id", "captured_at", "endpoint", "http_status",
    "content_type", "byte_count", "sha256", "symbol_count", "row_count",
    "generated", "validation_status", "validation_errors", "source", "authority",
}
_RECEIPT_AUTHORITY = {
    "advice": False, "evidence_grade": False, "judge": False,
    "production": False, "release": False,
}
_DATASET_AUTHORITY = {
    "advice": False, "decision": False, "evidence": False, "judge": False,
    "production": False, "release": False,
}
_LATEST_KEYS = {"account", "account_id", "errors", "generated", "market", "request"}


class QmtFrozenResearchError(RuntimeError):
    """A local QMT capture cannot be admitted as frozen research data."""


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QmtFrozenResearchError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise QmtFrozenResearchError(f"non-finite JSON number: {value}")


def _strict_json(raw: bytes, label: str) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QmtFrozenResearchError(f"{label} is not strict JSON") from exc


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise QmtFrozenResearchError("value is not canonical JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QmtFrozenResearchError(f"{field} must be a positive integer")
    return value


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise QmtFrozenResearchError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QmtFrozenResearchError(f"{field} must be a timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QmtFrozenResearchError(f"{field} must include a timezone")
    return value


def _generated(value: object) -> str:
    if not isinstance(value, str):
        raise QmtFrozenResearchError("generated must be a producer timestamp")
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            datetime.strptime(value, pattern)
            return value
        except ValueError:
            continue
    raise QmtFrozenResearchError("generated must be a producer timestamp")


def _number(value: object, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(float(value)):
        raise QmtFrozenResearchError(f"{field} must be finite numeric data")
    return value


def _validate_receipt(receipt: object, latest_raw: bytes) -> dict[str, object]:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        raise QmtFrozenResearchError("QMT raw capture receipt schema is incomplete")
    if receipt.get("schema") != RECEIPT_SCHEMA \
            or receipt.get("endpoint") != "/latest" \
            or receipt.get("http_status") != 200 \
            or receipt.get("validation_status") != "verified" \
            or receipt.get("validation_errors") != [] \
            or receipt.get("authority") != _RECEIPT_AUTHORITY:
        raise QmtFrozenResearchError("QMT raw capture receipt contract is invalid")
    if not isinstance(receipt.get("capture_id"), str) or not receipt["capture_id"] \
            or not isinstance(receipt.get("content_type"), str) \
            or not receipt["content_type"].lower().startswith("application/json") \
            or not isinstance(receipt.get("source"), str) or not receipt["source"]:
        raise QmtFrozenResearchError("QMT raw capture receipt identity is invalid")
    _timestamp(receipt.get("captured_at"), "captured_at")
    _generated(receipt.get("generated"))
    _positive_integer(receipt.get("symbol_count"), "symbol_count")
    _positive_integer(receipt.get("row_count"), "row_count")
    byte_count = _positive_integer(receipt.get("byte_count"), "byte_count")
    digest = receipt.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest) \
            or byte_count != len(latest_raw) \
            or digest != hashlib.sha256(latest_raw).hexdigest():
        raise QmtFrozenResearchError("latest.json bytes do not match receipt")
    return receipt


def _symbols(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise QmtFrozenResearchError("request symbols must be a non-empty list")
    if any(not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) for symbol in value) \
            or len(set(value)) != len(value):
        raise QmtFrozenResearchError("request symbols are invalid")
    return sorted(value)


def _validate_market(
    latest: object,
) -> tuple[dict[str, object], list[str], int, int, int]:
    if not isinstance(latest, dict) or set(latest) != _LATEST_KEYS \
            or latest.get("errors") != []:
        raise QmtFrozenResearchError("latest.json contract is invalid")
    generated = _generated(latest.get("generated"))
    request = latest.get("request")
    if not isinstance(request, dict) or set(request) != {"count", "fields", "period", "symbols"} \
            or request.get("period") != "1d" or request.get("fields") != list(FIELDS):
        raise QmtFrozenResearchError("QMT source request contract is invalid")
    count = _positive_integer(request.get("count"), "request.count")
    symbols = _symbols(request.get("symbols"))
    market = latest.get("market")
    if not isinstance(market, dict) or set(market) != set(symbols):
        raise QmtFrozenResearchError("request and market membership differ")
    source_row_count = 0
    accepted_row_count = 0
    dropped_zero_placeholder_count = 0
    normalized_market: dict[str, object] = {}
    for symbol in symbols:
        record = market[symbol]
        if not isinstance(record, dict) or set(record) != {"index", "columns"}:
            raise QmtFrozenResearchError(f"{symbol} market schema is invalid")
        index = record.get("index")
        columns = record.get("columns")
        if not isinstance(index, list) or not index or len(index) > count \
                or not isinstance(columns, dict) or set(columns) != set(FIELDS) \
                or any(not isinstance(columns[field], list)
                       or len(columns[field]) != len(index) for field in FIELDS):
            raise QmtFrozenResearchError(f"{symbol} market columns are invalid")
        days: list[str] = []
        accepted_days: list[str] = []
        accepted_columns = {field: [] for field in FIELDS}
        for offset, day in enumerate(index):
            if not isinstance(day, str) or not re.fullmatch(r"[0-9]{8}", day):
                raise QmtFrozenResearchError(f"{symbol} date is invalid")
            try:
                parsed = datetime.strptime(day, "%Y%m%d")
            except ValueError as exc:
                raise QmtFrozenResearchError(f"{symbol} date is invalid") from exc
            if parsed.strftime("%Y%m%d") != day:
                raise QmtFrozenResearchError(f"{symbol} date is invalid")
            days.append(day)
            values = {
                field: _number(columns[field][offset], f"{symbol}.{field}")
                for field in FIELDS
            }
            zero_placeholder = all(values[field] == 0 for field in FIELDS)
            if zero_placeholder:
                dropped_zero_placeholder_count += 1
                continue
            invalid_bar = any(
                values[field] <= 0 for field in ("open", "high", "low", "close")
            ) \
                    or values["volume"] < 0 or values["amount"] < 0 \
                    or values["high"] < max(values["open"], values["close"]) \
                    or values["low"] > min(values["open"], values["close"]) \
                    or values["low"] > values["high"]
            if invalid_bar:
                raise QmtFrozenResearchError(f"{symbol} OHLCVA is invalid")
            accepted_days.append(day)
            for field in FIELDS:
                accepted_columns[field].append(values[field])
        if days != sorted(set(days)):
            raise QmtFrozenResearchError(f"{symbol} dates must be unique and increasing")
        if not accepted_days:
            raise QmtFrozenResearchError(f"{symbol} has no usable research rows")
        source_row_count += len(index)
        accepted_row_count += len(accepted_days)
        normalized_market[symbol] = {
            "index": accepted_days,
            "columns": accepted_columns,
        }
    normalized_request = {
        "count": count, "fields": list(FIELDS), "period": "1d", "symbols": symbols,
    }
    normalized = {
        "generated": generated,
        "request": normalized_request,
        "market": normalized_market,
    }
    return (
        normalized,
        symbols,
        source_row_count,
        accepted_row_count,
        dropped_zero_placeholder_count,
    )


def _unsigned_dataset(
    normalized: dict[str, object], receipt: dict[str, object],
    symbols: list[str], source_row_count: int, row_count: int,
    dropped_zero_placeholder_count: int,
) -> dict[str, object]:
    source_receipt = {key: receipt[key] for key in sorted(_RECEIPT_KEYS)}
    return {
        "schema_version": SCHEMA_VERSION,
        "authority_grade": "shadow",
        "research_only": True,
        "authority": dict(_DATASET_AUTHORITY),
        "permitted_uses": ["offline_research", "strategy_backtest"],
        "price_identity": {
            "adjustment": "unbound", "volume_unit": "unbound",
            "amount_unit": "unbound", "finality": "unverified",
        },
        "source_binding": {
            "capture_receipt": source_receipt,
            "raw_latest": {
                "byte_count": receipt["byte_count"], "sha256": receipt["sha256"],
            },
            "source_declared": receipt["source"],
            "transport_topology": "qmt_cloud_relay_unverified",
            "generated_timezone": "unbound",
        },
        "generated": normalized["generated"],
        "request": normalized["request"],
        "symbol_count": len(symbols),
        "source_row_count": source_row_count,
        "row_count": row_count,
        "dropped_zero_placeholder_count": dropped_zero_placeholder_count,
        "membership_sha256": _sha256(symbols),
        "market": normalized["market"],
    }


def build_qmt_frozen_research(
    latest_path: str | Path, receipt_path: str | Path,
) -> dict[str, object]:
    """Build one self-verifying shadow dataset from two already-local files."""
    latest_raw = _read_regular_file(latest_path, MAX_SOURCE_BYTES, QmtFrozenResearchError)
    receipt_raw = _read_regular_file(receipt_path, MAX_SOURCE_BYTES, QmtFrozenResearchError)
    receipt = _validate_receipt(_strict_json(receipt_raw, "receipt.json"), latest_raw)
    normalized, symbols, source_row_count, row_count, dropped = _validate_market(
        _strict_json(latest_raw, "latest.json")
    )
    if receipt["generated"] != normalized["generated"] \
            or receipt["symbol_count"] != len(symbols) \
            or receipt["row_count"] != source_row_count:
        raise QmtFrozenResearchError("receipt does not match latest.json content")
    unsigned = _unsigned_dataset(
        normalized, receipt, symbols, source_row_count, row_count, dropped
    )
    dataset = {**unsigned, "dataset_sha256": _sha256(unsigned)}
    if len(_canonical(dataset)) > MAX_DATASET_BYTES:
        raise QmtFrozenResearchError("QMT frozen dataset byte cap exceeded")
    return dataset


def verify_qmt_frozen_research(payload: object) -> dict[str, object]:
    """Recompute every dataset semantic and content binding available offline."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "authority_grade", "research_only", "authority",
        "permitted_uses", "price_identity", "source_binding", "generated", "request",
        "symbol_count", "source_row_count", "row_count",
        "dropped_zero_placeholder_count", "membership_sha256", "market",
        "dataset_sha256",
    }:
        raise QmtFrozenResearchError("QMT frozen dataset schema is incomplete")
    supplied = payload.get("dataset_sha256")
    unsigned = {key: value for key, value in payload.items() if key != "dataset_sha256"}
    if not isinstance(supplied, str) or supplied != _sha256(unsigned) \
            or len(_canonical(payload)) > MAX_DATASET_BYTES:
        raise QmtFrozenResearchError("QMT frozen dataset seal is invalid")
    if unsigned.get("schema_version") != SCHEMA_VERSION \
            or unsigned.get("authority_grade") != "shadow" \
            or unsigned.get("research_only") is not True \
            or unsigned.get("authority") != _DATASET_AUTHORITY \
            or unsigned.get("permitted_uses") != ["offline_research", "strategy_backtest"] \
            or unsigned.get("price_identity") != {
                "adjustment": "unbound", "volume_unit": "unbound",
                "amount_unit": "unbound", "finality": "unverified",
            }:
        raise QmtFrozenResearchError("QMT frozen dataset authority is invalid")
    source = unsigned.get("source_binding")
    if not isinstance(source, dict) or set(source) != {
        "capture_receipt", "raw_latest", "source_declared", "transport_topology",
        "generated_timezone",
    } or source.get("transport_topology") != "qmt_cloud_relay_unverified" \
            or source.get("generated_timezone") != "unbound":
        raise QmtFrozenResearchError("QMT frozen dataset source binding is invalid")
    receipt = source.get("capture_receipt")
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS \
            or receipt.get("schema") != RECEIPT_SCHEMA \
            or receipt.get("authority") != _RECEIPT_AUTHORITY \
            or receipt.get("endpoint") != "/latest" \
            or receipt.get("http_status") != 200 \
            or receipt.get("validation_status") != "verified" \
            or receipt.get("validation_errors") != [] \
            or source.get("source_declared") != receipt.get("source") \
            or source.get("raw_latest") != {
                "byte_count": receipt.get("byte_count"), "sha256": receipt.get("sha256"),
            }:
        raise QmtFrozenResearchError("QMT frozen dataset source receipt is invalid")
    if not isinstance(receipt.get("capture_id"), str) or not receipt["capture_id"] \
            or not isinstance(receipt.get("content_type"), str) \
            or not receipt["content_type"].lower().startswith("application/json") \
            or not isinstance(receipt.get("source"), str) or not receipt["source"] \
            or not isinstance(receipt.get("sha256"), str) \
            or not _SHA256.fullmatch(receipt["sha256"]):
        raise QmtFrozenResearchError("QMT frozen dataset source receipt is invalid")
    _timestamp(receipt.get("captured_at"), "captured_at")
    _generated(receipt.get("generated"))
    _positive_integer(receipt.get("byte_count"), "byte_count")
    _positive_integer(receipt.get("symbol_count"), "symbol_count")
    _positive_integer(receipt.get("row_count"), "row_count")
    latest_projection = {
        "account": {}, "account_id": "stripped", "errors": [],
        "generated": unsigned.get("generated"), "request": unsigned.get("request"),
        "market": unsigned.get("market"),
    }
    normalized, symbols, projected_rows, row_count, projected_dropped = \
        _validate_market(latest_projection)
    source_row_count = _positive_integer(
        unsigned.get("source_row_count"), "source_row_count"
    )
    dropped = unsigned.get("dropped_zero_placeholder_count")
    if isinstance(dropped, bool) or not isinstance(dropped, int) or dropped < 0:
        raise QmtFrozenResearchError(
            "dropped_zero_placeholder_count must be a non-negative integer"
        )
    if receipt["generated"] != normalized["generated"] \
            or receipt["symbol_count"] != len(symbols) \
            or receipt["row_count"] != source_row_count \
            or source_row_count != row_count + dropped \
            or projected_rows != row_count or projected_dropped != 0:
        raise QmtFrozenResearchError("QMT frozen dataset source receipt is invalid")
    if unsigned != _unsigned_dataset(
        normalized, receipt, symbols, source_row_count, row_count, dropped
    ):
        raise QmtFrozenResearchError("QMT frozen dataset content is invalid")
    return payload


def load_qmt_frozen_research(path: str | Path) -> dict[str, object]:
    """Load a canonical frozen dataset and verify its seal and semantics."""
    raw = _read_regular_file(path, MAX_DATASET_BYTES, QmtFrozenResearchError)
    payload = _strict_json(raw, "QMT frozen dataset")
    if raw != _canonical(payload):
        raise QmtFrozenResearchError("QMT frozen dataset bytes are not canonical")
    return verify_qmt_frozen_research(payload)


def write_qmt_frozen_research(output_root: str | Path, dataset: dict) -> Path:
    """Durably create or byte-verify one content-addressed dataset."""
    dataset = verify_qmt_frozen_research(dataset)
    return _write_content_addressed(
        output_root, dataset["dataset_sha256"], _canonical(dataset),
        QmtFrozenResearchError,
    )


__all__ = [
    "QmtFrozenResearchError", "SCHEMA_VERSION", "build_qmt_frozen_research",
    "load_qmt_frozen_research", "verify_qmt_frozen_research",
    "write_qmt_frozen_research",
]
