from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import stockdata.sync as sync_module
from stockdata.cache import Cache
from stockdata.fetch_baostock import CapturedBars
from stockdata.fetch_tencent import CapturedTencentBars
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


def _tencent_capture(
    code, day, observed_at="2024-01-03T16:00:00+08:00", start_date="2024-01-01"
):
    market = code.split(".")[1].lower()
    number = code.split(".")[0]
    parts = [""] * 45
    parts[1] = "Example"
    parts[3] = "10.0"
    parts[5] = "9.5"
    parts[6] = "100"
    parts[30] = day.replace("-", "") + "160000"
    parts[33] = "10.5"
    parts[34] = "9.0"
    raw = f'v_{market}{number}="{"~".join(parts)}";'
    response = {
        "raw": raw,
        "fields": "date,open,high,low,close,volume",
        "rows": [[day, "9.5", "10.5", "9.0", "10.0", "10000.0"]],
    }
    receipt = {
        "observed_at": observed_at,
        "source": "tencent",
        "request": {
            "method": "qt",
            "url": f"https://qt.gtimg.cn/q={market}{number}",
            "start_date": start_date,
            "end_date": day,
        },
        "response": response,
    }
    bar = {
        "date": day,
        "open": 9.5,
        "high": 10.5,
        "low": 9.0,
        "close": 10.0,
        "volume": 10000.0,
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
        "retrieved_at": observed_at,
        "is_final": True,
        "_capture_receipt": receipt,
    }
    return CapturedTencentBars([bar], receipt)


def _enable_collector_tencent_branch(monkeypatch):
    monkeypatch.setattr(sync_module, "_is_collector_tencent_price_sync", lambda *args: True)
    monkeypatch.setattr(sync_module, "default_final_date", lambda: "2024-01-03")
    monkeypatch.setattr(sync_module, "latest_finalized_date", lambda: "2024-01-03")


def _insert_coverage(cache, code, start, end, retrieved_at="2024-01-02T16:00:00+08:00"):
    cache._conn.execute(
        "INSERT INTO sync_coverage VALUES (?,?,?,?,?,?,?)",
        (code, "tencent", "raw", "tencent-qt-daily-v1", start, end, retrieved_at),
    )
    cache._conn.commit()


def _persisted_collector_rows(cache):
    return {
        "daily": tuple(
            tuple(row)
            for row in cache._conn.execute(
                "SELECT code,date,open,high,low,close,volume,source,"
                "adjustment_mode,adjustment_version,retrieved_at,is_final,receipt_id "
                "FROM daily ORDER BY code,date,source,adjustment_mode,adjustment_version"
            )
        ),
        "receipts": tuple(
            tuple(row)
            for row in cache._conn.execute(
                "SELECT receipt_id,observed_at,source,request_json,response_json,"
                "response_sha256,created_at FROM collection_receipts ORDER BY receipt_id"
            )
        ),
        "coverage": tuple(
            tuple(row)
            for row in cache._conn.execute(
                "SELECT code,source,adjustment_mode,adjustment_version,start_date,"
                "end_date,retrieved_at FROM sync_coverage ORDER BY code,source,"
                "adjustment_mode,adjustment_version"
            )
        ),
    }


def _seed_tencent_current(cache, code="600519.SH", start_date="2024-01-01"):
    captured = _tencent_capture(code, "2024-01-03", start_date=start_date)
    cache.upsert(
        code,
        list(captured),
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        is_final=True,
        capture_receipts=[captured.capture_receipt],
    )


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


def test_sync_no_data_range_is_not_marked_as_covered(tmp_path):
    # fail-closed：历史区间无有效 bar 时不扩展 coverage，停牌/空响应会每次重试。
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
    assert second["symbols"][0]["status"] == "no_data"
    assert calls == [
        ("600519.SH", "2024-01-06", "2024-01-07"),
        ("600519.SH", "2024-01-06", "2024-01-07"),
    ]


def test_partial_dropped_rows_reject_batch_and_record_error(tmp_path):
    # fail-closed：部分有效行 + 部分坏行时整段拒收，不推进 coverage。
    cache = Cache(tmp_path / "dropped.sqlite")
    receipt = {
        "observed_at": "2024-01-02T16:00:00+08:00",
        "source": "baostock",
        "request": {"code": "sh.600519"},
        "response": {"fields": "date", "rows": []},
    }

    def fetch(code, start, end):
        return CapturedBars(
            [_bar("2024-01-02", 1.0)], receipt, dropped=1
        )

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-02",
        "2024-01-03",
        fetcher=fetch,
    )

    assert result["errors"] == 1
    assert result["symbols"][0]["status"] == "error"
    assert "dropped" in result["symbols"][0]["error"].lower()
    assert cache.get_range("600519.SH", "2024-01-02", "2024-01-03") == []
    assert cache.sync_coverage(
        "600519.SH", "baostock", "qfq", "baostock-adjustflag-2"
    ) is None


