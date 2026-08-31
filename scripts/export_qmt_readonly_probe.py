#!/usr/bin/env python3
"""Export a non-authoritative probe of already-local QMT daily bars."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stockdata.qmt_readonly_probe import (  # noqa: E402
    QmtReadonlyProbeError,
    build_qmt_readonly_probe,
    load_qmt_readonly_probe,
    write_qmt_readonly_probe,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--code", action="append", required=True)
    capture.add_argument("--start", required=True)
    capture.add_argument("--end", required=True)
    capture.add_argument(
        "--adjustment-mode", action="append", choices=("raw", "qfq")
    )
    capture.add_argument("--output-root", required=True, type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--artifact", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            artifact = load_qmt_readonly_probe(args.artifact)
            output = args.artifact
        else:
            artifact = build_qmt_readonly_probe(
                codes=args.code,
                start=args.start,
                end=args.end,
                adjustment_modes=args.adjustment_mode,
            )
            output = write_qmt_readonly_probe(args.output_root, artifact)
    except QmtReadonlyProbeError as exc:
        print(json.dumps({
            "status": "rejected",
            "reason": str(exc),
            "decision_authority": False,
            "actions": [],
        }, ensure_ascii=False, indent=1))
        return 2
    print(json.dumps({
        "status": (
            "verified_diagnostic_only" if args.command == "verify" else "captured"
        ),
        "artifact_sha256": artifact["artifact_sha256"],
        "authority_grade": artifact["authority_grade"],
        "decision_eligible": False,
        "decision_authority": False,
        "actions": [],
        "output": str(output),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
