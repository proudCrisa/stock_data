"""Research-only historical index membership collector for Baostock."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import shutil
import tempfile
from typing import Any


SCHEMA_VERSION = "stockdata-research-index-universe/1"
INDEX_QUERIES = {
    "hs300": "query_hs300_stocks",
    "zz500": "query_zz500_stocks",
    "sz50": "query_sz50_stocks",
}


class ResearchUniverseError(ValueError):
    """Raised when historical index membership data is invalid."""


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
        raise ResearchUniverseError("universe value is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ResearchUniverseError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ResearchUniverseError(f"{field} must be an ISO date") from exc


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ResearchUniverseError("retrieved_at must be timezone-aware")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchUniverseError("retrieved_at must be timezone-aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchUniverseError("retrieved_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        required = {"requested_date", "effective_date", "index", "symbol", "name"}
        if set(row) != required:
            raise ResearchUniverseError(f"universe row {index} has an invalid schema")
        requested = _date(row["requested_date"], f"universe row {index}.requested_date")
        effective = _date(row["effective_date"], f"universe row {index}.effective_date")
        values = {
            "requested_date": requested,
            "effective_date": effective,
            "index": row["index"],
            "symbol": row["symbol"],
            "name": row["name"],
        }
        if not all(
            isinstance(values[key], str) and values[key]
            for key in ("index", "symbol", "name")
        ):
            raise ResearchUniverseError(f"universe row {index} has empty identity")
        normalized.append(values)
    if not normalized:
        raise ResearchUniverseError("universe artifact is empty")
    return sorted(normalized, key=_canonical)


def build_universe_artifact(
    rows: Iterable[Mapping[str, object]],
    *,
    retrieved_at: str,
    source_receipt: Mapping[str, object],
    output_root: str | Path,
) -> Path:
    """Write historical index observations while preserving research limits."""

    retrieved = _timestamp(retrieved_at)
    if set(source_receipt) < {"provider", "queries"}:
        raise ResearchUniverseError("source_receipt must identify provider and queries")
    normalized = _rows(rows)
    raw = b"".join(_canonical(row) + b"\n" for row in normalized)
    receipt = {**dict(source_receipt), "retrieved_at": retrieved}
    identity_payload = {
        "schema_version": SCHEMA_VERSION,
        "rows_sha256": _sha256(raw),
        "row_count": len(normalized),
        "source_receipt": receipt,
    }
    identity = _sha256(_canonical(identity_payload))
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / identity
    if target.exists():
        verify_universe_artifact(target)
        return target
    temporary = Path(tempfile.mkdtemp(prefix=".universe-", dir=root))
    try:
        (temporary / "universe.jsonl").write_bytes(raw)
        manifest = {
            **identity_payload,
            "artifact_id": identity,
            "coverage_start": min(str(row["requested_date"]) for row in normalized),
            "coverage_end": max(str(row["requested_date"]) for row in normalized),
            "scope": "index_membership_only",
            "complete_panel": False,
            "point_in_time_verified": False,
            "execution_grade": False,
            "authority_status": "research_vendor_only",
        }
        (temporary / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def verify_universe_artifact(root: str | Path) -> dict[str, Any]:
    """Verify a historical index artifact without modifying it."""

    path = Path(root).expanduser().resolve()
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="ascii"))
        raw = (path / "universe.jsonl").read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchUniverseError("universe artifact is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise ResearchUniverseError("unsupported universe artifact schema")
    for field, expected in (
        ("scope", "index_membership_only"),
        ("complete_panel", False),
        ("point_in_time_verified", False),
        ("execution_grade", False),
        ("authority_status", "research_vendor_only"),
    ):
        if manifest.get(field) != expected:
            raise ResearchUniverseError(f"universe {field} is invalid")
    rows = [json.loads(line) for line in raw.decode("ascii").splitlines()]
    normalized = _rows(rows)
    canonical_raw = b"".join(_canonical(row) + b"\n" for row in normalized)
    if canonical_raw != raw or manifest.get("rows_sha256") != _sha256(raw):
        raise ResearchUniverseError("universe rows hash or canonical form mismatch")
    if manifest.get("row_count") != len(rows):
        raise ResearchUniverseError("universe row count mismatch")
    identity_payload = {
        "schema_version": SCHEMA_VERSION,
        "rows_sha256": _sha256(raw),
        "row_count": len(rows),
        "source_receipt": manifest.get("source_receipt"),
    }
    if manifest.get("artifact_id") != _sha256(_canonical(identity_payload)):
        raise ResearchUniverseError("universe artifact identity mismatch")
    return manifest


def fetch_baostock_index_universe(
    requested_dates: Iterable[str],
    indexes: Iterable[str] = INDEX_QUERIES,
) -> list[dict[str, object]]:
    """Fetch date-addressed index membership from the free Baostock service."""

    index_names = tuple(indexes)
    unknown = sorted(set(index_names) - set(INDEX_QUERIES))
    if unknown:
        raise ResearchUniverseError(f"unsupported indexes: {', '.join(unknown)}")
    dates = [_date(value, "requested_date") for value in requested_dates]
    if not dates:
        raise ResearchUniverseError("requested_dates is empty")
    try:
        import baostock as bs
    except ImportError as exc:
        raise ResearchUniverseError("baostock is required for universe fetch") from exc
    login = bs.login()
    if login.error_code != "0":
        raise ResearchUniverseError(f"Baostock login failed: {login.error_msg}")
    rows: list[dict[str, object]] = []
    try:
        for requested in dates:
            for index in index_names:
                result = getattr(bs, INDEX_QUERIES[index])(date=requested)
                if result.error_code != "0":
                    raise ResearchUniverseError(
                        f"Baostock {index} query failed: {result.error_msg}"
                    )
                while result.next():
                    effective, symbol, name = result.get_row_data()
                    rows.append(
                        {
                            "requested_date": requested,
                            "effective_date": effective,
                            "index": index,
                            "symbol": symbol,
                            "name": name,
                        }
                    )
    finally:
        bs.logout()
    return rows


__all__ = [
    "INDEX_QUERIES",
    "ResearchUniverseError",
    "SCHEMA_VERSION",
    "build_universe_artifact",
    "fetch_baostock_index_universe",
    "verify_universe_artifact",
]