def test_no_data_does_not_record_coverage(tmp_path):
    cache = Cache(tmp_path / "no-coverage.sqlite")

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-06",
        "2024-01-07",
        fetcher=lambda code, start, end: [],
    )

    assert result["symbols"][0]["status"] == "no_data"
    coverage = cache.sync_coverage(
        "600519.SH", "baostock", "qfq", "baostock-adjustflag-2"
    )
    assert coverage is None


def test_valid_bars_record_coverage(tmp_path):
    cache = Cache(tmp_path / "with-coverage.sqlite")

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-02",
        "2024-01-03",
        fetcher=lambda code, start, end: [_bar("2024-01-02", 1.0), _bar("2024-01-03", 1.1)],
    )

    assert result["symbols"][0]["status"] == "synced"
    coverage = cache.sync_coverage(
        "600519.SH", "baostock", "qfq", "baostock-adjustflag-2"
    )
    assert coverage == ("2024-01-02", "2024-01-03")


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


def test_collector_tencent_daily_skips_fetch_and_repairs_coverage_without_retimestamp(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-gap.sqlite")
    _seed_tencent_current(cache, start_date="2024-01-03")
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-02")
    old_receipts = cache._conn.execute(
        "SELECT COUNT(*) FROM collection_receipts"
    ).fetchone()[0]
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    coverage = cache._conn.execute(
        "SELECT start_date,end_date,retrieved_at FROM sync_coverage"
    ).fetchone()
    assert result["symbols"][0]["status"] == "up_to_date"
    assert calls == []
    assert cache._conn.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0] == old_receipts
    assert tuple(coverage) == (
        "2024-01-01",
        "2024-01-03",
        "2024-01-02T16:00:00+08:00",
    )


def test_collector_tencent_first_coverage_commit_replays_bound_current_daily(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-first-coverage.sqlite")
    _seed_tencent_current(cache, start_date="2024-01-01")
    before = _persisted_collector_rows(cache)
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    after = _persisted_collector_rows(cache)
    assert result["symbols"][0]["status"] == "up_to_date"
    assert calls == []
    assert after["daily"] == before["daily"]
    assert after["receipts"] == before["receipts"]
    assert after["coverage"][0][:6] == (
        "600519.SH",
        "tencent",
        "raw",
        "tencent-qt-daily-v1",
        "2024-01-01",
        "2024-01-03",
    )


@pytest.mark.parametrize("receipt_start", ["2024-01-01", "2024-01-03"])
def test_collector_tencent_new_receipt_start_must_equal_real_fetch_start(
    tmp_path, monkeypatch, receipt_start
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / f"collector-fetch-start-{receipt_start}.sqlite")
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-01")
    before = _persisted_collector_rows(cache)
    calls = []

    def fetch(code, start, end):
        calls.append((code, start, end))
        return _tencent_capture(code, end, start_date=receipt_start)

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=fetch,
    )

    assert result["errors"] == 1
    assert calls == [("600519.SH", "2024-01-02", "2024-01-03")]
    assert _persisted_collector_rows(cache) == before


