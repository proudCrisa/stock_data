#!/usr/bin/env python3
"""Verify all local research-only artifacts without changing them."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stockdata.research_calendar import verify_calendar_artifact
from stockdata.research_corporate_actions import verify_corporate_action_artifact
from stockdata.research_universe import verify_universe_artifact
from stockdata.fetch_tencent_history import verify_tencent_history_artifact


def _artifacts(root: Path, kind: str, verifier):
    directory = root / kind
    if not directory.is_dir():
        return []
    return [
        {"path": str(path), **verifier(path)}
        for path in sorted(directory.iterdir())
        if path.is_dir()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    artifacts = []
    for kind, verifier in (
        ("calendar", verify_calendar_artifact),
        ("corporate-actions", verify_corporate_action_artifact),
        ("index-universe", verify_universe_artifact),
        ("tencent-history", verify_tencent_history_artifact),
    ):
        for manifest in _artifacts(root, kind, verifier):
            artifacts.append({"kind": kind, **manifest})
    print(
        __import__("json").dumps(
            {
                "schema_version": "stockdata-research-artifact-inventory/1",
                "research_only": True,
                "execution_grade": False,
                "artifacts": artifacts,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
