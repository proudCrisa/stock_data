#!/usr/bin/env python3
"""Materialize or verify a local research-only QMT frozen dataset."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stockdata.qmt_frozen_research import (  # noqa: E402
    QmtFrozenResearchError,
    build_qmt_frozen_research,
    load_qmt_frozen_research,
    write_qmt_frozen_research,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--latest", required=True, type=Path)
    materialize.add_argument("--receipt", required=True, type=Path)
    materialize.add_argument("--output-root", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--dataset", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "materialize":
            dataset = build_qmt_frozen_research(args.latest, args.receipt)
            output = write_qmt_frozen_research(args.output_root, dataset)
            status = "materialized_shadow_only"
        else:
            dataset = load_qmt_frozen_research(args.dataset)
            output = args.dataset
            status = "verified_shadow_only"
    except QmtFrozenResearchError as exc:
        print(json.dumps({
            "status": "rejected", "error": str(exc), "authority": {
                "advice": False, "decision": False, "evidence": False,
                "judge": False, "production": False, "release": False,
            },
        }, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps({
        "status": status, "dataset_sha256": dataset["dataset_sha256"],
        "symbol_count": dataset["symbol_count"],
        "source_row_count": dataset["source_row_count"],
        "row_count": dataset["row_count"],
        "dropped_zero_placeholder_count": dataset["dropped_zero_placeholder_count"],
        "authority_grade": "shadow", "research_only": True,
        "authority": dataset["authority"], "output": str(output),
    }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
