#!/usr/bin/env python3
"""Fetch Tencent historical bars into a research-only artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stockdata.fetch_tencent_history import fetch_tencent_history, write_tencent_history_artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--adjustment-mode", choices=("raw", "qfq", "hfq"), default="raw")
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    captured = fetch_tencent_history(args.code, args.start, args.end, adjustment_mode=args.adjustment_mode)
    artifact = write_tencent_history_artifact(
        args.output_root,
        code=args.code,
        start=args.start,
        end=args.end,
        adjustment_mode=args.adjustment_mode,
        captured=captured,
    )
    pages = captured.capture_receipt["response"]["pages"]
    print(json.dumps({
        "artifact": str(artifact),
        "bar_count": len(captured),
        "coverage_start": captured.capture_receipt["response"]["coverage_start"],
        "coverage_end": captured.capture_receipt["response"]["coverage_end"],
        "page_count": len(pages),
        "response_sha256": [page["response"]["response_sha256"] for page in pages],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
