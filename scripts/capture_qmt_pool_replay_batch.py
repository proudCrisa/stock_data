#!/usr/bin/env python3
"""Explicit single-acquisition capture for the fixed RQGM forward cohort."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stockdata.qmt_pool_batch_capture import capture_qmt_pool_replay_batch  # noqa: E402
from stockdata.qmt_pool_replay import QmtPoolReplayClient, QmtPoolReplayError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = capture_qmt_pool_replay_batch(
            QmtPoolReplayClient(base_url=args.base_url), args.symbols, args.output_root
        )
    except (QmtPoolReplayError, OSError) as exc:
        print(json.dumps({"status": "INCOMPLETE", "decision_eligible": False,
                          "decision_authority": False, "actions": [], "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
