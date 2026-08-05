from __future__ import annotations

import json

from stockdata.research_calendar import build_calendar_artifact
from stockdata.research_corporate_actions import build_corporate_action_artifact
from stockdata.research_universe import build_universe_artifact
from scripts.verify_research_artifacts import main


def test_inventory_verifies_all_research_artifact_kinds(tmp_path, monkeypatch, capsys):
    retrieved_at = "2026-08-05T00:00:00+00:00"
    build_calendar_artifact(
        [
            {"date": "2026-01-01", "is_trading_day": False},
            {"date": "2026-01-02", "is_trading_day": True},
        ],
        coverage_start="2026-01-01",
        coverage_end="2026-01-02",
        retrieved_at=retrieved_at,
        source_receipt={"provider": "test", "query": "calendar"},
        output_root=tmp_path / "calendar",
    )
    build_corporate_action_artifact(
        {"600000.SH": [{"event": "dividend"}]},
        observation_date="2026-08-05",
        retrieved_at=retrieved_at,
        source_receipt={"provider": "test", "query": "actions"},
        output_root=tmp_path / "corporate-actions",
    )
    build_universe_artifact(
        [
            {
                "requested_date": "2026-06-30",
                "effective_date": "2026-06-29",
                "index": "hs300",
                "symbol": "sh.600000",
                "name": "浦发银行",
            }
        ],
        retrieved_at=retrieved_at,
        source_receipt={"provider": "test", "queries": ["hs300"]},
        output_root=tmp_path / "index-universe",
    )
    monkeypatch.setattr("sys.argv", ["verify", "--root", str(tmp_path)])

    assert main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["research_only"] is True
    assert payload["execution_grade"] is False
    assert {item["kind"] for item in payload["artifacts"]} == {
        "calendar",
        "corporate-actions",
        "index-universe",
    }
