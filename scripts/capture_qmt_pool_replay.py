#!/usr/bin/env python3
"""Seal existing QMT pool symbols through read-only loopback endpoints."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stockdata.qmt_pool_replay import (  # noqa: E402
    QmtPoolReplayClient,
    QmtPoolReplayError,
    write_qmt_pool_replay,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        artifact = QmtPoolReplayClient(base_url=args.base_url).capture(args.symbols)
        output = write_qmt_pool_replay(args.output_root, artifact)
    except QmtPoolReplayError as exc:
        print(json.dumps({"status": "rejected", "decision_eligible": False, "actions": [], "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": "sealed_shadow_only", "decision_eligible": False, "actions": [], "output": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
