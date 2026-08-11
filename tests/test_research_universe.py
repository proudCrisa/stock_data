from __future__ import annotations

import pytest

from stockdata.research_universe import (
    ResearchUniverseError,
    build_universe_artifact,
    verify_universe_artifact,
)


def _rows():
    return [
        {
            "requested_date": "2026-06-30",
            "effective_date": "2026-06-29",
            "index": "hs300",
            "symbol": "sh.600000",
            "name": "浦发银行",
        }
    ]


def test_universe_artifact_preserves_scope_limit(tmp_path):
    artifact = build_universe_artifact(
        _rows(),
        retrieved_at="2026-08-05T00:00:00+00:00",
        source_receipt={"provider": "baostock", "queries": ["query_hs300_stocks"]},
        output_root=tmp_path,
    )
    manifest = verify_universe_artifact(artifact)
    assert manifest["scope"] == "index_membership_only"
    assert manifest["complete_panel"] is False
    assert manifest["point_in_time_verified"] is False
    assert manifest["execution_grade"] is False


def test_universe_rejects_wrong_row_schema(tmp_path):
    with pytest.raises(ResearchUniverseError, match="invalid schema"):
        build_universe_artifact(
            [{"requested_date": "2026-06-30"}],
            retrieved_at="2026-08-05T00:00:00+00:00",
            source_receipt={"provider": "baostock", "queries": []},
            output_root=tmp_path,
        )