def test_collector_tencent_gap_receipt_coverage_only_replay_is_byte_stable(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-gap-replay.sqlite")
    _seed_tencent_current(cache, start_date="2024-01-03")
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-02")
    before = _persisted_collector_rows(cache)
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    after = _persisted_collector_rows(cache)
    assert result["symbols"][0]["status"] == "up_to_date"
    assert calls == []
    assert before["daily"] == after["daily"]
    assert before["receipts"] == after["receipts"]
    assert before["coverage"][0][:-2] == after["coverage"][0][:-2]
    assert before["coverage"][0][-1] == after["coverage"][0][-1]
    assert after["coverage"][0][5] == "2024-01-03"


def test_collector_tencent_multi_day_gap_replays_one_exact_receipt_without_fetch(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-multi-day-gap.sqlite")
    captured = _tencent_capture(
        "600519.SH", "2024-01-03", start_date="2024-01-02"
    )
    cache.upsert(
        "600519.SH",
        list(captured),
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        is_final=True,
        capture_receipts=[captured.capture_receipt],
    )
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-01")
    before = _persisted_collector_rows(cache)
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    after = _persisted_collector_rows(cache)
    assert result["symbols"][0]["status"] == "up_to_date"
    assert calls == []
    assert after["daily"] == before["daily"]
    assert after["receipts"] == before["receipts"]
    assert after["coverage"] == (
        ("600519.SH", "tencent", "raw", "tencent-qt-daily-v1", "2024-01-01", "2024-01-03", before["coverage"][0][6]),
    )


@pytest.mark.parametrize("receipt_start", ["2024-01-01", "2024-01-03"])
def test_collector_tencent_multi_day_gap_with_wrong_receipt_is_rejected_without_fetch(
    tmp_path, monkeypatch, receipt_start
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / f"collector-multi-day-wrong-gap-{receipt_start}.sqlite")
    captured = _tencent_capture(
        "600519.SH", "2024-01-03", start_date=receipt_start
    )
    cache.upsert(
        "600519.SH",
        list(captured),
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        is_final=True,
        capture_receipts=[captured.capture_receipt],
    )
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-01")
    before = _persisted_collector_rows(cache)
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    assert result["errors"] == 1
    assert calls == []
    assert _persisted_collector_rows(cache) == before


@pytest.mark.parametrize("wrong_field", ["start_date", "url"])
def test_collector_tencent_wrong_gap_receipt_is_rejected_without_fetch(
    tmp_path, monkeypatch, wrong_field
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / f"collector-wrong-gap-{wrong_field}.sqlite")
    captured = _tencent_capture(
        "600519.SH", "2024-01-03", start_date="2024-01-03"
    )
    if wrong_field == "start_date":
        captured.capture_receipt["request"][wrong_field] = "2024-01-02"
    else:
        captured.capture_receipt["request"][wrong_field] = "https://qt.gtimg.cn/q=sh000002"
    cache.upsert(
        "600519.SH",
        list(captured),
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        is_final=True,
        capture_receipts=[captured.capture_receipt],
    )
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-02")
    before = _persisted_collector_rows(cache)
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    assert result["errors"] == 1
    assert calls == []
    assert _persisted_collector_rows(cache) == before


def test_collector_tencent_writer_rejects_trailing_junk_before_any_write(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-envelope.sqlite")
    captured = _tencent_capture(
        "600519.SH", "2024-01-03", start_date="2024-01-03"
    )
    captured.capture_receipt["response"]["raw"] += "TRAILING-JUNK"
    cache.upsert(
        "600519.SH",
        list(captured),
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        is_final=True,
        capture_receipts=[captured.capture_receipt],
    )
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-02")
    before = _persisted_collector_rows(cache)
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    assert result["errors"] == 1
    assert calls == []
    assert _persisted_collector_rows(cache) == before


def test_collector_tencent_empty_response_with_receipt_writes_no_orphan_or_coverage(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-empty.sqlite")
    empty = _tencent_capture("600519.SH", "2024-01-03")
    empty.clear()
    empty.capture_receipt["response"] = {
        "raw": "",
        "fields": "date,open,high,low,close,volume",
        "rows": [],
    }

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-03",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: empty,
    )

    assert result["symbols"][0]["status"] == "no_data"
    assert cache._conn.execute("SELECT COUNT(*) FROM collection_receipts").fetchone()[0] == 0
    assert cache._conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 0
    assert cache._conn.execute("SELECT COUNT(*) FROM sync_coverage").fetchone()[0] == 0


def test_collector_tencent_missing_current_daily_fetches_even_when_coverage_is_complete(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-missing-daily.sqlite")
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-03")
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or _tencent_capture(
            code, "2024-01-03", start_date="2024-01-03"
        ),
    )

    assert result["symbols"][0]["status"] == "synced"
    assert calls == [("600519.SH", "2024-01-03", "2024-01-03")]
    assert cache._conn.execute(
        "SELECT COUNT(*) FROM daily WHERE code='600519.SH' AND date='2024-01-03'"
    ).fetchone()[0] == 1


def test_collector_tencent_foreign_existing_identity_fails_closed_without_fetch(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-foreign.sqlite")
    captured = _tencent_capture("600519.SH", "2024-01-03")
    foreign_bars = [
        {**bar, "adjustment_version": "foreign-v1"} for bar in captured
    ]
    cache.upsert(
        "600519.SH",
        foreign_bars,
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="foreign-v1",
        is_final=True,
        capture_receipts=[captured.capture_receipt],
    )
    calls = []
    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-03",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    assert result["errors"] == 1
    assert calls == []


def test_collector_tencent_exact_coverage_retry_preserves_timestamp(
    tmp_path, monkeypatch
):
    _enable_collector_tencent_branch(monkeypatch)
    cache = Cache(tmp_path / "collector-exact.sqlite")
    _seed_tencent_current(cache)
    _insert_coverage(cache, "600519.SH", "2024-01-01", "2024-01-03")
    calls = []

    result = sync_symbols(
        cache,
        ["600519.SH"],
        "2024-01-01",
        "2024-01-03",
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        fetcher=lambda code, start, end: calls.append((code, start, end)) or [],
    )

    retrieved_at = cache._conn.execute(
        "SELECT retrieved_at FROM sync_coverage"
    ).fetchone()[0]
    assert result["symbols"][0]["status"] == "up_to_date"
    assert calls == []
    assert retrieved_at == "2024-01-02T16:00:00+08:00"
