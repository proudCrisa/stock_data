from __future__ import annotations

import json

import pytest

from stockdata.research_corporate_actions import (
    ResearchCorporateActionError,
    build_corporate_action_artifact,
    verify_corporate_action_artifact,
)


def _actions() -> dict[str, list[dict[str, object]]]:
    return {
        "600000.SH": [{"dividPayDate": "2026-06-01", "dividCashPsAfterTax": "0.1"}],
        "000001.SZ": [{"dividPayDate": "2026-05-20", "dividCashPsAfterTax": "0.2"}],
    }


def _build(tmp_path):
    return build_corporate_action_artifact(
        _actions(),
        observation_date="2026-08-05",
        retrieved_at="2026-08-05T00:00:00+00:00",
        source_receipt={"provider": "baostock", "query": "test"},
        output_root=tmp_path,
    )


def test_corporate_action_artifact_preserves_research_limitations(tmp_path):
    artifact = _build(tmp_path)
    manifest = verify_corporate_action_artifact(artifact)

    assert manifest["execution_grade"] is False
    assert manifest["point_in_time_verified"] is False
    assert manifest["revision_complete"] is False
    assert manifest["row_count"] == 2


def test_duplicate_action_is_rejected(tmp_path):
    actions = {"600000.SH": [_actions()["600000.SH"][0]] * 2}
    with pytest.raises(ResearchCorporateActionError, match="duplicate"):
        build_corporate_action_artifact(
            actions,
            observation_date="2026-08-05",
            retrieved_at="2026-08-05T00:00:00+00:00",
            source_receipt={"provider": "baostock", "query": "test"},
            output_root=tmp_path,
        )


def test_corporate_action_tampering_is_rejected(tmp_path):
    artifact = _build(tmp_path)
    action_file = artifact / "actions.jsonl"
    lines = action_file.read_text(encoding="ascii").splitlines()
    row = json.loads(lines[0])
    row["data"]["dividCashPsAfterTax"] = "99"
    lines[0] = json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    action_file.write_text("\n".join(lines) + "\n", encoding="ascii")

    with pytest.raises(
        ResearchCorporateActionError, match="action rows are not canonical"
    ):
        verify_corporate_action_artifact(artifact)
