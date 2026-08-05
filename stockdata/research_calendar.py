"""Research-only historical trading calendar collector.

The artifact is useful for point-in-time research, but it is deliberately not
an execution authority.  A provider-signed exchange calendar is still required
by the execution readiness gate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


SCHEMA_VERSION = "stockdata-research-trading-calendar/1"


class ResearchCalendarError(ValueError):
    """Raised when a calendar cannot be trusted as a complete research artifact."""


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
        raise ResearchCalendarError("calendar value is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _iso_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ResearchCalendarError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ResearchCalendarError(f"{field} must be an ISO date") from exc


def _iso_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ResearchCalendarError(f"{field} must be timezone-aware")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchCalendarError(f"{field} must be timezone-aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchCalendarError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _expected_dates(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first > last:
        raise ResearchCalendarError("coverage_start must not be after coverage_end")
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def _normalize_rows(
    rows: Iterable[Mapping[str, object]], coverage_start: str, coverage_end: str
) -> list[dict[str, object]]:
    expected = _expected_dates(coverage_start, coverage_end)
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if set(row) != {"date", "is_trading_day"}:
            raise ResearchCalendarError(f"calendar row {index} has an invalid schema")
        day = _iso_date(row["date"], f"calendar row {index}.date")
        flag = row["is_trading_day"]
        if type(flag) is not bool:
            raise ResearchCalendarError(
                f"calendar row {index}.is_trading_day must be boolean"
            )
        normalized.append({"date": day, "is_trading_day": flag})
    if [row["date"] for row in normalized] != expected:
        raise ResearchCalendarError("calendar must cover every date in order")
    return normalized


def build_calendar_artifact(
    rows: Iterable[Mapping[str, object]],
    *,
    coverage_start: str,
    coverage_end: str,
    retrieved_at: str,
    source_receipt: Mapping[str, object],
    output_root: str | Path,
) -> Path:
    """Write a content-addressed, research-only calendar artifact atomically."""

    start = _iso_date(coverage_start, "coverage_start")
    end = _iso_date(coverage_end, "coverage_end")
    retrieved = _iso_timestamp(retrieved_at, "retrieved_at")
    if set(source_receipt) < {"provider", "query"}:
        raise ResearchCalendarError("source_receipt must identify provider and query")
    normalized = _normalize_rows(rows, start, end)
    raw = b"".join(_canonical(row) + b"\n" for row in normalized)
    receipt = {**dict(source_receipt), "retrieved_at": retrieved}
    manifest_identity = {
        "schema_version": SCHEMA_VERSION,
        "coverage_start": start,
        "coverage_end": end,
        "rows_sha256": _sha256(raw),
        "row_count": len(normalized),
        "source_receipt": receipt,
    }
    identity = _sha256(_canonical(manifest_identity))
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{end}-{identity}"
    if target.exists():
        verify_calendar_artifact(target)
        return target

    temporary = Path(tempfile.mkdtemp(prefix=".calendar-", dir=root))
    try:
        (temporary / "calendar.jsonl").write_bytes(raw)
        manifest = {
            **manifest_identity,
            "artifact_id": identity,
            "execution_grade": False,
            "authority_status": "research_vendor_only",
        }
        (temporary / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def verify_calendar_artifact(root: str | Path) -> dict[str, Any]:
    """Verify a calendar artifact and return its manifest without modifying it."""

    path = Path(root).expanduser().resolve()
    if not path.is_dir() or path.is_symlink():
        raise ResearchCalendarError("calendar artifact root must be a directory")
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="ascii"))
        raw = (path / "calendar.jsonl").read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchCalendarError("calendar artifact is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise ResearchCalendarError("unsupported calendar artifact schema")
    if manifest.get("execution_grade") is not False:
        raise ResearchCalendarError("research calendar cannot claim execution grade")
    if manifest.get("authority_status") != "research_vendor_only":
        raise ResearchCalendarError("calendar authority status is invalid")
    start = _iso_date(manifest.get("coverage_start"), "coverage_start")
    end = _iso_date(manifest.get("coverage_end"), "coverage_end")
    lines = raw.decode("ascii").splitlines()
    rows = [json.loads(line) for line in lines]
    normalized = _normalize_rows(rows, start, end)
    canonical_raw = b"".join(_canonical(row) + b"\n" for row in normalized)
    if canonical_raw != raw:
        raise ResearchCalendarError("calendar rows are not canonical")
    if manifest.get("rows_sha256") != _sha256(raw):
        raise ResearchCalendarError("calendar rows hash mismatch")
    if manifest.get("row_count") != len(rows):
        raise ResearchCalendarError("calendar row count mismatch")
    identity_payload = {
        "schema_version": SCHEMA_VERSION,
        "coverage_start": start,
        "coverage_end": end,
        "rows_sha256": _sha256(raw),
        "row_count": len(rows),
        "source_receipt": manifest.get("source_receipt"),
    }
    if manifest.get("artifact_id") != _sha256(_canonical(identity_payload)):
        raise ResearchCalendarError("calendar artifact identity mismatch")
    return manifest


def fetch_baostock_trade_calendar(
    start_date: str, end_date: str
) -> list[dict[str, object]]:
    """Fetch a complete calendar from the free Baostock service."""

    try:
        import baostock as bs
    except ImportError as exc:
        raise ResearchCalendarError("baostock is required for calendar fetch") from exc
    login = bs.login()
    if login.error_code != "0":
        raise ResearchCalendarError(f"Baostock login failed: {login.error_msg}")
    try:
        result = bs.query_trade_dates(start_date=start_date, end_date=end_date)
        if result.error_code != "0":
            raise ResearchCalendarError(
                f"Baostock calendar query failed: {result.error_msg}"
            )
        rows: list[dict[str, object]] = []
        while result.next():
            day, flag = result.get_row_data()
            rows.append({"date": day, "is_trading_day": flag == "1"})
        return rows
    finally:
        bs.logout()


__all__ = [
    "SCHEMA_VERSION",
    "ResearchCalendarError",
    "build_calendar_artifact",
    "fetch_baostock_trade_calendar",
    "verify_calendar_artifact",
]
