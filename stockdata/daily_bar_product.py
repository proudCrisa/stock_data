"""Shadow-only receipted daily-bar DataProduct export."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
from datetime import date, datetime, timezone
from pathlib import Path

from .ticker import normalize


PRODUCT_SCHEMA = "stockdata-daily-bar-product/1"
MANIFEST_SCHEMA = "stockdata-data-manifest/1"
ROW_SCHEMA = "ohlcv-daily/1"
PERMITTED_USES = ["offline_replay", "shadow_compare"]
QUALITY_STATUS = "self_consistent_current_observation"
_ADJUST_FLAGS = {"qfq": "2", "raw": "3"}
_ROW_FIELDS = ("date", "open", "high", "low", "close", "volume")


class DailyBarProductError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _timestamp(value: object, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise DailyBarProductError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DailyBarProductError(f"{field} must include timezone")
    return str(value)


def _timestamp_value(value: object, field: str) -> datetime:
    _timestamp(value, field)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _date(value: object, field: str) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise DailyBarProductError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise DailyBarProductError(f"{field} must be a canonical ISO date")
    return str(value)


def _request_code(request: dict) -> str:
    value = str(request.get("code") or "").strip()
    if len(value) == 9 and value[2] == "." \
            and value[:2].lower() in {"sh", "sz", "bj"} \
            and value[3:].isdigit():
        return f"{value[3:]}.{value[:2].upper()}"
    try:
        return normalize(value)
    except ValueError:
        return ""


def _response_covers(response: dict, database_row: sqlite3.Row) -> bool:
    fields = response.get("fields")
    if isinstance(fields, str):
        fields = [item.strip() for item in fields.split(",")]
    rows = response.get("rows")
    if not isinstance(fields, list) or not isinstance(rows, list):
        return False
    positions = {str(name): index for index, name in enumerate(fields)}
    if any(field not in positions for field in _ROW_FIELDS):
        return False
    for values in rows:
        if not isinstance(values, list):
            continue
        try:
            if str(values[positions["date"]]) != str(database_row["date"]):
                continue
            if all(
                float(values[positions[field]]) == float(database_row[field])
                for field in _ROW_FIELDS[1:]
            ):
                return True
        except (IndexError, TypeError, ValueError):
            continue
    return False


def _validate_bar(row: sqlite3.Row) -> None:
    day = _date(row["date"], "row.date")
    values = {field: float(row[field]) for field in _ROW_FIELDS[1:]}
    if any(not math.isfinite(value) for value in values.values()) \
            or values["open"] <= 0 or values["high"] <= 0 \
            or values["low"] <= 0 or values["close"] <= 0 \
            or values["volume"] < 0 \
            or values["high"] < max(values["open"], values["close"]) \
            or values["low"] > min(values["open"], values["close"]) \
            or values["high"] < values["low"]:
        raise DailyBarProductError(f"daily-bar OHLCV is invalid: {day}")


def build_daily_bar_manifest(
    database: str | Path,
    *,
    code: str,
    start: str,
    end: str,
    source: str,
    adjustment_mode: str,
    adjustment_version: str,
    universe_version: str,
    trading_calendar_version: str,
    created_at: str | None = None,
) -> dict:
    """Build one portable, shadow-only manifest from exact receipted rows."""
    database = Path(database).expanduser().resolve()
    if not database.is_file():
        raise DailyBarProductError(f"database not found: {database}")
    canonical_code = normalize(code)
    start = _date(start, "start")
    end = _date(end, "end")
    if start > end:
        raise DailyBarProductError("start must not exceed end")
    if source != "baostock" or adjustment_mode not in _ADJUST_FLAGS:
        raise DailyBarProductError("daily-bar v1 supports receipted baostock raw/qfq only")
    expected_version = f"baostock-adjustflag-{_ADJUST_FLAGS[adjustment_mode]}"
    if adjustment_version != expected_version:
        raise DailyBarProductError("adjustment version does not match mode")
    if not isinstance(universe_version, str) or not universe_version:
        raise DailyBarProductError("universe_version is required")
    if not isinstance(trading_calendar_version, str) \
            or not trading_calendar_version:
        raise DailyBarProductError("trading_calendar_version is required")
    created_at = _timestamp(
        created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "created_at",
    )
    decision_cutoff = _timestamp_value(created_at, "created_at")

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT d.date,d.open,d.high,d.low,d.close,d.volume,d.retrieved_at,
                   d.is_final,d.receipt_id,r.observed_at,r.source AS receipt_source,
                   r.request_json,r.response_json,r.response_sha256,r.created_at
            FROM daily AS d
            JOIN collection_receipts AS r ON r.receipt_id=d.receipt_id
            WHERE d.code=? AND d.date>=? AND d.date<=? AND d.source=?
              AND d.adjustment_mode=? AND d.adjustment_version=?
            ORDER BY d.date
            """,
            (
                canonical_code, start, end, source, adjustment_mode,
                adjustment_version,
            ),
        ).fetchall()
    except sqlite3.Error as exc:
        raise DailyBarProductError("daily-bar database schema is unavailable") from exc
    finally:
        connection.close()
    if not rows or str(rows[0]["date"]) != start or str(rows[-1]["date"]) != end:
        raise DailyBarProductError("receipted rows do not cover exact requested boundaries")

    product_rows = []
    receipts: dict[str, dict] = {}
    for row in rows:
        _validate_bar(row)
        if int(row["is_final"]) != 1 or row["receipt_id"] is None:
            raise DailyBarProductError(f"row is not final and receipted: {row['date']}")
        try:
            request = json.loads(str(row["request_json"]))
            response = json.loads(str(row["response_json"]))
        except json.JSONDecodeError as exc:
            raise DailyBarProductError("collection receipt is not JSON") from exc
        response_hash = hashlib.sha256(
            str(row["response_json"]).encode("utf-8")
        ).hexdigest()
        if response_hash != str(row["response_sha256"]):
            raise DailyBarProductError("collection receipt response hash mismatch")
        if str(row["receipt_source"]) != source \
                or _request_code(request) != canonical_code \
                or str(request.get("adjustflag") or "") != _ADJUST_FLAGS[adjustment_mode] \
                or not _response_covers(response, row):
            raise DailyBarProductError(
                f"collection receipt does not replay row {canonical_code}@{row['date']}"
            )
        request_start = _date(request.get("start_date"), "receipt.request.start_date")
        request_end = _date(request.get("end_date"), "receipt.request.end_date")
        if request_start > str(row["date"]) or request_end < str(row["date"]):
            raise DailyBarProductError("collection receipt request range excludes row")
        observed_at = _timestamp(row["observed_at"], "receipt.observed_at")
        retrieved_at = _timestamp(row["retrieved_at"], "row.retrieved_at")
        if _timestamp_value(observed_at, "receipt.observed_at") > decision_cutoff:
            raise DailyBarProductError("receipt observed after decision cutoff")
        if observed_at != retrieved_at:
            raise DailyBarProductError("row retrieved_at differs from receipt observed_at")
        receipt = {
            "observed_at": observed_at,
            "source": source,
            "request": request,
            "response": response,
            "response_sha256": str(row["response_sha256"]),
        }
        receipt_id = _hash(receipt)
        receipts.setdefault(receipt_id, {"source_receipt_id": receipt_id, **receipt})
        product_rows.append({
            "date": str(row["date"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "retrieved_at": retrieved_at,
            "is_final": True,
            "source_receipt_id": receipt_id,
        })
    if len({row["date"] for row in product_rows}) != len(product_rows):
        raise DailyBarProductError("daily-bar product contains duplicate dates")

    content_hash = _hash(product_rows)
    receipt_ids = sorted(receipts)
    available_at = max(
        receipts.values(),
        key=lambda receipt: _timestamp_value(
            receipt["observed_at"], "receipt.observed_at"
        ),
    )["observed_at"]
    product = {
        "schema_version": PRODUCT_SCHEMA,
        "data_product_id": (
            f"daily-bars:{canonical_code}:{source}:{adjustment_mode}:"
            f"{adjustment_version}"
        ),
        "version": content_hash,
        "schema_id": ROW_SCHEMA,
        "authority_grade": "shadow",
        "decision_eligible": False,
        "source_authentication": "unverified",
        "reference_binding_status": "declared_unverified",
        "instrument_scope": {"codes": [canonical_code], "start": start, "end": end},
        "event_time_range": {"start": product_rows[0]["date"],
                             "end": product_rows[-1]["date"]},
        "content_hash": content_hash,
        "source_receipt_ids": receipt_ids,
        "available_at": available_at,
        "finality": {
            "status": "source_marked_final",
            "watermark": product_rows[-1]["date"],
        },
        "pit_mode": "current_observation",
        "corporate_action_version": "not_bound",
        "universe_version": universe_version,
        "trading_calendar_version": trading_calendar_version,
        "quality_grade": QUALITY_STATUS,
        "permitted_uses": PERMITTED_USES,
        "lineage_ids": [],
        "price_identity": {
            "source": source,
            "adjustment_mode": adjustment_mode,
            "adjustment_version": adjustment_version,
            "volume_unit": "share",
        },
        "rows": product_rows,
        "source_receipts": [receipts[key] for key in receipt_ids],
    }
    product["product_sha256"] = _hash(product)
    manifest_identity = {
        "schema_version": MANIFEST_SCHEMA,
        "authority_grade": "shadow",
        "decision_eligible": False,
        "source_authentication": "unverified",
        "reference_binding_status": "declared_unverified",
        "created_at": created_at,
        "decision_cutoff": created_at,
        "dataset_ids": [product["data_product_id"]],
        "receipt_ids": receipt_ids,
        "content_hashes": [product["product_sha256"]],
        "provider_authorities": [],
        "claimed_sources": [source],
        "instrument_universe_version": universe_version,
        "trading_calendar_version": trading_calendar_version,
        "corporate_action_version": "not_bound",
        "available_at": product["available_at"],
        "finality": "source_marked_final",
        "quality_status": QUALITY_STATUS,
        "fallback_status": "not_used",
        "permitted_uses": PERMITTED_USES,
        "products": [product],
    }
    manifest_sha = _hash(manifest_identity)
    return {
        **manifest_identity,
        "manifest_id": f"shadow-{manifest_sha}",
        "manifest_sha256": manifest_sha,
    }


def write_daily_bar_manifest(output_root: str | Path, manifest: dict) -> Path:
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    digest = str(manifest.get("manifest_sha256") or "")
    unsigned = {
        key: value for key, value in manifest.items()
        if key not in {"manifest_id", "manifest_sha256"}
    }
    if digest != _hash(unsigned) \
            or manifest.get("manifest_id") != f"shadow-{digest}":
        raise DailyBarProductError("manifest is not sealed")
    target = root / f"{digest}.json"
    raw = _canonical(manifest)
    if target.exists():
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 \
                or target.read_bytes() != raw:
            raise DailyBarProductError("existing manifest differs from sealed bytes")
        return target
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target
