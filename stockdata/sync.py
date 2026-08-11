"""Bounded, resumable daily history synchronization."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Iterable

from .cache import Cache
from .fetch_baostock import fetch_baostock
from .finalization import latest_finalized_date
from .ticker import normalize

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

    results = []
    for code in normalized:
        covered = cache.sync_coverage(
            code, source, adjustment_mode, version
        )
        if covered is None:
            covered = cache.covered_range(
                code,
                source=source,
                adjustment_mode=adjustment_mode,
                adjustment_version=version,
                finalized_only=True,
            )
        gaps = _missing_ranges(covered, start, end)
        if not gaps:
            cache.record_sync_coverage(
                code, source, adjustment_mode, version, start, end
            )
            results.append({"code": code, "status": "up_to_date", "written": 0})
            continue

        try:
            fetched = []
            capture_receipts = []
            for fetch_start, fetch_end in gaps:
                response = fetch(code, fetch_start, fetch_end)
                receipt = getattr(response, "capture_receipt", None)
                if receipt is not None:
                    capture_receipts.append(receipt)
                fetched.extend(
                    bar for bar in response
                    if fetch_start <= bar.get("date", "") <= fetch_end
                )

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
            coverage_end = end
            if end == latest_finalized_date() and end not in by_date:
                coverage_end = (
                    date.fromisoformat(end) - timedelta(days=1)
                ).isoformat()
            if start <= coverage_end:
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
