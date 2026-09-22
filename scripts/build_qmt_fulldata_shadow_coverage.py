#!/usr/bin/env python3
"""Build a presence-only QMT fulldata shadow coverage manifest."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stockdata.qmt_fulldata_shadow_coverage import (  # noqa: E402
    QmtFulldataShadowCoverageError, write_coverage,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        path = write_coverage(args.evidence_root)
    except QmtFulldataShadowCoverageError as exc:
        print(json.dumps({"status": "rejected", "decision_eligible": False, "actions": [], "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": "registered_shadow_artifact_presence", "decision_eligible": False, "actions": [], "manifest": str(path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
