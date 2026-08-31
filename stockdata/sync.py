"""Bounded, resumable daily history synchronization."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Callable, Iterable

from .cache import Cache
from .fetch_baostock import fetch_baostock
from .fetch_tencent import parse_tencent_bar
from .finalization import latest_finalized_date
from .ticker import normalize, to_tencent

default_final_date = latest_finalized_date

_VERSIONS = {
    "qfq": "baostock-adjustflag-2",
    "raw": "baostock-adjustflag-3",
    "hfq": "baostock-adjustflag-1",
}


def _default_fetcher(adjustment_mode: str) -> Callable[[str, str, str], list[dict]]:
    return lambda code, start, end: fetch_baostock(
        code, start, end, adjustment_mode=adjustment_mode
    )


def _missing_ranges(
    covered: tuple[str, str] | None, start: str, end: str
) -> list[tuple[str, str]]:
    if covered is None:
        return [(start, end)]
    lo, hi = covered
    gaps = []
    if start < lo:
        gaps.append((start, (date.fromisoformat(lo) - timedelta(days=1)).isoformat()))
    if end > hi:
        gaps.append(((date.fromisoformat(hi) + timedelta(days=1)).isoformat(), end))
    return gaps


def _is_collector_tencent_price_sync(
    cache: Cache,
    source: str,
    adjustment_mode: str,
    adjustment_version: str,
) -> bool:
    """Return whether the immutable collector price writer rules apply."""
    if (
        source != "tencent"
        or adjustment_mode != "raw"
        or not adjustment_version.startswith("tencent-qt-daily-v")
    ):
        return False
    row = cache._conn.execute(
        "SELECT 1 FROM main.sqlite_master "
        "WHERE type='table' AND name='forward_collector_genesis'"
    ).fetchone()
    return row is not None


def _decode_canonical_json(value: object) -> tuple[object, str]:
    if not isinstance(value, str):
        raise ValueError("collector receipt JSON is invalid")
    try:
        decoded = json.loads(value)
        canonical = json.dumps(
            decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("collector receipt JSON is invalid") from exc
    if canonical != value:
        raise ValueError("collector receipt JSON is not canonical")
    return decoded, hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_exact_tencent_envelope(raw: object, symbol: str) -> bool:
    """Require the complete one-line Tencent assignment, with no trailing bytes."""

    prefix = f'v_{symbol}="'
    return (
        isinstance(raw, str)
        and raw.startswith(prefix)
        and raw.endswith('";')
        and raw.count('"') == 2
    )


def _validate_collector_tencent_capture(
    receipt: object,
    code: str,
    start: str,
    end: str,
) -> None:
    """Prove a collector capture receipt before its transaction is admitted."""

    if not isinstance(receipt, dict) or set(receipt) != {
        "observed_at", "source", "request", "response"
    }:
        raise ValueError("collector Tencent capture receipt is invalid")
    symbol = to_tencent(code)
    if receipt["source"] != "tencent" or not isinstance(receipt["observed_at"], str):
        raise ValueError("collector Tencent capture receipt is invalid")
    request = receipt["request"]
    if (
        not isinstance(request, dict)
        or set(request) != {"method", "url", "start_date", "end_date"}
        or request.get("method") != "qt"
        or request.get("url") != f"https://qt.gtimg.cn/q={symbol}"
        or request.get("end_date") != end
        or not isinstance(request.get("start_date"), str)
        or request["start_date"] != start
    ):
        raise ValueError("collector Tencent capture request is invalid")
    response = receipt["response"]
    if not isinstance(response, dict) or set(response) != {"raw", "fields", "rows"}:
        raise ValueError("collector Tencent capture response is invalid")
    raw = response["raw"]
    if (
        response["fields"] != "date,open,high,low,close,volume"
        or not _is_exact_tencent_envelope(raw, symbol)
        or not isinstance(response["rows"], list)
    ):
        raise ValueError("collector Tencent capture response is invalid")
    parsed = parse_tencent_bar(raw, code)
    if parsed is None or not start <= parsed["date"] <= end:
        raise ValueError("collector Tencent capture response is invalid")
    expected_volume = float(parsed["volume"]) * 100.0
    if response["rows"] != [[
        parsed["date"],
        str(parsed["open"]),
        str(parsed["high"]),
        str(parsed["low"]),
        str(parsed["close"]),
        str(expected_volume),
    ]]:
        raise ValueError("collector Tencent capture response is invalid")


def _has_exact_collector_session_daily(
    cache: Cache,
    code: str,
    start: str,
    session: str,
    coverage: tuple[str, str] | None,
    source: str,
    adjustment_mode: str,
    adjustment_version: str,
) -> bool:
    """Validate the immutable daily evidence before using it to skip a fetch."""
    rows = cache._conn.execute(
        "SELECT d.open,d.high,d.low,d.close,d.volume,d.source,d.adjustment_mode,"
        "d.adjustment_version,d.retrieved_at,d.is_final,d.receipt_id,"
        "r.source AS receipt_source,r.observed_at,"
        "r.request_json,r.response_json,r.response_sha256 "
        "FROM main.daily AS d "
        "LEFT JOIN main.collection_receipts AS r ON r.receipt_id=d.receipt_id "
        "WHERE d.code=? AND d.date=?",
        (code, session),
    ).fetchall()
    if not rows:
        return False
    if len(rows) != 1:
        raise ValueError("collector current-session daily evidence is ambiguous")

    row = rows[0]
    if (
        (row["source"], row["adjustment_mode"], row["adjustment_version"])
        != (source, adjustment_mode, adjustment_version)
        or row["is_final"] != 1
        or row["receipt_id"] is None
        or row["receipt_source"] != source
        or not isinstance(row["observed_at"], str)
        or row["retrieved_at"] != row["observed_at"]
    ):
        raise ValueError("collector current-session daily evidence is invalid")
    request, _ = _decode_canonical_json(row["request_json"])
    response, response_sha256 = _decode_canonical_json(row["response_json"])
    if response_sha256 != row["response_sha256"]:
        raise ValueError("collector current-session daily receipt is invalid")
    symbol = to_tencent(code)
    if coverage is None:
        expected_start = start
    elif coverage[1] < session:
        gaps = _missing_ranges(coverage, start, session)
        if len(gaps) != 1 or gaps[0][1] != session:
            raise ValueError("collector current-session coverage is invalid")
        expected_start = gaps[0][0]
    else:
        expected_start = None
    request_is_valid = (
        set(request) == {"method", "url", "start_date", "end_date"}
        and request.get("method") == "qt"
        and request.get("url") == f"https://qt.gtimg.cn/q={symbol}"
        and request.get("end_date") == session
        and isinstance(request.get("start_date"), str)
        and start <= request["start_date"] <= session
        and (expected_start is None or request["start_date"] == expected_start)
    ) if isinstance(request, dict) else False
    if (
        not request_is_valid
        or not isinstance(response, dict)
        or set(response) != {"raw", "fields", "rows"}
    ):
        raise ValueError("collector current-session daily raw identity is invalid")
    raw = response["raw"]
    parsed = (
        parse_tencent_bar(raw, code)
        if _is_exact_tencent_envelope(raw, symbol)
        else None
    )
    if parsed is None or parsed["date"] != session:
        raise ValueError("collector current-session daily raw response is invalid")
    expected_values = (
        parsed["open"],
        parsed["high"],
        parsed["low"],
        parsed["close"],
        float(parsed["volume"]) * 100.0,
    )
    if (
        tuple(row[column] for column in ("open", "high", "low", "close", "volume"))
        != expected_values
        or response["fields"] != "date,open,high,low,close,volume"
        or response["rows"] != [[
            session,
            str(parsed["open"]),
            str(parsed["high"]),
            str(parsed["low"]),
            str(parsed["close"]),
            str(expected_values[-1]),
        ]]
    ):
        raise ValueError("collector current-session daily evidence is invalid")
    return True


def sync_symbols(
    cache: Cache,
    codes: Iterable[str],
    start: str,
    end: str,
    *,
    adjustment_mode: str = "qfq",
    adjustment_version: str | None = None,
    source: str = "baostock",
    fetcher: Callable[[str, str, str], list[dict]] | None = None,
) -> dict:
    """Synchronize a bounded symbol/date set, committing each symbol separately."""
    if start > end:
        raise ValueError("start must be <= end")
    if end > default_final_date():
        raise ValueError("end must not be later than the latest finalized date")
    if adjustment_mode not in _VERSIONS:
        raise ValueError(f"unsupported adjustment_mode: {adjustment_mode}")

    if adjustment_version is None and source != "baostock":
        raise ValueError("adjustment_version is required for a non-baostock source")
    version = adjustment_version or _VERSIONS[adjustment_mode]
    fetch = fetcher or _default_fetcher(adjustment_mode)
    normalized = list(dict.fromkeys(normalize(code) for code in codes))
    if not normalized:
        raise ValueError("at least one code is required")

    collector_price_sync = _is_collector_tencent_price_sync(
        cache, source, adjustment_mode, version
    )
    if cache.is_collector_database:
        cache._require_collector_writer(
            step_id="post_close_prices", session=end
        )
        if not collector_price_sync:
            raise ValueError("collector price capture requires Tencent raw identity")
    if collector_price_sync:
        from .collector_continuity import require_collector_continuity_health

        require_collector_continuity_health()
    current_session = end if end == latest_finalized_date() else None
    results = []
    for code in normalized:
        try:
            coverage = cache.sync_coverage(
                code, source, adjustment_mode, version
            )
            covered = coverage
            if covered is None:
                covered = cache.covered_range(
                    code,
                    source=source,
                    adjustment_mode=adjustment_mode,
                    adjustment_version=version,
                    finalized_only=True,
                )
            gaps = _missing_ranges(covered, start, end)
            if collector_price_sync and current_session is not None:
                has_current_daily = _has_exact_collector_session_daily(
                    cache,
                    code,
                    start,
                    current_session,
                    coverage,
                    source,
                    adjustment_mode,
                    version,
                )
                if has_current_daily:
                    # The proven receipt exactly spans the right-side gap from
                    # the baseline coverage through the current session. Only
                    # monotonic coverage may be committed; evidence is reused.
                    gaps = []
                elif not any(
                    gap_start <= current_session <= gap_end
                    for gap_start, gap_end in gaps
                ):
                    gaps.append((current_session, current_session))
            if not gaps:
                if collector_price_sync and coverage is not None:
                    if coverage[0] > start or coverage[1] < end:
                        cache._conn.execute(
                            "UPDATE main.sync_coverage SET "
                            "start_date=MIN(start_date,?),end_date=MAX(end_date,?) "
                            "WHERE code=? AND source=? AND adjustment_mode=? "
                            "AND adjustment_version=?",
                            (start, end, code, source, adjustment_mode, version),
                        )
                        cache._conn.commit()
                else:
                    cache.record_sync_coverage(
                        code, source, adjustment_mode, version, start, end
                    )
                results.append({"code": code, "status": "up_to_date", "written": 0})
                continue

            fetched: list[dict] = []
            capture_receipts = []
            empty_collector_response = False
            for fetch_start, fetch_end in gaps:
                response = fetch(code, fetch_start, fetch_end)
                response_bars = [
                    bar for bar in response
                    if fetch_start <= bar.get("date", "") <= fetch_end
                ]
                if collector_price_sync and not response_bars:
                    empty_collector_response = True
                    break
                receipt = getattr(response, "capture_receipt", None)
                if collector_price_sync:
                    _validate_collector_tencent_capture(
                        receipt, code, fetch_start, fetch_end
                    )
                    parsed = parse_tencent_bar(receipt["response"]["raw"], code)
                    if parsed is None:
                        raise ValueError("collector Tencent capture bars are invalid")
                    expected_bar = {
                        "date": parsed["date"],
                        "open": parsed["open"],
                        "high": parsed["high"],
                        "low": parsed["low"],
                        "close": parsed["close"],
                        "volume": float(parsed["volume"]) * 100.0,
                    }
                    if (
                        len(response_bars) != 1
                        or response_bars[0].get("_capture_receipt") is not receipt
                        or any(
                            response_bars[0].get(field) != value
                            for field, value in expected_bar.items()
                        )
                        or response_bars[0].get("retrieved_at") != receipt["observed_at"]
                    ):
                        raise ValueError("collector Tencent capture bars are invalid")
                if receipt is not None:
                    capture_receipts.append(receipt)
                fetched.extend(response_bars)

            if empty_collector_response:
                results.append({
                    "code": code,
                    "status": "no_data",
                    "start": gaps[0][0],
                    "end": end,
                    "written": 0,
                })
                continue

            by_date = {}
            for bar in fetched:
                if bar.get("is_final") is False:
                    raise ValueError(
                        f"fetcher returned unfinished bar for {code} {bar.get('date')}"
                    )
                for field, expected in (
                    ("source", source),
                    ("adjustment_mode", adjustment_mode),
                    ("adjustment_version", version),
                ):
                    if field in bar and bar[field] != expected:
                        if (
                            collector_price_sync
                            and field == "adjustment_version"
                        ):
                            bar[field] = expected
                            continue
                        raise ValueError(
                            f"fetcher {field} {bar[field]!r} conflicts with "
                            f"requested {expected!r}"
                        )
                bar["is_final"] = True
                by_date[bar["date"]] = bar
            bars = [by_date[day] for day in sorted(by_date)]
            written = cache.upsert(
                code,
                bars,
                source=source,
                adjustment_mode=adjustment_mode,
                adjustment_version=version,
                is_final=True,
                capture_receipts=capture_receipts,
            ) if bars or capture_receipts else 0
            # 历史区间无有效 bar 时不扩展 coverage：数据源临时故障/停牌不应被
            # 永久记为"已完成"。停牌股会因此每次重试，这是 fail-closed 的可接受代价。
            if bars:
                coverage_end = end
                if end == latest_finalized_date() and end not in by_date:
                    coverage_end = (
                        date.fromisoformat(end) - timedelta(days=1)
                    ).isoformat()
                if start <= coverage_end:
                    if not (
                        collector_price_sync
                        and coverage is not None
                        and coverage[0] <= start
                        and coverage[1] >= coverage_end
                    ):
                        cache.record_sync_coverage(
                            code, source, adjustment_mode, version, start, coverage_end
                        )
            results.append({
                "code": code,
                "status": "synced" if bars else "no_data",
                "start": gaps[0][0],
                "end": end,
                "written": written,
            })
        except Exception as exc:
            results.append({
                "code": code,
                "status": "error",
                "start": gaps[0][0],
                "end": end,
                "written": 0,
                "error": f"{type(exc).__name__}: {exc}",
            })

    return {
        "start": start,
        "end": end,
        "source": source,
        "adjustment_mode": adjustment_mode,
        "adjustment_version": version,
        "symbols": results,
        "synced": sum(item["status"] == "synced" for item in results),
        "up_to_date": sum(item["status"] == "up_to_date" for item in results),
        "errors": sum(item["status"] == "error" for item in results),
    }
