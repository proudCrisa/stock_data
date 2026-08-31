"""Forward-only raw price evidence capture for a fixed research cohort."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .cache import Cache
from .execution_readiness import check_execution_readiness
from .fetch_tencent import fetch_tencent_daily
from .finalization import latest_finalized_date
from .sync import sync_symbols
from .ticker import normalize


SCHEMA_VERSION = "stockdata-forward-evidence-capture/1"
_SOURCE_VERSIONS = {
    "baostock": "baostock-adjustflag-3",
    "tencent": "tencent-qt-daily-v1",
}


def _bind_cohort(cache: Cache, spec: dict[str, object]) -> str:
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    with cache._conn:
        cache._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS forward_capture_cohort (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                spec_json TEXT NOT NULL,
                spec_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS forward_capture_cohort_no_update
            BEFORE UPDATE ON forward_capture_cohort BEGIN
                SELECT RAISE(ABORT, 'forward capture cohort is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS forward_capture_cohort_no_delete
            BEFORE DELETE ON forward_capture_cohort BEGIN
                SELECT RAISE(ABORT, 'forward capture cohort is immutable');
            END;
            """
        )
        existing = cache._conn.execute(
            "SELECT spec_sha256 FROM forward_capture_cohort WHERE singleton=1"
        ).fetchone()
        if existing is None:
            cache._conn.execute(
                "INSERT INTO forward_capture_cohort VALUES (1,?,?,?)",
                (encoded, digest, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        elif str(existing[0]) != digest:
            raise ValueError("forward evidence cohort identity drift")
    return digest


def capture_forward_evidence(
    cache: Cache,
    codes: Iterable[str],
    start: str,
    end: str | None = None,
    *,
    source: str = "baostock",
    adjustment_mode: str = "raw",
    adjustment_version: str | None = None,
    fetcher: Callable[[str, str, str], list[dict]] | None = None,
) -> dict[str, object]:
    """Capture and verify one fixed raw cohort without mixing legacy data."""
    if adjustment_mode != "raw":
        raise ValueError("forward execution evidence requires raw prices")
    try:
        adjustment_version = adjustment_version or _SOURCE_VERSIONS[source]
    except KeyError as exc:
        raise ValueError(f"unsupported forward evidence source: {source}") from exc
    symbols = tuple(sorted({normalize(code) for code in codes}))
    if not symbols:
        raise ValueError("at least one code is required")
    calendar = cache.trading_calendar
    end = end or (
        latest_finalized_date(calendar=calendar)
        if calendar.has_data()
        else latest_finalized_date()
    )
    cache._require_collector_writer(
        step_id="post_close_prices", session=end
    )

    identities = {
        tuple(row)
        for row in cache._conn.execute(
            "SELECT DISTINCT source,adjustment_mode,adjustment_version FROM daily"
        )
    }
    expected_identity = (source, adjustment_mode, adjustment_version)
    if identities - {expected_identity}:
        raise ValueError("forward evidence database contains another price identity")
    existing_symbols = {
        normalize(str(row[0]))
        for row in cache._conn.execute("SELECT DISTINCT code FROM daily")
    }
    if existing_symbols - set(symbols):
        raise ValueError("forward evidence database contains symbols outside the cohort")
    earliest = cache._conn.execute("SELECT MIN(date) FROM daily").fetchone()[0]
    if earliest is not None and str(earliest) < start:
        raise ValueError("forward evidence database predates the cohort start")

    cohort: dict[str, object] = {
        "symbols": list(symbols),
        "start": start,
        "source": source,
        "adjustment_mode": adjustment_mode,
        "adjustment_version": adjustment_version,
    }
    cohort_sha256 = _bind_cohort(cache, cohort)

    sync_result = sync_symbols(
        cache,
        symbols,
        start,
        end,
        source=source,
        adjustment_mode=adjustment_mode,
        adjustment_version=adjustment_version,
        fetcher=fetcher or (fetch_tencent_daily if source == "tencent" else None),
    )
    dates = tuple(
        str(row[0])
        for row in cache._conn.execute(
            """
            SELECT DISTINCT date FROM daily
            WHERE source=? AND adjustment_mode=? AND adjustment_version=?
              AND date>=? AND date<=? AND is_final=1
            ORDER BY date
            """,
            (source, adjustment_mode, adjustment_version, start, end),
        )
    )
    if dates:
        panel = {(symbol, day) for symbol in symbols for day in dates}
        readiness = check_execution_readiness(
            cache.path,
            source=source,
            adjustment_mode=adjustment_mode,
            adjustment_version=adjustment_version,
            panel=panel,
        )
    else:
        panel = set()
        readiness = {
            "ready": False,
            "blockers": [
                {"code": "no_captured_dates", "count": 1, "examples": [f"{start}..{end}"]}
            ],
        }
    ready = sync_result["errors"] == 0 and readiness.get("ready") is True
    return {
        "schema_version": SCHEMA_VERSION,
        "ready": ready,
        "database": str(Path(cache.path)),
        "cohort_sha256": cohort_sha256,
        "cohort": {
            **cohort,
            "end": end,
        },
        "captured_dates": list(dates),
        "panel_size": len(panel),
        "sync": sync_result,
        "readiness": readiness,
    }
