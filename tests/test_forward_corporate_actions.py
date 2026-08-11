import hashlib
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stockdata.cache import Cache
from stockdata.forward_capture import _bind_cohort
from stockdata.forward_corporate_actions import (
    SOURCE,
    CapturedCorporateActions,
    capture_forward_corporate_actions,
    check_forward_corporate_action_readiness,
)
from stockdata.full_execution_readiness import check_full_execution_readiness


NOW = datetime(2026, 7, 28, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
FIELDS = ["dividOperateDate", "dividPlanAnnounceDate", "dividCashPsBeforeTax"]


def _cache(tmp_path):
    cache = Cache(tmp_path / "forward.sqlite")
    _bind_cohort(
        cache,
        {
            "symbols": ["000001.SZ", "600000.SH"],
            "start": "2026-07-27",
            "source": "tencent",
            "adjustment_mode": "raw",
            "adjustment_version": "tencent-qt-daily-v1",
        },
    )
    return cache


def _captured(include_second=True):
    batches = {
        "000001.SZ": [
            {
                "year": 2025,
                "fields": FIELDS,
                "rows": [["2026-06-12", "2026-05-20", "0.1"]],
            }
        ]
    }
    if include_second:
        batches["600000.SH"] = [{"year": 2025, "fields": FIELDS, "rows": []}]
    parsed = {
        symbol: [dict(zip(batch["fields"], row)) for batch in symbol_batches for row in batch["rows"]]
        for symbol, symbol_batches in batches.items()
    }
    receipt = {
        "observed_at": NOW.isoformat(timespec="seconds"),
        "source": SOURCE,
        "request": {
            "symbols": sorted(batches),
            "observation_date": "2026-07-28",
            "years": [2025, 2026],
        },
        "response": {"symbols": batches},
    }
    return CapturedCorporateActions(parsed, receipt)


def test_capture_preserves_positive_and_zero_event_completeness(tmp_path):
    cache = _cache(tmp_path)

    result = capture_forward_corporate_actions(
        cache, "2026-07-28", fetcher=lambda _symbols, _day: _captured(), now=NOW
    )

    assert result["event_count"] == 1
    assert result["zero_event_symbols"] == 1
    assert result["decision_available_at"] == NOW.isoformat(timespec="seconds")
    report = check_forward_corporate_action_readiness(
        str(cache.path.resolve()),
        {("000001.SZ", "2026-07-28"), ("600000.SH", "2026-07-28")},
    )
    assert report["ready"] is False
    assert report["integrity_ready"] is True
    assert report["zero_event_rows"] == 1
    assert report["usable_for_execution_export"] is False
    assert {item["code"] for item in report["blockers"]} == {
        "dividend_observation_not_full_corporate_action_ledger",
        "corporate_action_revisions_not_supported",
        "corporate_action_publisher_key_not_enrolled",
    }
    full_report = check_full_execution_readiness(
        cache.path,
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        panel={("000001.SZ", "2026-07-28"), ("600000.SH", "2026-07-28")},
    )
    assert full_report["components"]["corporate_actions"]["integrity_ready"] is True
    assert full_report["components"]["corporate_actions"]["ready"] is False
    with pytest.raises(sqlite3.IntegrityError):
        cache._conn.execute("DELETE FROM forward_corporate_action_coverage")


def test_capture_rejects_incomplete_cohort_without_writes(tmp_path):
    cache = _cache(tmp_path)

    with pytest.raises(ValueError, match="exact cohort"):
        capture_forward_corporate_actions(
            cache,
            "2026-07-28",
            fetcher=lambda _symbols, _day: _captured(include_second=False),
            now=NOW,
        )

    assert cache._conn.execute(
        "SELECT COUNT(*) FROM forward_corporate_action_coverage"
    ).fetchone()[0] == 0


def test_readiness_detects_receipt_tampering(tmp_path):
    cache = _cache(tmp_path)
    capture_forward_corporate_actions(
        cache, "2026-07-28", fetcher=lambda _symbols, _day: _captured(), now=NOW
    )
    cache._conn.execute("DROP TRIGGER collection_receipts_no_update")
    cache._conn.execute(
        "UPDATE collection_receipts SET response_sha256=?",
        (hashlib.sha256(json.dumps({"changed": True}).encode()).hexdigest(),),
    )
    cache._conn.commit()

    report = check_forward_corporate_action_readiness(
        str(cache.path.resolve()), {("000001.SZ", "2026-07-28")}
    )
    assert report["integrity_ready"] is False
    assert any(item["code"] == "invalid_corporate_action_receipts" for item in report["blockers"])


def test_readiness_detects_event_row_tampering(tmp_path):
    cache = _cache(tmp_path)
    capture_forward_corporate_actions(
        cache, "2026-07-28", fetcher=lambda _symbols, _day: _captured(), now=NOW
    )
    cache._conn.execute("DROP TRIGGER forward_corporate_actions_no_update")
    cache._conn.execute(
        "UPDATE forward_corporate_actions SET payload_json='{}' WHERE symbol='000001.SZ'"
    )
    cache._conn.execute(
        """
        CREATE TRIGGER forward_corporate_actions_no_update
        BEFORE UPDATE ON forward_corporate_actions BEGIN
            SELECT RAISE(ABORT, 'corporate actions are append-only');
        END
        """
    )
    cache._conn.commit()

    report = check_forward_corporate_action_readiness(
        str(cache.path.resolve()), {("000001.SZ", "2026-07-28")}
    )
    assert report["integrity_ready"] is False
    assert any(item["code"] == "invalid_corporate_action_receipts" for item in report["blockers"])


def test_capture_rejects_post_close_or_backfill(tmp_path):
    cache = _cache(tmp_path)
    post_close = datetime(2026, 7, 28, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(ValueError, match="pre-open evidence window"):
        capture_forward_corporate_actions(cache, "2026-07-28", now=post_close)
    with pytest.raises(ValueError, match="cannot backfill"):
        capture_forward_corporate_actions(cache, "2026-07-27", now=NOW)


def test_readiness_fails_closed_for_malformed_request(tmp_path):
    cache = _cache(tmp_path)
    capture_forward_corporate_actions(
        cache, "2026-07-28", fetcher=lambda _symbols, _day: _captured(), now=NOW
    )
    cache._conn.execute("DROP TRIGGER collection_receipts_no_update")
    cache._conn.execute(
        "UPDATE collection_receipts SET request_json=?",
        (
            json.dumps(
                {
                    "symbols": ["000001.SZ", "600000.SH"],
                    "observation_date": "2026-07-28",
                    "years": ["bad"],
                }
            ),
        ),
    )
    cache._conn.executescript(
        """
        CREATE TRIGGER collection_receipts_no_update
        BEFORE UPDATE ON collection_receipts BEGIN
            SELECT RAISE(ABORT, 'collection receipts are append-only');
        END;
        """
    )
    cache._conn.commit()

    report = check_forward_corporate_action_readiness(
        str(cache.path.resolve()), {("000001.SZ", "2026-07-28")}
    )
    assert report["integrity_ready"] is False
    assert any(
        item["code"] == "invalid_corporate_action_receipts"
        for item in report["blockers"]
    )
