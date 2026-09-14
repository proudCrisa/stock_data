#!/usr/bin/env python3
"""Capture one QMT fulldata history-kline response as non-decision shadow evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stockdata.qmt_fulldata_shadow_capture import (  # noqa: E402
    QmtFulldataShadowCaptureClient, QmtFulldataShadowCaptureError,
    write_qmt_fulldata_shadow_capture,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--adjustment", required=True, choices=("raw", "qfq"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--wait-timeout", type=float, default=90.0)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        capture = QmtFulldataShadowCaptureClient(base_url=args.base_url).capture(
            symbol=args.symbol, start=args.start, end=args.end, count=args.count,
            adjustment=args.adjustment, wait_timeout=args.wait_timeout,
        )
        output = write_qmt_fulldata_shadow_capture(args.output_root, capture)
    except QmtFulldataShadowCaptureError as exc:
        print(json.dumps({"status": "rejected", "decision_eligible": False,
                          "actions": [], "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": "captured_shadow_only", "authority_grade": "shadow",
                      "decision_eligible": False, "actions": [],
                      "capture_sha256": capture["capture_sha256"], "capture": str(output)},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
