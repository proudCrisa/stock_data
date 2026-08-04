from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from stockdata.cache import Cache
from stockdata.fetch_baostock import CapturedBars
from stockdata.sync import default_final_date, sync_symbols


def _bar(day, close):
    return {
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100,
    }


def test_sync_is_resumable_and_idempotent(tmp_path):
    cache = Cache(tmp_path / "sync.sqlite")
    calls = []
    fail_second = {"value": True}

    def fetch(code, start, end):
        calls.append((code, start, end))
        if code == "000001.SZ" and fail_second["value"]:
            raise RuntimeError("temporary")
        return [_bar("2024-01-02", 1.0), _bar("2024-01-03", 1.1)]

    first = sync_symbols(
        cache, ["600519.SH", "000001.SZ"], "2024-01-01", "2024-01-03",
        fetcher=fetch,
    )
    assert first["synced"] == 1
    assert first["errors"] == 1

    fail_second["value"] = False
    calls.clear()
    second = sync_symbols(
        cache, ["600519.SH", "000001.SZ"], "2024-01-01", "2024-01-03",
        fetcher=fetch,
    )
    assert second["up_to_date"] == 1
    assert second["synced"] == 1
    assert calls == [("000001.SZ", "2024-01-01", "2024-01-03")]

    calls.clear()
    third = sync_symbols(
        cache, ["600519.SH", "000001.SZ"], "2024-01-01", "2024-01-03",
        fetcher=fetch,
    )
    assert third["up_to_date"] == 2
    assert calls == []
    rows = cache.get_range("600519.SH", "2024-01-01", "2024-01-03")
    assert len(rows) == 2
    assert rows[0]["source"] == "baostock"
    assert rows[0]["adjustment_mode"] == "qfq"
    assert rows[0]["adjustment_version"] == "baostock-adjustflag-2"
    assert rows[0]["retrieved_at"]
    assert rows[0]["is_final"] is True


def test_cache_keeps_raw_and_qfq_syncs_separate(tmp_path):
    cache = Cache(tmp_path / "mixed.sqlite")
    cache.upsert("600519.SH", [_bar("2024-01-02", 1.0)], adjustment_mode="qfq")
    cache.upsert("600519.SH", [_bar("2024-01-02", 10.0)], adjustment_mode="raw")
    assert cache.get_range(
        "600519.SH", "2024-01-02", "2024-01-02", source="baostock",
        adjustment_mode="qfq",
        adjustment_version="baostock-adjustflag-2",
    )[0]["close"] == 1.0
    assert cache.get_range(
        "600519.SH", "2024-01-02", "2024-01-02", source="baostock",
        adjustment_mode="raw",
        adjustment_version="baostock-adjustflag-3",
    )[0]["close"] == 10.0


def test_sync_persists_a_source_response_receipt_even_when_empty(tmp_path):
    cache = Cache(tmp_path / "receipt.sqlite")
    receipt = {
        "observed_at": "2026-07-27T10:00:00+00:00",
        "source": "baostock",
        "request": {"code": "sh.600519"},
        "response": {"fields": "date", "rows": []},
    }
    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-06",
        "2024-01-07",
        fetcher=lambda code, start, end: CapturedBars([], receipt),
    )
    assert result["symbols"][0]["status"] == "no_data"
    stored = cache._conn.execute(
        "SELECT observed_at,source,response_json FROM collection_receipts"
    ).fetchone()
    assert dict(stored) == {
        "observed_at": "2026-07-27T10:00:00+00:00",
        "source": "baostock",
        "response_json": '{"fields":"date","rows":[]}',
    }


def test_sync_no_data_range_is_idempotent(tmp_path):
    cache = Cache(tmp_path / "empty.sqlite")
    calls = []

    def fetch(code, start, end):
        calls.append((code, start, end))
        return []

    first = sync_symbols(
        cache, ["600519.SH"], "2024-01-06", "2024-01-07", fetcher=fetch
    )
    second = sync_symbols(
        cache, ["600519.SH"], "2024-01-06", "2024-01-07", fetcher=fetch
    )
    assert first["symbols"][0]["status"] == "no_data"
    assert second["symbols"][0]["status"] == "up_to_date"
    assert calls == [("600519.SH", "2024-01-06", "2024-01-07")]


def test_sync_retries_latest_cutoff_when_source_is_temporarily_empty(
    tmp_path, monkeypatch
):
    cache = Cache(tmp_path / "retry-latest.sqlite")
    calls = []
    responses = [[], [_bar("2024-01-03", 1.1)]]
    monkeypatch.setattr("stockdata.sync.default_final_date", lambda: "2024-01-03")
    monkeypatch.setattr("stockdata.sync.latest_finalized_date", lambda: "2024-01-03")

    def fetch(code, start, end):
        calls.append((code, start, end))
        return responses.pop(0)

    first = sync_symbols(
        cache, ["600519.SH"], "2024-01-03", "2024-01-03", fetcher=fetch
    )
    second = sync_symbols(
        cache, ["600519.SH"], "2024-01-03", "2024-01-03", fetcher=fetch
    )

    assert first["symbols"][0]["status"] == "no_data"
    assert second["symbols"][0]["status"] == "synced"
    assert calls == [
        ("600519.SH", "2024-01-03", "2024-01-03"),
        ("600519.SH", "2024-01-03", "2024-01-03"),
    ]


def test_sync_rejects_fetcher_metadata_mismatch(tmp_path):
    cache = Cache(tmp_path / "mismatch.sqlite")
    bar = _bar("2024-01-02", 1.0)
    bar.update({
        "source": "other",
        "adjustment_mode": "raw",
        "adjustment_version": "other-raw-v1",
    })
    result = sync_symbols(
        cache, ["600519.SH"], "2024-01-01", "2024-01-03",
        fetcher=lambda code, start, end: [bar],
    )
    assert result["errors"] == 1
    assert cache.get_range("600519.SH", "2024-01-01", "2024-01-03") == []


def test_default_final_date_allows_today_only_after_shanghai_close():
    shanghai = ZoneInfo("Asia/Shanghai")
    assert default_final_date(
        datetime(2026, 7, 22, 15, 59, tzinfo=shanghai)
    ) == "2026-07-21"
    assert default_final_date(
        datetime(2026, 7, 22, 16, 0, tzinfo=shanghai)
    ) == "2026-07-22"
    assert default_final_date(
        datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
    ) == "2026-07-22"
