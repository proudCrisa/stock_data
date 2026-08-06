"""Research-only Tencent historical daily bars.

The endpoint is useful for signal research, including ETFs, but it is not an
execution-grade price authority. Raw prices are kept separate from adjusted
prices and every response is preserved in an append-only capture receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .ticker import to_tencent

TENCENT_HISTORY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
TENCENT_HISTORY_SOURCE = "tencent-fqkline-history-v1"
TENCENT_HISTORY_SCHEMA = "stockdata-research-tencent-history/1"
_ADJUSTMENT_KEYS = {"raw": "day", "qfq": "qfqday", "hfq": "hfqday"}
_USER_AGENT = "Mozilla/5.0"


class TencentHistoryError(ValueError):
    """Raised when Tencent history cannot be trusted as a research artifact."""


class CapturedTencentHistory(list[dict[str, object]]):
    """Parsed bars plus the exact HTTP capture receipt."""

    def __init__(self, bars: list[dict[str, object]], capture_receipt: dict[str, object]):
        super().__init__(bars)
        self.capture_receipt = capture_receipt


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise TencentHistoryError("value is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _receipt_summary(receipt: Mapping[str, object]) -> dict[str, object]:
    pages = receipt.get("response", {}).get("pages", [])
    summaries = []
    for page in pages:
        if not isinstance(page, Mapping):
            raise TencentHistoryError("Tencent receipt page is invalid")
        response = page.get("response")
        if not isinstance(response, Mapping):
            raise TencentHistoryError("Tencent receipt page response is invalid")
        summaries.append(
            {
                "observed_at": page.get("observed_at"),
                "request": page.get("request"),
                "response": {
                    key: response.get(key)
                    for key in ("adjustment_key", "response_sha256", "row_count")
                },
            }
        )
    return {
        "source": receipt.get("source"),
        "observed_at": receipt.get("observed_at"),
        "request": receipt.get("request"),
        "response": {
            key: receipt.get("response", {}).get(key)
            for key in ("bar_count", "coverage_start", "coverage_end")
        }
        | {"pages": summaries},
    }


def _iso_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise TencentHistoryError(f"{field} must be an ISO date") from exc


def _request_page(
    symbol: str,
    year: int,
    adjustment_mode: str,
    timeout: float,
    http_get: Callable[[str, Mapping[str, str], float], tuple[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, object]]:
    adjustment_key = _ADJUSTMENT_KEYS[adjustment_mode]
    params = {
        "_var": f"kline_day{'' if adjustment_mode == 'raw' else adjustment_mode}{symbol}{year}",
        "param": f"{symbol},day,{year}-01-01,{year + 1}-12-31,640,{'' if adjustment_mode == 'raw' else adjustment_mode}",
        "r": "0.8205512681390605",
    }
    if http_get is None:
        def http_get(url: str, query: Mapping[str, str], request_timeout: float) -> tuple[str, str]:
            request = Request(f"{url}?{urlencode(query)}", headers={"User-Agent": _USER_AGENT})
            with urlopen(request, timeout=request_timeout) as response:
                return response.read().decode("utf-8"), f"{response.geturl()}"

    raw, requested_url = http_get(TENCENT_HISTORY_URL, params, timeout)
    marker = raw.find("=")
    if marker < 0:
        raise TencentHistoryError("Tencent history response is not JSONP")
    try:
        payload = json.loads(raw[marker + 1 :])
    except json.JSONDecodeError as exc:
        raise TencentHistoryError("Tencent history response is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise TencentHistoryError("Tencent history response returned an error")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get(symbol), dict):
        raise TencentHistoryError("Tencent history response has no symbol data")
    rows = data[symbol].get(adjustment_key)
    if not isinstance(rows, list):
        raise TencentHistoryError(f"Tencent response has no {adjustment_key} rows")
    receipt = {
        "source": TENCENT_HISTORY_SOURCE,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request": {"method": "GET", "url": requested_url, "params": dict(params)},
        "response": {
            "raw": raw,
            "response_sha256": _sha256(raw.encode("utf-8")),
            "row_count": len(rows),
            "adjustment_key": adjustment_key,
        },
    }
    return payload, receipt


def _parse_rows(payload: Mapping[str, Any], symbol: str, adjustment_mode: str) -> list[dict[str, object]]:
    key = _ADJUSTMENT_KEYS[adjustment_mode]
    rows = payload["data"][symbol][key]
    parsed: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise TencentHistoryError("Tencent history row is incomplete")
        try:
            day = date.fromisoformat(str(row[0])).isoformat()
            values = [float(row[index]) for index in (1, 2, 3, 4, 5)]
        except (TypeError, ValueError) as exc:
            raise TencentHistoryError("Tencent history row contains invalid values") from exc
        if not all(math.isfinite(value) for value in values) or any(value <= 0 for value in values[:4]):
            raise TencentHistoryError(f"Tencent history row {day} contains invalid prices")
        parsed.append(
            {
                "date": day,
                "open": values[0],
                "close": values[1],
                "high": values[2],
                "low": values[3],
                # Tencent returns daily volume in lots; stock_data stores shares.
                "volume": values[4] * 100.0,
            }
        )
    return parsed


def fetch_tencent_history(
    code: str,
    start: str,
    end: str,
    *,
    adjustment_mode: str = "raw",
    timeout: float = 20.0,
    http_get: Callable[[str, Mapping[str, str], float], tuple[str, str]] | None = None,
) -> CapturedTencentHistory:
    """Fetch and date-filter Tencent history, preserving page receipts."""
    start_date = _iso_date(start, "start")
    end_date = _iso_date(end, "end")
    if start_date > end_date:
        raise TencentHistoryError("start must not be after end")
    if adjustment_mode not in _ADJUSTMENT_KEYS:
        raise TencentHistoryError("adjustment_mode must be raw, qfq, or hfq")
    symbol = to_tencent(code).replace(".", "")
    pages: list[dict[str, object]] = []
    bars_by_date: dict[str, dict[str, object]] = {}
    for year in range(date.fromisoformat(start_date).year, date.fromisoformat(end_date).year + 1):
        payload, receipt = _request_page(symbol, year, adjustment_mode, timeout, http_get)
        pages.append(receipt)
        for bar in _parse_rows(payload, symbol, adjustment_mode):
            if start_date <= str(bar["date"]) <= end_date:
                bars_by_date[str(bar["date"])] = bar
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bars = []
    for day in sorted(bars_by_date):
        bar = dict(bars_by_date[day])
        bar.update(
            {
                "source": TENCENT_HISTORY_SOURCE,
                "adjustment_mode": adjustment_mode,
                "adjustment_version": f"{TENCENT_HISTORY_SOURCE}-{adjustment_mode}",
                "retrieved_at": retrieved_at,
                "is_final": True,
            }
        )
        bars.append(bar)
    receipt = {
        "source": TENCENT_HISTORY_SOURCE,
        "observed_at": retrieved_at,
        "request": {"code": code, "symbol": symbol, "start": start_date, "end": end_date, "adjustment_mode": adjustment_mode},
        "response": {
            "pages": pages,
            "bar_count": len(bars),
            "coverage_start": bars[0]["date"] if bars else None,
            "coverage_end": bars[-1]["date"] if bars else None,
        },
    }
    for bar in bars:
        bar["_capture_receipt"] = receipt
    return CapturedTencentHistory(bars, receipt)


def write_tencent_history_artifact(
    output_root: str | Path,
    *,
    code: str,
    start: str,
    end: str,
    adjustment_mode: str,
    captured: CapturedTencentHistory,
) -> Path:
    """Write a content-addressed, research-only Tencent history artifact."""
    rows = [dict({key: value for key, value in bar.items() if not key.startswith("_")}) for bar in captured]
    raw = b"".join(_canonical(row) + b"\n" for row in rows)
    manifest_identity = {
        "schema_version": TENCENT_HISTORY_SCHEMA,
        "code": code,
        "start": _iso_date(start, "start"),
        "end": _iso_date(end, "end"),
        "adjustment_mode": adjustment_mode,
        "rows_sha256": _sha256(raw),
        "response_sha256": _sha256(_canonical(captured.capture_receipt["response"])),
        "receipt_sha256": _sha256(_canonical(captured.capture_receipt)),
        "row_count": len(rows),
    }
    artifact_id = _sha256(_canonical(manifest_identity))
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{code.replace('.', '_')}-{adjustment_mode}-{artifact_id}"
    if target.exists():
        verify_tencent_history_artifact(target)
        return target
    temporary = Path(tempfile.mkdtemp(prefix=".tencent-history-", dir=root))
    try:
        (temporary / "bars.jsonl").write_bytes(raw)
        (temporary / "receipt.json").write_bytes(_canonical(captured.capture_receipt) + b"\n")
        manifest = {
            **manifest_identity,
            "artifact_id": artifact_id,
            "source_receipt": _receipt_summary(captured.capture_receipt),
            "research_only": True,
            "execution_grade": False,
            "authority_status": "research_vendor_only",
        }
        (temporary / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def verify_tencent_history_artifact(root: str | Path) -> dict[str, Any]:
    """Verify one Tencent history artifact without modifying it."""
    path = Path(root).expanduser().resolve()
    if not path.is_dir() or path.is_symlink():
        raise TencentHistoryError("Tencent artifact root must be a directory")
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="ascii"))
        raw = (path / "bars.jsonl").read_bytes()
        receipt = json.loads((path / "receipt.json").read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TencentHistoryError("Tencent artifact is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != TENCENT_HISTORY_SCHEMA:
        raise TencentHistoryError("unsupported Tencent artifact schema")
    if manifest.get("research_only") is not True or manifest.get("execution_grade") is not False:
        raise TencentHistoryError("Tencent artifact crosses execution boundary")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("response"), dict):
        raise TencentHistoryError("Tencent artifact receipt is missing")
    if _sha256(_canonical(receipt)) != manifest.get("receipt_sha256"):
        raise TencentHistoryError("Tencent artifact receipt does not match manifest")
    if _sha256(_canonical(receipt["response"])) != manifest.get("response_sha256"):
        raise TencentHistoryError("Tencent artifact receipt does not match manifest")
    if manifest.get("source_receipt") != _receipt_summary(receipt):
        raise TencentHistoryError("Tencent artifact receipt summary does not match manifest")
    try:
        rows = [json.loads(line) for line in raw.decode("ascii").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TencentHistoryError("Tencent artifact rows are invalid") from exc
    if _sha256(raw) != manifest.get("rows_sha256") or len(rows) != manifest.get("row_count"):
        raise TencentHistoryError("Tencent artifact rows do not match manifest")
    identity = {key: manifest.get(key) for key in ("schema_version", "code", "start", "end", "adjustment_mode", "rows_sha256", "response_sha256", "receipt_sha256", "row_count")}
    if _sha256(_canonical(identity)) != manifest.get("artifact_id"):
        raise TencentHistoryError("Tencent artifact identity mismatch")
    return manifest


def reconcile_tencent_baostock(
    tencent_rows: list[Mapping[str, object]],
    baostock_rows: list[Mapping[str, object]],
    *,
    price_tolerance: float = 1e-8,
    volume_tolerance: float = 100.0,
) -> dict[str, object]:
    """Compare same-identity daily rows without silently filling discrepancies."""
    fields = ("open", "high", "low", "close", "volume")
    left = {str(row["date"]): row for row in tencent_rows}
    right = {str(row["date"]): row for row in baostock_rows}
    common = sorted(set(left) & set(right))
    mismatches: list[dict[str, object]] = []
    for day in common:
        differences = {}
        for field in fields:
            tolerance = volume_tolerance if field == "volume" else price_tolerance
            delta = abs(float(left[day][field]) - float(right[day][field]))
            if delta > tolerance:
                differences[field] = {"tencent": left[day][field], "baostock": right[day][field], "absolute_delta": delta}
        if differences:
            mismatches.append({"date": day, "fields": differences})
    return {
        "same_identity_required": True,
        "price_tolerance": price_tolerance,
        "volume_tolerance": volume_tolerance,
        "tencent_dates": len(left),
        "baostock_dates": len(right),
        "matched_dates": len(common),
        "tencent_only_dates": sorted(set(left) - set(right)),
        "baostock_only_dates": sorted(set(right) - set(left)),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "exact_match": not mismatches and set(left) == set(right),
    }


__all__ = [
    "CapturedTencentHistory",
    "TENCENT_HISTORY_SCHEMA",
    "TENCENT_HISTORY_SOURCE",
    "TencentHistoryError",
    "fetch_tencent_history",
    "reconcile_tencent_baostock",
    "verify_tencent_history_artifact",
    "write_tencent_history_artifact",
]
