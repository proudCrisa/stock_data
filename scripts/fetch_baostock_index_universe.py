#!/usr/bin/env python3
"""Fetch research-only historical index membership from Baostock."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from stockdata.research_universe import (
    INDEX_QUERIES,
    build_universe_artifact,
    fetch_baostock_index_universe,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", action="append", required=True, dest="dates")
    parser.add_argument(
        "--index", action="append", choices=tuple(INDEX_QUERIES), dest="indexes"
    )
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    indexes = tuple(args.indexes or INDEX_QUERIES)
    rows = fetch_baostock_index_universe(args.dates, indexes)
    artifact = build_universe_artifact(
        rows,
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_receipt={
            "provider": "baostock",
            "queries": [INDEX_QUERIES[index] for index in indexes],
            "sdk_version": "00.9.20",
        },
        output_root=args.output_root,
    )
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
