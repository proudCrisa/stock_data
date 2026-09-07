import hashlib
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stockdata.cache import Cache
from stockdata.forward_capture import _bind_cohort
from stockdata.forward_context import (
    SOURCE,
    CapturedMarketRows,
    capture_forward_context,
    check_forward_context_readiness,
)


NOW = datetime(2026, 7, 27, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _row(symbol, name="Example", trade="10.0", volume=1000):
    return {"symbol": symbol, "name": name, "trade": trade, "volume": volume}


def _captured(rows, observed_at=None):
    receipt = {
        "observed_at": (observed_at or NOW).isoformat(timespec="seconds"),
        "source": SOURCE,
        "request": {"node": "hs_a"},
        "response": {
            "advertised_count": len(rows),
            "rows": rows,
            "raw_pages": [json.dumps(rows)],
        },
    }
    return CapturedMarketRows(rows, receipt)


def test_preopen_capture_is_decision_available(tmp_path):
    cache = _cache(tmp_path)
    preopen = datetime(2026, 7, 27, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    rows = [_row("sz000001"), _row("sh600000")]

    result = capture_forward_context(
        cache,
        "2026-07-27",
        fetcher=lambda: _captured(rows, preopen),
        now=preopen,
    )

    assert result["capture_phase"] == "pre_open"
    assert result["decision_available_at"] == preopen.isoformat(timespec="seconds")
    report = check_forward_context_readiness(
        str(cache.path.resolve()),
        {("000001.SZ", "2026-07-27"), ("600000.SH", "2026-07-27")},
    )
    assert report["ready"] is False
    assert report["decision_rows"] == 2
    assert report["finalized_rows"] == 0
    assert report["blockers"][0]["code"] == "missing_finalized_context_rows"

    post_close = capture_forward_context(
        cache,
        "2026-07-27",
        fetcher=lambda: _captured(rows),
        now=NOW,
    )
    assert post_close["capture_phase"] == "post_close"
    assert post_close["outcome_observed_at"] == NOW.isoformat(timespec="seconds")
    assert post_close["finalized_at"] == NOW.isoformat(timespec="seconds")
    complete = check_forward_context_readiness(
        str(cache.path.resolve()),
        {("000001.SZ", "2026-07-27"), ("600000.SH", "2026-07-27")},
    )
    assert complete["ready"] is False
    assert complete["integrity_ready"] is True
    assert complete["blockers"] == [
        {"code": "signed_session_calendar_not_enrolled", "count": 1}
    ]


def test_capture_rejects_intraday_window(tmp_path):
    cache = _cache(tmp_path)
    intraday = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(ValueError, match="allowed evidence window"):
        capture_forward_context(cache, "2026-07-27", now=intraday)


def test_capture_rejects_weekend_even_inside_preopen_window(tmp_path):
    cache = _cache(tmp_path)
    saturday = datetime(2026, 8, 1, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(ValueError, match="weekday session candidate"):
        capture_forward_context(cache, "2026-08-01", now=saturday)


def test_capture_rejects_holiday_when_calendar_covers_it(tmp_path):
    cache = _cache(tmp_path)
    cache.refresh_trading_calendar(
        "2026-08-05", "2026-08-05",
        fetcher=lambda _s, _e: [{"date": "2026-08-05", "is_trading_day": False}],
    )
    post_close = datetime(2026, 8, 5, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(ValueError, match="trading day session"):
        capture_forward_context(cache, "2026-08-05", now=post_close)


def test_readiness_rejects_consistent_but_out_of_window_phase_timestamp(tmp_path):
    cache = _cache(tmp_path)
    preopen = datetime(2026, 7, 27, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    rows = [_row("sz000001"), _row("sh600000")]
    capture_forward_context(
        cache, "2026-07-27", fetcher=lambda: _captured(rows, preopen), now=preopen
    )
    invalid = "2026-07-27T10:00:00+08:00"
    cache._conn.execute("DROP TRIGGER collection_receipts_no_update")
    cache._conn.execute("DROP TRIGGER forward_context_observations_no_update")
    cache._conn.execute("UPDATE collection_receipts SET observed_at=?", (invalid,))
    cache._conn.execute(
        "UPDATE forward_context_observations SET decision_available_at=? "
        "WHERE observation_phase='pre_open'",
        (invalid,),
    )
    cache._conn.executescript(
        """
        CREATE TRIGGER collection_receipts_no_update
        BEFORE UPDATE ON collection_receipts BEGIN
            SELECT RAISE(ABORT, 'collection receipts are append-only');
        END;
        CREATE TRIGGER forward_context_observations_no_update
        BEFORE UPDATE ON forward_context_observations BEGIN
            SELECT RAISE(ABORT, 'forward context observations are append-only');
        END;
        """
    )
    cache._conn.commit()

    report = check_forward_context_readiness(
        str(cache.path.resolve()),
        {("000001.SZ", "2026-07-27"), ("600000.SH", "2026-07-27")},
    )
    assert report["integrity_ready"] is False
    assert any(item["code"] == "invalid_context_receipts" for item in report["blockers"])


def _cache(tmp_path):
    cache = Cache(tmp_path / "forward.sqlite")
    _bind_cohort(cache, {
        "symbols": ["000001.SZ", "600000.SH"],
        "start": "2026-07-27",
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
    })
    return cache


def test_capture_is_append_only_complete_and_idempotent(tmp_path):
    cache = _cache(tmp_path)
    rows = [_row("sz000001", "平安银行"), _row("sh600000", "浦发银行")]

    first = capture_forward_context(
        cache, "2026-07-27", fetcher=lambda: _captured(rows), now=NOW
    )
    second = capture_forward_context(
        cache, "2026-07-27", fetcher=lambda: pytest.fail("must not refetch"), now=NOW
    )

    assert first["captured"] is True
    assert second["captured"] is False
    assert cache._conn.execute("SELECT COUNT(*) FROM forward_universe_observations").fetchone()[0] == 2
    assert cache._conn.execute("SELECT COUNT(*) FROM forward_status_observations").fetchone()[0] == 2
    assert cache._conn.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        cache._conn.execute("DELETE FROM forward_status_observations")


def test_capture_rejects_backfill_and_missing_cohort_without_partial_writes(tmp_path):
    cache = _cache(tmp_path)
    with pytest.raises(ValueError, match="cannot backfill"):
        capture_forward_context(cache, "2026-07-26", now=NOW)
    with pytest.raises(ValueError, match="misses cohort"):
        capture_forward_context(
            cache,
            "2026-07-27",
            fetcher=lambda: _captured([_row("sz000001")]),
            now=NOW,
        )
    assert cache._conn.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0] == 0


def test_context_readiness_detects_receipt_tampering(tmp_path):
    cache = _cache(tmp_path)
    rows = [_row("sz000001"), _row("sh600000", "*ST Test", trade=0, volume=0)]
    capture_forward_context(
        cache, "2026-07-27", fetcher=lambda: _captured(rows), now=NOW
    )
    database = str(cache.path.resolve())
    panel = {("000001.SZ", "2026-07-27"), ("600000.SH", "2026-07-27")}
    initial = check_forward_context_readiness(database, panel)
    assert initial["ready"] is False
    assert initial["integrity_ready"] is True
    assert initial["blockers"][0]["code"] == "missing_decision_context_rows"

    cache._conn.execute("DROP TRIGGER collection_receipts_no_update")
    cache._conn.execute(
        "UPDATE collection_receipts SET response_sha256=?",
        (hashlib.sha256(b"changed").hexdigest(),),
    )
    cache._conn.commit()
    assert check_forward_context_readiness(database, panel)["ready"] is False


def test_context_readiness_detects_status_or_trigger_tampering(tmp_path):
    cache = _cache(tmp_path)
    rows = [_row("sz000001"), _row("sh600000")]
    capture_forward_context(
        cache, "2026-07-27", fetcher=lambda: _captured(rows), now=NOW
    )
    database = str(cache.path.resolve())
    panel = {("000001.SZ", "2026-07-27"), ("600000.SH", "2026-07-27")}

    cache._conn.execute("DROP TRIGGER forward_status_observations_no_update")
    cache._conn.execute(
        "UPDATE forward_status_observations SET is_st=1 WHERE symbol='000001.SZ'"
    )
    cache._conn.execute(
        "CREATE TRIGGER forward_status_observations_no_update "
        "BEFORE UPDATE ON forward_status_observations BEGIN SELECT 1; END"
    )
    cache._conn.commit()

    report = check_forward_context_readiness(database, panel)
    assert report["ready"] is False
    assert {item["code"] for item in report["blockers"]} == {
        "invalid_context_append_only_triggers",
        "invalid_context_receipts",
        "missing_decision_context_rows",
        "signed_session_calendar_not_enrolled",
    }


def test_capture_rejects_rows_that_do_not_come_from_raw_pages(tmp_path):
    cache = _cache(tmp_path)
    rows = [_row("sz000001"), _row("sh600000")]
    captured = _captured(rows)
    captured.capture_receipt["response"]["raw_pages"] = [
        json.dumps([_row("sz000001"), _row("sh600000", name="Changed")])
    ]

    with pytest.raises(ValueError, match="do not match raw pages"):
        capture_forward_context(
            cache, "2026-07-27", fetcher=lambda: captured, now=NOW
        )


def test_capture_rejects_future_observed_at(tmp_path):
    cache = _cache(tmp_path)
    rows = [_row("sz000001"), _row("sh600000")]
    captured = _captured(rows)
    captured.capture_receipt["observed_at"] = "2026-07-27T16:31:00+08:00"

    with pytest.raises(ValueError, match="future-dated"):
        capture_forward_context(
            cache, "2026-07-27", fetcher=lambda: captured, now=NOW
        )
