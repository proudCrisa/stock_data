#!/usr/bin/env python3
"""Export the Stage 2C sz159655 imported frozen observation manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stockdata.imported_frozen_daily import (
    build_imported_frozen_daily_manifest,
    write_imported_frozen_daily_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preimage", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--imported-at", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    manifest = build_imported_frozen_daily_manifest(
        args.preimage,
        expected_sha256=args.expected_sha256,
        imported_at=args.imported_at,
    )
    output = write_imported_frozen_daily_manifest(args.output_root, manifest)
    print(
        json.dumps(
            {
                "manifest_id": manifest["manifest_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "profile": manifest["profile"],
                "authority_grade": manifest["authority_grade"],
                "decision_eligible": manifest["decision_eligible"],
                "decision_authority": manifest["decision_authority"],
                "actions": manifest["actions"],
                "output": str(output),
            },
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
