#!/usr/bin/env python3
"""Export one shadow-only receipted daily-bar DataManifest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stockdata.daily_bar_product import (  # noqa: E402
    build_daily_bar_manifest,
    write_daily_bar_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--code", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--source", default="baostock")
    parser.add_argument("--adjustment-mode", choices=("raw", "qfq"), required=True)
    parser.add_argument("--adjustment-version", required=True)
    parser.add_argument("--universe-version", required=True)
    parser.add_argument("--trading-calendar-version", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    manifest = build_daily_bar_manifest(
        args.database,
        code=args.code,
        start=args.start,
        end=args.end,
        source=args.source,
        adjustment_mode=args.adjustment_mode,
        adjustment_version=args.adjustment_version,
        universe_version=args.universe_version,
        trading_calendar_version=args.trading_calendar_version,
    )
    output = write_daily_bar_manifest(args.output_root, manifest)
    print(json.dumps({
        "manifest_id": manifest["manifest_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "data_product_id": manifest["dataset_ids"][0],
        "authority_grade": "shadow",
        "decision_eligible": False,
        "source_authentication": manifest["source_authentication"],
        "quality_status": manifest["quality_status"],
        "output": str(output),
        "decision_authority": False,
        "actions": [],
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
