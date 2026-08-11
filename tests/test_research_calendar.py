from __future__ import annotations

import json

import pytest

from stockdata.research_calendar import (
    ResearchCalendarError,
    build_calendar_artifact,
    verify_calendar_artifact,
)


def _rows() -> list[dict[str, object]]:
    return [
        {"date": "2026-01-01", "is_trading_day": False},
        {"date": "2026-01-02", "is_trading_day": True},
        {"date": "2026-01-03", "is_trading_day": False},
    ]


def _build(tmp_path):
    return build_calendar_artifact(
        _rows(),
        coverage_start="2026-01-01",
        coverage_end="2026-01-03",
        retrieved_at="2026-01-04T00:00:00+00:00",
        source_receipt={"provider": "baostock", "query": "test"},
        output_root=tmp_path,
    )


def test_calendar_artifact_is_content_addressed_and_research_only(tmp_path):
    artifact = _build(tmp_path)
    manifest = verify_calendar_artifact(artifact)

    assert manifest["execution_grade"] is False
    assert manifest["authority_status"] == "research_vendor_only"
    assert manifest["row_count"] == 3
    assert _build(tmp_path) == artifact


def test_calendar_requires_contiguous_ordered_coverage(tmp_path):
    with pytest.raises(ResearchCalendarError, match="every date"):
        build_calendar_artifact(
            [_rows()[0], _rows()[2]],
            coverage_start="2026-01-01",
            coverage_end="2026-01-03",
            retrieved_at="2026-01-04T00:00:00+00:00",
            source_receipt={"provider": "baostock", "query": "test"},
            output_root=tmp_path,
        )


def test_calendar_tampering_is_rejected(tmp_path):
    artifact = _build(tmp_path)
    calendar = artifact / "calendar.jsonl"
    rows = calendar.read_text(encoding="ascii").splitlines()
    rows[1] = json.dumps(
        {"date": "2026-01-02", "is_trading_day": False},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    calendar.write_text("\n".join(rows) + "\n", encoding="ascii")

    with pytest.raises(ResearchCalendarError, match="hash mismatch"):
        verify_calendar_artifact(artifact)
