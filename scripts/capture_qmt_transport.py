#!/usr/bin/env python3
"""Capture one QMT v2 response as shadow-only content-addressed evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stockdata.qmt_daily_bar_product import (  # noqa: E402
    QmtDailyBarProductError,
    build_qmt_daily_bar_product,
    write_qmt_daily_bar_product,
)
from stockdata.qmt_transport_capture import (  # noqa: E402
    QmtTransportCaptureClient,
    QmtTransportCaptureError,
    write_qmt_transport_snapshot,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--count", type=int, default=250)
    parser.add_argument("--adjustment", choices=("raw", "qfq"), default="raw")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--wait-timeout", type=float, default=90.0)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        capture = QmtTransportCaptureClient(base_url=args.base_url).capture(
            args.symbols, count=args.count, adjustment=args.adjustment,
            wait_timeout=args.wait_timeout,
        )
        snapshot_path = write_qmt_transport_snapshot(args.output_root, capture)
        product = build_qmt_daily_bar_product(capture)
        product_path = write_qmt_daily_bar_product(args.output_root, product)
    except (QmtTransportCaptureError, QmtDailyBarProductError) as exc:
        print(json.dumps({
            "status": "rejected", "decision_eligible": False, "actions": [],
            "error": str(exc),
        }, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "captured_shadow_only", "authority_grade": "shadow",
        "decision_eligible": False, "actions": [],
        "permitted_uses": product["permitted_uses"],
        "snapshot_sha256": capture["snapshot_sha256"],
        "product_sha256": product["product_sha256"],
        "snapshot": str(snapshot_path), "product": str(product_path),
    }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
