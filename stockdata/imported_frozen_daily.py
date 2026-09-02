"""Shadow-only daily manifest imported from one frozen trading preimage."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path

from .qmt_transport_capture import _read_regular_file, _write_content_addressed

MANIFEST_SCHEMA = "stockdata-imported-frozen-daily-manifest/1"
PRODUCT_SCHEMA = "stockdata-imported-frozen-daily-product/1"
RECEIPT_SCHEMA = "stockdata-imported-frozen-observation-receipt/1"
PROFILE = "imported_frozen_observation"
SOURCE_ARTIFACT_KIND = "trading_daily_formal_preimage"
SOURCE_FILE_SHA256 = "263b811f817cec371fc8cd71037ad6c6bfebc22bf2bdb5691c901723616a88e9"
INSTRUMENT = "sz159655"
WINDOW_START = "2025-07-03"
WINDOW_END = "2026-08-27"
ROW_COUNT = 282
PERMITTED_USES = ["offline_replay", "shadow_compare"]
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]
_PREIMAGE_KEYS = {
    "columns",
    "instrument_code",
    "provenance",
    "reference_binding_status",
    "rows",
    "schema_version",
}
_PROVENANCE = {
    "adjustment": "qfq",
    "contract_version": "txkq_v4",
    "finality": "intraday_bar_rejected",
    "provider_id": "tencent",
    "schema_version": "market-data-provenance-v1",
    "source_id": "tencent.ifzq",
    "volume_unit": "hand",
}
class ImportedFrozenDailyError(ValueError):
    """The frozen observation cannot be represented without overstating authority."""


def _duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ImportedFrozenDailyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ImportedFrozenDailyError(f"non-finite JSON number: {value}")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ImportedFrozenDailyError("value is not canonical JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _parse_canonical(raw: bytes, label: str) -> object:
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportedFrozenDailyError(f"{label} is not strict JSON") from exc
    if raw != _canonical(value):
        raise ImportedFrozenDailyError(f"{label} bytes are not canonical JSON")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ImportedFrozenDailyError("imported_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImportedFrozenDailyError("imported_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ImportedFrozenDailyError("imported_at must include timezone")
    return value


def _number(value: object, field: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ImportedFrozenDailyError(f"{field} must be finite numeric data")
    return value


def _validate_preimage(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _PREIMAGE_KEYS:
        raise ImportedFrozenDailyError("formal preimage schema is incomplete")
    if (
        value.get("schema_version") != "daily-formal-preimage/1"
        or value.get("instrument_code") != INSTRUMENT
        or value.get("columns") != _COLUMNS
        or value.get("reference_binding_status") != "artifact_key"
        or value.get("provenance") != _PROVENANCE
    ):
        raise ImportedFrozenDailyError(
            "formal preimage identity does not match Stage 2C"
        )
    rows = value.get("rows")
    if not isinstance(rows, list) or len(rows) != ROW_COUNT:
        raise ImportedFrozenDailyError(
            "formal preimage row count does not match Stage 2C"
        )
    previous = ""
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != set(_COLUMNS):
            raise ImportedFrozenDailyError(f"row {index} schema is invalid")
        try:
            day = date.fromisoformat(str(row.get("date"))).isoformat()
        except ValueError as exc:
            raise ImportedFrozenDailyError(f"row {index} date is invalid") from exc
        if day != row.get("date") or day <= previous:
            raise ImportedFrozenDailyError(
                "formal preimage dates are not strictly ordered"
            )
        previous = day
        open_ = _number(row.get("open"), f"row {index} open")
        high = _number(row.get("high"), f"row {index} high")
        low = _number(row.get("low"), f"row {index} low")
        close = _number(row.get("close"), f"row {index} close")
        volume = _number(row.get("volume"), f"row {index} volume")
        if (
            min(float(open_), float(high), float(low), float(close)) <= 0
            or float(volume) < 0
            or float(high) < max(float(open_), float(close))
            or float(low) > min(float(open_), float(close))
            or float(high) < float(low)
        ):
            raise ImportedFrozenDailyError(f"row {index} OHLCV is invalid")
        if row.get("amount") is not None:
            raise ImportedFrozenDailyError(
                "Stage 2C amount must remain explicitly unavailable"
            )
    if rows[0]["date"] != WINDOW_START or rows[-1]["date"] != WINDOW_END:
        raise ImportedFrozenDailyError("formal preimage window does not match Stage 2C")
    return value


def _receipt(preimage: dict[str, object], imported_at: str) -> dict[str, object]:
    source_raw = _canonical(preimage)
    unsigned = {
        "schema_version": RECEIPT_SCHEMA,
        "receipt_kind": PROFILE,
        "source_artifact_kind": SOURCE_ARTIFACT_KIND,
        "source_file_sha256": hashlib.sha256(source_raw).hexdigest(),
        "source_file_byte_count": len(source_raw),
        "instrument": INSTRUMENT,
        "window": {
            "start": WINDOW_START,
            "end": WINDOW_END,
            "row_count": ROW_COUNT,
        },
        "observed_at": None,
        "imported_at": imported_at,
        "source_authentication": "unverified",
        "reference_binding_status": "imported_hash_bound",
        "response_sha256": hashlib.sha256(source_raw).hexdigest(),
        "response": copy.deepcopy(preimage),
    }
    return {**unsigned, "receipt_id": _sha256(unsigned)}


def _manifest(preimage: dict[str, object], imported_at: str) -> dict[str, object]:
    preimage = _validate_preimage(preimage)
    imported_at = _timestamp(imported_at)
    if hashlib.sha256(_canonical(preimage)).hexdigest() != SOURCE_FILE_SHA256:
        raise ImportedFrozenDailyError(
            "formal preimage is not the frozen Stage 2C input"
        )
    receipt = _receipt(preimage, imported_at)
    rows = copy.deepcopy(preimage["rows"])
    content_hash = _sha256({"columns": _COLUMNS, "rows": rows})
    product_identity = {
        "schema_version": PRODUCT_SCHEMA,
        "data_product_id": (
            f"imported-frozen-daily:{INSTRUMENT}:{receipt['source_file_sha256']}"
        ),
        "version": content_hash,
        "schema_id": "ohlcv-daily/1",
        "profile": PROFILE,
        "independence_status": "self_comparison_only",
        "cutover_ready": False,
        "authority_grade": "shadow",
        "decision_eligible": False,
        "decision_authority": False,
        "actions": [],
        "source_id": PROFILE,
        "source_authentication": "unverified",
        "reference_binding_status": "imported_hash_bound",
        "permitted_uses": list(PERMITTED_USES),
        "instrument_scope": {
            "codes": [INSTRUMENT],
            "start": WINDOW_START,
            "end": WINDOW_END,
        },
        "event_time_range": {"start": WINDOW_START, "end": WINDOW_END},
        "frozen_window": {
            "start": WINDOW_START,
            "end": WINDOW_END,
            "row_count": ROW_COUNT,
        },
        "finality": {
            "status": PROFILE,
            "watermark": WINDOW_END,
        },
        "pit_mode": PROFILE,
        "available_at": imported_at,
        "quality_status": "imported_frozen_observation_unverified",
        "corporate_action_version": "not_bound",
        "universe_version": "single_instrument_hash_bound",
        "trading_calendar_version": "not_bound",
        "content_hash": content_hash,
        "input_receipt_ids": [receipt["receipt_id"]],
        "claimed_provenance": copy.deepcopy(preimage["provenance"]),
        "authenticated_provenance": None,
        "columns": list(_COLUMNS),
        "rows": rows,
    }
    product = {**product_identity, "product_sha256": _sha256(product_identity)}
    identity = {
        "schema_version": MANIFEST_SCHEMA,
        "profile": PROFILE,
        "independence_status": "self_comparison_only",
        "cutover_ready": False,
        "authority_grade": "shadow",
        "decision_eligible": False,
        "decision_authority": False,
        "actions": [],
        "source_authentication": "unverified",
        "reference_binding_status": "imported_hash_bound",
        "permitted_uses": list(PERMITTED_USES),
        "created_at": imported_at,
        "decision_cutoff": imported_at,
        "available_at": imported_at,
        "dataset_ids": [product["data_product_id"]],
        "content_hashes": [product["product_sha256"]],
        "input_receipt_ids": [receipt["receipt_id"]],
        "input_receipts": [receipt],
        "products": [product],
    }
    digest = _sha256(identity)
    return {**identity, "manifest_id": f"shadow-{digest}", "manifest_sha256": digest}


def build_imported_frozen_daily_manifest(
    preimage_path: str | Path,
    *,
    expected_sha256: str,
    imported_at: str,
) -> dict[str, object]:
    """Hash, parse, and import the fixed sz159655 preimage without source authority."""
    raw = _read_regular_file(
        preimage_path,
        MAX_ARTIFACT_BYTES,
        ImportedFrozenDailyError,
    )
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 != SOURCE_FILE_SHA256 or actual_sha256 != expected_sha256:
        raise ImportedFrozenDailyError("formal preimage byte hash mismatch")
    preimage = _parse_canonical(raw, "formal preimage")
    return _manifest(_validate_preimage(preimage), imported_at)


def verify_imported_frozen_daily_manifest(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or len(_canonical(payload)) > MAX_ARTIFACT_BYTES:
        raise ImportedFrozenDailyError("imported frozen manifest is invalid")
    receipts = payload.get("input_receipts")
    if (
        not isinstance(receipts, list)
        or len(receipts) != 1
        or not isinstance(receipts[0], dict)
    ):
        raise ImportedFrozenDailyError("imported frozen manifest receipt is missing")
    response = receipts[0].get("response")
    imported_at = receipts[0].get("imported_at")
    expected = _manifest(_validate_preimage(response), _timestamp(imported_at))
    if payload != expected:
        raise ImportedFrozenDailyError(
            "imported frozen manifest does not replay its receipt"
        )
    return payload


def load_imported_frozen_daily_manifest(path: str | Path) -> dict[str, object]:
    raw = _read_regular_file(path, MAX_ARTIFACT_BYTES, ImportedFrozenDailyError)
    return verify_imported_frozen_daily_manifest(
        _parse_canonical(raw, "imported frozen manifest")
    )


def write_imported_frozen_daily_manifest(
    output_root: str | Path,
    manifest: dict[str, object],
) -> Path:
    verified = verify_imported_frozen_daily_manifest(manifest)
    return _write_content_addressed(
        output_root,
        str(verified["manifest_sha256"]),
        _canonical(verified),
        ImportedFrozenDailyError,
    )


__all__ = [
    "ImportedFrozenDailyError",
    "build_imported_frozen_daily_manifest",
    "load_imported_frozen_daily_manifest",
    "verify_imported_frozen_daily_manifest",
    "write_imported_frozen_daily_manifest",
]
