#!/usr/bin/env python3
"""Compare Tencent raw history with Baostock raw history."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stockdata.fetch_baostock import fetch_baostock
from stockdata.fetch_tencent_history import fetch_tencent_history, reconcile_tencent_baostock


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    tencent = fetch_tencent_history(args.code, args.start, args.end, adjustment_mode="raw")
    baostock = fetch_baostock(args.code, args.start, args.end, adjustment_mode="raw")
    report = reconcile_tencent_baostock(tencent, baostock)
    report.update({"code": args.code, "start": args.start, "end": args.end, "adjustment_mode": "raw"})
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["exact_match"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
