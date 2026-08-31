#!/usr/bin/env python3
"""Fetch a free, research-only Baostock trading calendar artifact or refresh the cache DB calendar table."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from stockdata.cache import Cache
from stockdata.research_calendar import (
    build_calendar_artifact,
    fetch_baostock_trade_calendar,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--output-root", type=Path)
    target.add_argument("--database", type=Path)
    args = parser.parse_args()

    if args.database is not None:
        cache = Cache(args.database)
        try:
            rows = cache.refresh_trading_calendar(args.start, args.end)
            print(f"refreshed {rows} calendar rows into {args.database}")
        finally:
            cache.close()
        return 0

    rows = fetch_baostock_trade_calendar(args.start, args.end)
    artifact = build_calendar_artifact(
        rows,
        coverage_start=args.start,
        coverage_end=args.end,
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_receipt={
            "provider": "baostock",
            "query": "query_trade_dates",
            "sdk_version": "00.9.20",
        },
        output_root=args.output_root,
    )
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
