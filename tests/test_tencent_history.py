import json

import pytest

from scripts.verify_research_artifacts import main as verify_research_main
from stockdata.fetch_tencent_history import (
    TencentHistoryError,
    fetch_tencent_history,
    reconcile_tencent_baostock,
    verify_tencent_history_artifact,
    write_tencent_history_artifact,
)


def _page(symbol: str, key: str) -> str:
    rows = [
        ["2024-12-31", "9", "9", "9.2", "8.8", "100", {}],
        ["2025-01-02", "10", "10.1", "10.2", "9.9", "200", {}],
        ["2025-01-03", "11", "11.1", "11.2", "10.9", "300", {}],
    ]
    return f"kline={{\"code\":0,\"msg\":\"\",\"data\":{{\"{symbol}\":{{\"{key}\":{json.dumps(rows)}}}}}}}"


def _fake_get(url, params, timeout):
    assert "newfqkline" in url
    symbol = params["param"].split(",", 1)[0]
    adjustment = params["param"].split(",")[-1] or "raw"
    key = {"raw": "day", "qfq": "qfqday", "hfq": "hfqday"}[adjustment]
    return _page(symbol, key), f"{url}?test=1"


def test_fetch_history_filters_server_window_and_preserves_receipt():
    captured = fetch_tencent_history(
        "600519.SH", "2025-01-01", "2025-01-02", http_get=_fake_get
    )
    assert [bar["date"] for bar in captured] == ["2025-01-02"]
    assert captured[0]["open"] == 10.0
    assert captured[0]["close"] == 10.1
    assert captured[0]["high"] == 10.2
    assert captured[0]["low"] == 9.9
    assert captured[0]["volume"] == 20000.0
    assert captured[0]["adjustment_mode"] == "raw"
    assert captured.capture_receipt["source"] == "tencent-fqkline-history-v1"
    assert captured.capture_receipt["response"]["pages"][0]["response"]["adjustment_key"] == "day"
    assert captured.capture_receipt["response"]["pages"][0]["request"]["params"]["param"].startswith(
        "sh600519,day,2025-01-01,2025-01-02,640,"
    )


def test_fetch_history_partitions_cross_year_requests():
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["param"])
        return _fake_get(url, params, timeout)

    captured = fetch_tencent_history(
        "600519.SH", "2024-12-31", "2025-01-02", http_get=fake_get
    )
    assert len(calls) == 2
    assert "2024-12-31,2024-12-31,640," in calls[0]
    assert "2025-01-01,2025-01-02,640," in calls[1]
    assert [bar["date"] for bar in captured] == ["2024-12-31", "2025-01-02"]


def test_adjustment_mode_selects_distinct_response_key():
    qfq = fetch_tencent_history("600519.SH", "2025-01-02", "2025-01-02", adjustment_mode="qfq", http_get=_fake_get)
    hfq = fetch_tencent_history("600519.SH", "2025-01-02", "2025-01-02", adjustment_mode="hfq", http_get=_fake_get)
    assert qfq.capture_receipt["response"]["pages"][0]["response"]["adjustment_key"] == "qfqday"
    assert hfq.capture_receipt["response"]["pages"][0]["response"]["adjustment_key"] == "hfqday"


def test_artifact_is_content_addressed_and_rejects_tampering(tmp_path):
    captured = fetch_tencent_history("600519.SH", "2025-01-02", "2025-01-02", http_get=_fake_get)
    artifact = write_tencent_history_artifact(
        tmp_path, code="600519.SH", start="2025-01-02", end="2025-01-02", adjustment_mode="raw", captured=captured
    )
    manifest = verify_tencent_history_artifact(artifact)
    assert manifest["research_only"] is True
    assert manifest["execution_grade"] is False
    (artifact / "bars.jsonl").write_text("{}\n", encoding="ascii")
    with pytest.raises(TencentHistoryError, match="rows do not match"):
        verify_tencent_history_artifact(artifact)


def test_artifact_receipt_tampering_is_rejected(tmp_path):
    captured = fetch_tencent_history("600519.SH", "2025-01-02", "2025-01-02", http_get=_fake_get)
    artifact = write_tencent_history_artifact(
        tmp_path, code="600519.SH", start="2025-01-02", end="2025-01-02", adjustment_mode="raw", captured=captured
    )
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["source_receipt"]["response"]["bar_count"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")
    with pytest.raises(TencentHistoryError, match="receipt .* does not match"):
        verify_tencent_history_artifact(artifact)


def test_reconcile_reports_missing_and_value_differences():
    left = [{"date": "2025-01-02", "open": 10, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 200}]
    right = [{"date": "2025-01-02", "open": 10, "high": 10.3, "low": 9.9, "close": 10.1, "volume": 200}, {"date": "2025-01-03", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    report = reconcile_tencent_baostock(left, right)
    assert report["matched_dates"] == 1
    assert report["mismatch_count"] == 1
    assert report["baostock_only_dates"] == ["2025-01-03"]
    assert report["exact_match"] is False


def test_invalid_adjustment_mode_fails_closed():
    with pytest.raises(TencentHistoryError):
        fetch_tencent_history("600519.SH", "2025-01-01", "2025-01-02", adjustment_mode="split", http_get=_fake_get)


def test_research_inventory_includes_tencent_history(tmp_path, monkeypatch, capsys):
    captured = fetch_tencent_history("600519.SH", "2025-01-02", "2025-01-02", http_get=_fake_get)
    write_tencent_history_artifact(
        tmp_path / "tencent-history",
        code="600519.SH",
        start="2025-01-02",
        end="2025-01-02",
        adjustment_mode="raw",
        captured=captured,
    )
    monkeypatch.setattr("sys.argv", ["verify", "--root", str(tmp_path)])
    assert verify_research_main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["research_only"] is True
    assert payload["execution_grade"] is False
    assert [artifact["kind"] for artifact in payload["artifacts"]] == ["tencent-history"]
