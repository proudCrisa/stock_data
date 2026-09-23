from __future__ import annotations

import copy
import json

import pytest

from stockdata import candidate_admission_capture as capture
from stockdata import industry_research_snapshot as industry


ASOF = "2026-09-08"
OBSERVED = f"{ASOF}T14:00:00+08:00"
CUTOFF = f"{ASOF}T14:30:00+08:00"


def _row(code: str, _asof: str) -> dict:
    provider_code = f"{code[:2]}.{code[2:]}"
    raw = {
        "updateDate": ASOF,
        "code": provider_code,
        "code_name": "浦发银行" if code == "sh600000" else "平安银行",
        "industry": "J66",
        "industryClassification": "证监会行业分类",
    }
    return {
        "code": code,
        "finance_rows": [],
        "industry_rows": [{
            "update_date": raw["updateDate"],
            "code": code,
            "industry": raw["industry"],
            "classification": raw["industryClassification"],
            "raw": raw,
        }],
        "corporate_action_rows": [],
    }


def _capture(codes=("sh600000", "sz000001")) -> dict:
    return capture.build_capture(
        list(codes), asof=ASOF, capture_one=_row, observed_at=OBSERVED)


def _reseal(value: dict) -> None:
    value["artifact_sha256"] = capture._sha256({
        key: item for key, item in value.items() if key != "artifact_sha256"})


def test_snapshot_replays_exact_current_observation_and_writes_by_hash(tmp_path):
    source = _capture()

    snapshot = industry.build_industry_research_snapshot(
        source, decision_cutoff=CUTOFF)
    path = industry.write_industry_research_snapshot(tmp_path, snapshot)

    assert path.name == f"{snapshot['snapshot_sha256']}.json"
    assert industry.load_industry_research_snapshot(path) == snapshot
    assert snapshot["schema_version"] == "stockdata-industry-research-snapshot/1"
    assert snapshot["asof"] == ASOF
    assert snapshot["observed_at"] == snapshot["available_at"] == OBSERVED
    assert snapshot["source"] == {
        "provider": "baostock",
        "sdk_package_version": source["source_receipt"]["sdk_package_version"],
        "capture_schema_version": capture.SCHEMA_VERSION,
        "capture_sha256": source["artifact_sha256"],
    }
    assert snapshot["taxonomy"] == {
        "id": industry.TAXONOMY_ID, "version": "unknown"}
    assert snapshot["availability_basis"] == "capture_observed_at"
    assert snapshot["authority"] == source["authority"]
    assert snapshot["actions"] == []
    assert snapshot["records"][0]["provider_update_date"] == ASOF
    assert "effective_from" not in snapshot["records"][0]
    assert snapshot["records"][0]["raw"] == \
        source["records"][0]["industry_rows"][0]["raw"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_snapshot_excludes_mapping_observed_after_cutoff():
    snapshot = industry.build_industry_research_snapshot(
        _capture(), decision_cutoff=f"{ASOF}T13:59:59+08:00")

    assert snapshot["status"] == "unavailable"
    assert snapshot["reason"] == "mapping_observed_after_cutoff"
    assert snapshot["records"] == []
    assert [row["instrument_code"] for row in snapshot["excluded"]] == [
        "sh600000", "sz000001"]
    assert all(row["reason"] == "available_after_cutoff"
               for row in snapshot["excluded"])
    assert snapshot["excluded"][0]["raw"] == \
        snapshot["source_capture"]["records"][0]["industry_rows"][0]["raw"]
    assert industry.verify_industry_research_snapshot(snapshot) == snapshot


@pytest.mark.parametrize("cutoff", [
    "2026-09-07T15:00:00+08:00",
    "2026-09-09T15:00:00+08:00",
])
def test_snapshot_rejects_cross_day_backfill(cutoff):
    with pytest.raises(industry.IndustryResearchSnapshotError, match="backfill"):
        industry.build_industry_research_snapshot(
            _capture(), decision_cutoff=cutoff)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "conflict", "blocker"])
def test_snapshot_fails_closed_on_incomplete_or_nonunique_mapping(fault):
    source = _capture(("sh600000",))
    rows = source["records"][0]["industry_rows"]
    if fault == "missing":
        rows.clear()
    elif fault == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif fault == "conflict":
        other = copy.deepcopy(rows[0])
        other["industry"] = other["raw"]["industry"] = "J67"
        rows.append(other)
    else:
        source["records"].clear()
        source["blockers"] = [{"code": "sh600000", "reason": "provider unavailable"}]
    _reseal(source)

    expected = {
        "missing": "missing", "duplicate": "duplicate",
        "conflict": "conflicting", "blocker": "coverage",
    }[fault]
    with pytest.raises(industry.IndustryResearchSnapshotError, match=expected):
        industry.build_industry_research_snapshot(
            source, decision_cutoff=CUTOFF)


@pytest.mark.parametrize("fault", [
    "raw_industry", "raw_code", "future_update", "classification",
])
def test_snapshot_rejects_resealed_provider_row_drift(fault):
    source = _capture(("sh600000",))
    row = source["records"][0]["industry_rows"][0]
    if fault == "raw_industry":
        row["raw"]["industry"] = "J67"
    elif fault == "raw_code":
        row["raw"]["code"] = "sz.000001"
    elif fault == "future_update":
        row["update_date"] = row["raw"]["updateDate"] = "2026-09-09"
    else:
        row["classification"] = row["raw"]["industryClassification"] = ""
    _reseal(source)

    with pytest.raises(industry.IndustryResearchSnapshotError):
        industry.build_industry_research_snapshot(
            source, decision_cutoff=CUTOFF)


def test_snapshot_tampering_fails_even_after_rehashing():
    snapshot = industry.build_industry_research_snapshot(
        _capture(), decision_cutoff=CUTOFF)
    snapshot["records"][0]["industry"] = "J67"
    unsigned = {key: value for key, value in snapshot.items()
                if key != "snapshot_sha256"}
    snapshot["snapshot_sha256"] = industry._hash(unsigned)

    with pytest.raises(industry.IndustryResearchSnapshotError, match="replay"):
        industry.verify_industry_research_snapshot(snapshot)


def test_cli_reads_capture_and_publishes_snapshot(tmp_path, capsys):
    capture_path = capture.publish_capture(_capture(), tmp_path / "captures")
    output_root = tmp_path / "snapshots"

    assert industry.main([
        "--capture", str(capture_path),
        "--decision-cutoff", CUTOFF,
        "--output-root", str(output_root),
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    snapshot_path = output_root / f"{result['snapshot_sha256']}.json"
    assert result["snapshot_path"] == str(snapshot_path)
    assert industry.load_industry_research_snapshot(snapshot_path)[
        "source"]["capture_sha256"] == _capture()["artifact_sha256"]
