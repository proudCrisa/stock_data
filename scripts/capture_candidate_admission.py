#!/usr/bin/env python3
"""Capture bounded BaoStock evidence for trading candidate admission."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockdata.candidate_admission_capture import (  # noqa: E402
    BaoStockAdmissionSource,
    build_capture,
    publish_capture,
)


def _requested(args: argparse.Namespace) -> list[str]:
    values = [part.strip() for part in args.codes.split(",") if part.strip()]
    if args.codes_file:
        values.extend(
            line.strip()
            for line in Path(args.codes_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="采集最多 20 只 A 股的当日 BaoStock 原始准入证据"
    )
    parser.add_argument("--date", required=True)
    parser.add_argument("--codes", default="", help="逗号分隔的 sh/sz/bj 代码")
    parser.add_argument("--codes-file", default="")
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    with BaoStockAdmissionSource() as source:
        capture = build_capture(_requested(args), asof=args.date, capture_one=source)
    output = publish_capture(capture, args.output_root)
    print(
        json.dumps(
            {
                "asof": args.date,
                "requested": len(capture["requested_codes"]),
                "captured": len(capture["records"]),
                "blocked": capture["blockers"],
                "artifact_sha256": capture["artifact_sha256"],
                "output": str(output),
                "decision_authority": False,
                "actions": [],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
