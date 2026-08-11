import json
import os

import pytest

from stockdata.execution_snapshot import (
    ARTIFACT_SCHEMAS,
    create_execution_snapshot,
    verify_execution_snapshot,
)


def _row(kind, day):
    base = {
        "effective_date": day,
        "available_at": f"{day}T{'15:01:00' if kind.endswith('prices') else '09:00:00'}+08:00",
    }
    if kind.endswith("prices"):
        return {**base, "symbol": "000001.SZ", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 1000.0}
    if kind == "corporate_actions":
        return {**base, "symbol": "000001.SZ", "action_type": "cash_dividend", "payload": {"cash_per_share": 0.1}}
    if kind == "instrument_status":
        return {**base, "symbol": "000001.SZ", "listing_status": "listed", "board": "MAIN", "is_st": False, "is_suspended": False}
    if kind == "universe":
        return {**base, "symbol": "000001.SZ", "is_member": True}
    return {**base, "rule_id": "main-limit-v1", "rule_type": "price_limit", "parameters": {"fraction": 0.1}}


def _inputs():
    artifacts = {kind: [_row(kind, "2024-01-02")] for kind in ARTIFACT_SCHEMAS}
    authorities = {kind: f"authority:{kind}:v1" for kind in ARTIFACT_SCHEMAS}
    return artifacts, authorities


def test_execution_snapshot_is_content_addressed_and_idempotent(tmp_path):
    artifacts, authorities = _inputs()
    first = create_execution_snapshot(
        tmp_path,
        coverage_start="2024-01-01",
        coverage_end="2024-12-31",
        artifacts=artifacts,
        authorities=authorities,
        selection_policy_id="all-a-shares/v1",
        rulebook_id="cn-a-share/v1",
    )
    second = create_execution_snapshot(
        tmp_path,
        coverage_start="2024-01-01",
        coverage_end="2024-12-31",
        artifacts=artifacts,
        authorities=authorities,
        selection_policy_id="all-a-shares/v1",
        rulebook_id="cn-a-share/v1",
    )

    assert first["snapshot_id"] == second["snapshot_id"]
    root = tmp_path / first["snapshot_id"]
    assert verify_execution_snapshot(root)["snapshot_id"] == first["snapshot_id"]
    assert os.stat(root).st_mode & 0o222 == 0
    assert all(os.stat(path).st_mode & 0o222 == 0 for path in root.iterdir())


def test_missing_or_non_raw_execution_evidence_fails_closed(tmp_path):
    artifacts, authorities = _inputs()
    artifacts.pop("corporate_actions")
    with pytest.raises(ValueError, match="all six"):
        create_execution_snapshot(
            tmp_path,
            coverage_start="2024-01-01",
            coverage_end="2024-12-31",
            artifacts=artifacts,
            authorities=authorities,
            selection_policy_id="all-a-shares/v1",
            rulebook_id="cn-a-share/v1",
        )

    artifacts, authorities = _inputs()
    with pytest.raises(ValueError, match="must use raw"):
        create_execution_snapshot(
            tmp_path,
            coverage_start="2024-01-01",
            coverage_end="2024-12-31",
            artifacts=artifacts,
            authorities=authorities,
            execution_price_basis="qfq",
            selection_policy_id="all-a-shares/v1",
            rulebook_id="cn-a-share/v1",
        )


def test_artifact_tampering_is_detected(tmp_path):
    artifacts, authorities = _inputs()
    manifest = create_execution_snapshot(
        tmp_path,
        coverage_start="2024-01-01",
        coverage_end="2024-12-31",
        artifacts=artifacts,
        authorities=authorities,
        selection_policy_id="all-a-shares/v1",
        rulebook_id="cn-a-share/v1",
    )
    root = tmp_path / manifest["snapshot_id"]
    path = root / "universe.jsonl"
    os.chmod(root, 0o755)
    os.chmod(path, 0o644)
    row = json.loads(path.read_text().strip())
    row["is_member"] = False
    path.write_text(json.dumps(row) + "\n")

    with pytest.raises(ValueError, match="hash mismatch"):
        verify_execution_snapshot(root)


def test_rows_require_authority_schema_and_pit_time(tmp_path):
    artifacts, authorities = _inputs()
    artifacts["universe"][0].pop("is_member")
    with pytest.raises(ValueError, match="missing fields"):
        create_execution_snapshot(
            tmp_path,
            coverage_start="2024-01-01",
            coverage_end="2024-12-31",
            artifacts=artifacts,
            authorities=authorities,
            selection_policy_id="all-a-shares/v1",
            rulebook_id="cn-a-share/v1",
        )

    artifacts, authorities = _inputs()
    artifacts["corporate_actions"][0]["available_at"] = "2024-01-02T15:01:00+08:00"
    with pytest.raises(ValueError, match="execution cutoff"):
        create_execution_snapshot(
            tmp_path,
            coverage_start="2024-01-01",
            coverage_end="2024-12-31",
            artifacts=artifacts,
            authorities=authorities,
            selection_policy_id="all-a-shares/v1",
            rulebook_id="cn-a-share/v1",
        )

    artifacts, authorities = _inputs()
    artifacts["universe"][0]["available_at"] = "2024-01-02T15:01:00"
    with pytest.raises(ValueError, match="timezone"):
        create_execution_snapshot(
            tmp_path,
            coverage_start="2024-01-01",
            coverage_end="2024-12-31",
            artifacts=artifacts,
            authorities=authorities,
            selection_policy_id="all-a-shares/v1",
            rulebook_id="cn-a-share/v1",
        )
