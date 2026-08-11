#!/usr/bin/env python3
"""Fetch research-only historical dividend observations from Baostock."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from stockdata.forward_corporate_actions import fetch_baostock_corporate_actions
from stockdata.research_corporate_actions import build_corporate_action_artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--observation-date", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        symbols = tuple(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT code FROM daily ORDER BY code"
            )
        )
    actions = fetch_baostock_corporate_actions(symbols, args.observation_date)
    artifact = build_corporate_action_artifact(
        actions,
        observation_date=args.observation_date,
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_receipt={
            "provider": "baostock",
            "query": "query_dividend_data",
            "symbol_count": len(symbols),
            "sdk_version": "00.9.20",
        },
        output_root=args.output_root,
    )
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
