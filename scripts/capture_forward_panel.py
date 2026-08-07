from __future__ import annotations

import argparse
import json
from pathlib import Path

from stockdata.forward_panel_capture import CaptureSpec, capture_phase


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one prospective replay panel phase")
    parser.add_argument("--phase", choices=("pre_open", "post_close"), required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--date", required=True, dest="effective_date")
    parser.add_argument("--symbols", required=True, help="comma-separated cohort symbols")
    parser.add_argument("--source", default="tencent")
    parser.add_argument("--adjustment-version", default="tencent-qt-daily-v1")
    args = parser.parse_args()
    spec = CaptureSpec(
        database=Path(args.database).expanduser(),
        effective_date=args.effective_date,
        symbols=tuple(value.strip() for value in args.symbols.split(",") if value.strip()),
        source=args.source,
        adjustment_version=args.adjustment_version,
    )
    results = capture_phase(spec, args.phase)
    print(json.dumps({"phase": args.phase, "date": args.effective_date, "steps": results}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
