"""Slice 2: SQLite 缓存层。

存日线 OHLCV，(code, date) 唯一。查区间、增量缺口、离线读取。
缺口语义: 相对已覆盖区间 [min,max] 的左右延伸段（往前/往后扩历史），
停牌造成的中间空洞不视为缺口。
"""
import math
import sqlite3

import pytest

from stockdata.cache import Cache, InvalidBarError


BARS_JAN = [
    {"date": "2024-01-02", "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100},
    {"date": "2024-01-03", "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2, "volume": 200},
    {"date": "2024-01-04", "open": 1.2, "high": 1.4, "low": 1.1, "close": 1.3, "volume": 300},
]


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "test.sqlite")


class TestUpsert:
    def test_insert_and_read_back(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        rows = cache.get_range("600519.SH", "2024-01-01", "2024-01-31")
        assert len(rows) == 3
        assert rows[0]["date"] == "2024-01-02"
        assert rows[0]["close"] == 1.1

    def test_dedup_on_code_date(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        # 重复写同一天，close 更新，不产生重复行
        cache.upsert("600519.SH", [
            {"date": "2024-01-02", "open": 9.0, "high": 9.9, "low": 9.0, "close": 9.5, "volume": 1}
        ])
        rows = cache.get_range("600519.SH", "2024-01-01", "2024-01-31")
        assert len(rows) == 3
        assert rows[0]["close"] == 9.5  # 被覆盖更新

    def test_separate_codes_isolated(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        cache.upsert("000001.SZ", BARS_JAN)
        assert len(cache.get_range("600519.SH", "2024-01-01", "2024-12-31")) == 3
        assert len(cache.get_range("000001.SZ", "2024-01-01", "2024-12-31")) == 3


class TestGetRange:
    def test_ascending_order(self, cache):
        cache.upsert("600519.SH", list(reversed(BARS_JAN)))
        rows = cache.get_range("600519.SH", "2024-01-01", "2024-01-31")
        dates = [r["date"] for r in rows]
        assert dates == sorted(dates)

    def test_range_filters_bounds(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        rows = cache.get_range("600519.SH", "2024-01-03", "2024-01-03")
        assert len(rows) == 1
        assert rows[0]["date"] == "2024-01-03"

    def test_empty_when_no_data(self, cache):
        assert cache.get_range("600519.SH", "2024-01-01", "2024-01-31") == []


class TestCoveredRange:
    def test_none_when_empty(self, cache):
        assert cache.covered_range("600519.SH") is None

    def test_min_max(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        assert cache.covered_range("600519.SH") == ("2024-01-02", "2024-01-04")


class TestMissingGaps:
    def test_all_missing_when_empty(self, cache):
        gaps = cache.missing_gaps("600519.SH", "2024-01-01", "2024-01-31")
        assert gaps == [("2024-01-01", "2024-01-31")]

    def test_no_gap_when_fully_covered(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        # 请求区间落在已覆盖区间内
        gaps = cache.missing_gaps("600519.SH", "2024-01-02", "2024-01-04")
        assert gaps == []

    def test_left_extension(self, cache):
        cache.upsert("600519.SH", BARS_JAN)  # 覆盖 01-02 ~ 01-04
        gaps = cache.missing_gaps("600519.SH", "2023-12-01", "2024-01-04")
        assert gaps == [("2023-12-01", "2024-01-01")]

    def test_right_extension(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        gaps = cache.missing_gaps("600519.SH", "2024-01-02", "2024-06-30")
        assert gaps == [("2024-01-05", "2024-06-30")]

    def test_both_extensions(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        gaps = cache.missing_gaps("600519.SH", "2023-12-01", "2024-06-30")
        assert gaps == [("2023-12-01", "2024-01-01"), ("2024-01-05", "2024-06-30")]


def _seed_calendar(cache, rows):
    cache.refresh_trading_calendar(
        rows[0]["date"], rows[-1]["date"], fetcher=lambda _s, _e: rows
    )


class TestTradingCalendarGaps:
    def test_weekend_and_holiday_not_in_right_gap(self, cache):
        cache.upsert("600519.SH", BARS_JAN)  # 01-02 ~ 01-04
        _seed_calendar(cache, [
            {"date": "2024-01-02", "is_trading_day": True},
            {"date": "2024-01-03", "is_trading_day": True},
            {"date": "2024-01-04", "is_trading_day": True},
            {"date": "2024-01-05", "is_trading_day": False},  # 假假期
            {"date": "2024-01-06", "is_trading_day": False},  # 周六
            {"date": "2024-01-07", "is_trading_day": False},  # 周日
            {"date": "2024-01-08", "is_trading_day": True},
        ])
        gaps = cache.missing_gaps("600519.SH", "2024-01-02", "2024-01-08")
        assert gaps == [("2024-01-08", "2024-01-08")]

    def test_holiday_not_in_left_gap(self, cache):
        # 只覆盖 01-04；01-03 是假期，01-02 是交易日，应被纳入左侧缺口
        cache.upsert("600519.SH", [BARS_JAN[2]])
        _seed_calendar(cache, [
            {"date": "2024-01-01", "is_trading_day": False},  # 假期
            {"date": "2024-01-02", "is_trading_day": True},
            {"date": "2024-01-03", "is_trading_day": False},  # 假期
            {"date": "2024-01-04", "is_trading_day": True},
        ])
        gaps = cache.missing_gaps("600519.SH", "2024-01-01", "2024-01-04")
        assert gaps == [("2024-01-01", "2024-01-02")]

    def test_calendar_missing_is_fail_closed(self, cache):
        cache.upsert("600519.SH", BARS_JAN)
        gaps = cache.missing_gaps("600519.SH", "2024-01-02", "2024-01-08")
        assert gaps == [("2024-01-05", "2024-01-08")]

    def test_all_known_holidays_before_coverage_yield_no_left_gap(self, cache):
        cache.upsert("600519.SH", BARS_JAN)  # starts 01-02
        _seed_calendar(cache, [
            {"date": "2024-01-01", "is_trading_day": False},
        ])
        gaps = cache.missing_gaps("600519.SH", "2024-01-01", "2024-01-04")
        assert gaps == []

    def test_trading_calendar_table_is_created(self, cache):
        tables = {
            row[0] for row in cache._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "trading_calendar" in tables

    def test_refresh_trading_calendar_upserts_rows(self, cache):
        rows = [
            {"date": "2024-01-01", "is_trading_day": False},
            {"date": "2024-01-02", "is_trading_day": True},
        ]
        count = cache.refresh_trading_calendar(
            "2024-01-01", "2024-01-02", fetcher=lambda _s, _e: rows
        )
        assert count == 2
        cal = cache.trading_calendar
        assert cal.is_trading_day("2024-01-02") is True
        assert cal.is_trading_day("2024-01-01") is False
        assert cal.is_trading_day("2024-01-03") is None


class TestPersistenceOffline:
    def test_reopen_same_file_keeps_data(self, tmp_path):
        p = tmp_path / "persist.sqlite"
        c1 = Cache(p)
        c1.upsert("600519.SH", BARS_JAN)
        # 重新打开同一文件（模拟断网后离线读取）
        c2 = Cache(p)
        rows = c2.get_range("600519.SH", "2024-01-01", "2024-01-31")
        assert len(rows) == 3

    def test_migrates_legacy_schema_in_place(self, tmp_path):
        path = tmp_path / "legacy.sqlite"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE daily (code TEXT NOT NULL,date TEXT NOT NULL,"
            "open REAL,high REAL,low REAL,close REAL,volume REAL,source TEXT,"
            "PRIMARY KEY (code,date))"
        )
        connection.execute(
            "INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)",
            ("600519.SH", "2024-01-02", 1, 2, 0.5, 1.5, 100, None),
        )
        connection.commit()
        connection.close()

        migrated = Cache(path)
        rows = migrated.get_range("600519.SH", "2024-01-01", "2024-01-31")
        assert rows[0]["close"] == 1.5
        assert rows[0]["source"] == "legacy_unknown"
        assert rows[0]["adjustment_mode"] == "unknown"
        assert rows[0]["adjustment_version"] == "legacy_unknown"
        assert rows[0]["retrieved_at"]
        assert rows[0]["is_final"] is True
        retrieved_at = rows[0]["retrieved_at"]
        migrated.close()

        reopened = Cache(path)
        rows = reopened.get_range("600519.SH", "2024-01-01", "2024-01-31")
        assert len(rows) == 1
        assert rows[0]["retrieved_at"] == retrieved_at
        assert reopened.schema_version >= 2

    def test_migrates_schema_without_source_column(self, tmp_path):
        path = tmp_path / "oldest.sqlite"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE daily (code TEXT NOT NULL,date TEXT NOT NULL,"
            "open REAL,high REAL,low REAL,close REAL,volume REAL,"
            "PRIMARY KEY (code,date))"
        )
        connection.execute(
            "INSERT INTO daily VALUES (?,?,?,?,?,?,?)",
            ("600519.SH", "2024-01-02", 1, 2, 0.5, 1.5, 100),
        )
        connection.commit()
        connection.close()

        migrated = Cache(path)
        row = migrated.get_range("600519.SH", "2024-01-02", "2024-01-02")[0]
        assert row["source"] == "legacy_unknown"
        assert row["close"] == 1.5


class TestMetadataIsolation:
    def test_source_filter_does_not_return_other_provider(self, cache):
        cache.upsert(
            "600519.SH", [BARS_JAN[0]], source="other",
            adjustment_mode="qfq", adjustment_version="shared-qfq-v1",
        )
        assert cache.get_range(
            "600519.SH", "2024-01-01", "2024-01-31",
            source="baostock", adjustment_mode="qfq",
            adjustment_version="shared-qfq-v1",
        ) == []

    def test_price_variants_coexist_without_overwriting_each_other(self, cache):
        cache.upsert(
            "600519.SH", [BARS_JAN[0]],
            source="baostock", adjustment_mode="qfq",
            adjustment_version="baostock-adjustflag-2",
        )
        raw_bar = {**BARS_JAN[0], "high": 9.9, "close": 9.9}
        cache.upsert(
            "600519.SH", [raw_bar], source="baostock",
            adjustment_mode="raw", adjustment_version="baostock-adjustflag-3",
        )
        qfq = cache.get_range(
            "600519.SH", "2024-01-01", "2024-01-31", source="baostock",
            adjustment_mode="qfq", adjustment_version="baostock-adjustflag-2",
        )
        raw = cache.get_range(
            "600519.SH", "2024-01-01", "2024-01-31", source="baostock",
            adjustment_mode="raw", adjustment_version="baostock-adjustflag-3",
        )
        assert qfq[0]["close"] == 1.1
        assert raw[0]["close"] == 9.9
        with pytest.raises(ValueError, match="multiple price variants"):
            cache.get_range("600519.SH", "2024-01-01", "2024-01-31")

    def test_rejects_bar_metadata_conflicting_with_batch(self, cache):
        bar = dict(BARS_JAN[0])
        bar["adjustment_mode"] = "raw"
        with pytest.raises(ValueError, match="adjustment_mode"):
            cache.upsert("600519.SH", [bar], adjustment_mode="qfq")

    def test_empty_retrieved_at_is_filled(self, cache):
        bar = dict(BARS_JAN[0])
        bar["retrieved_at"] = ""
        cache.upsert("600519.SH", [bar])
        row = cache.get_range("600519.SH", "2024-01-02", "2024-01-02")[0]
        assert row["retrieved_at"]

    def test_capture_receipts_are_append_only_and_hashed(self, cache):
        receipt = {
            "observed_at": "2026-07-27T10:00:00+00:00",
            "source": "baostock",
            "request": {"code": "sh.600519"},
            "response": {"rows": [["2024-01-02", "1.1"]]},
        }
        cache.upsert(
            "600519.SH", [{**BARS_JAN[0], "_capture_receipt": receipt}],
            capture_receipts=[receipt],
        )
        row = cache._conn.execute(
            "SELECT request_json,response_json,response_sha256 FROM collection_receipts"
        ).fetchone()
        assert row["request_json"] == '{"code":"sh.600519"}'
        assert len(row["response_sha256"]) == 64
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            cache._conn.execute("DELETE FROM collection_receipts")

    def test_migration_rebuilds_daily_primary_key_for_variants(self, tmp_path):
        path = tmp_path / "v3.sqlite"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE daily (code TEXT NOT NULL,date TEXT NOT NULL,open REAL,"
            "high REAL,low REAL,close REAL,volume REAL,source TEXT NOT NULL,"
            "adjustment_mode TEXT NOT NULL,adjustment_version TEXT NOT NULL,"
            "retrieved_at TEXT NOT NULL,is_final INTEGER NOT NULL,"
            "PRIMARY KEY (code,date))"
        )
        connection.execute(
            "INSERT INTO daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("600519.SH", "2024-01-02", 1, 2, 0.5, 1.5, 100, "baostock",
             "qfq", "baostock-adjustflag-2", "2024-01-03T00:00:00+00:00", 1),
        )
        connection.commit()
        connection.close()

        cache = Cache(path)
        cache.upsert(
            "600519.SH", [{**BARS_JAN[0], "high": 9.9, "close": 9.9}],
            adjustment_mode="raw", adjustment_version="baostock-adjustflag-3",
        )
        assert cache.schema_version == 5
        assert cache._daily_primary_key() == (
            "code", "date", "source", "adjustment_mode", "adjustment_version"
        )
        assert cache.get_range(
            "600519.SH", "2024-01-02", "2024-01-02", source="baostock",
            adjustment_mode="qfq",
            adjustment_version="baostock-adjustflag-2",
        )[0]["close"] == 1.5

    def test_coverage_requires_full_identity_when_variants_exist(self, cache):
        cache.upsert("600519.SH", [BARS_JAN[0]], adjustment_mode="qfq")
        cache.upsert("600519.SH", [BARS_JAN[0]], adjustment_mode="raw")
        with pytest.raises(ValueError, match="multiple price variants"):
            cache.covered_range("600519.SH")
        with pytest.raises(ValueError, match="provided together"):
            cache.covered_range("600519.SH", adjustment_mode="qfq")


class TestBarValidation:
    def _bar(self, **overrides):
        base = {
            "date": "2024-01-02",
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": 1.1,
            "volume": 100,
        }
        base.update(overrides)
        return base

    def test_accepts_valid_bar(self, cache):
        cache.upsert("600519.SH", [self._bar()])
        assert len(cache.get_range("600519.SH", "2024-01-02", "2024-01-02")) == 1

    def test_rejects_invalid_date(self, cache):
        with pytest.raises(InvalidBarError, match="date"):
            cache.upsert("600519.SH", [self._bar(date="2024-13-40")])

    @pytest.mark.parametrize("field", ["open", "high", "low", "close"])
    def test_rejects_non_positive_or_non_finite_price(self, cache, field):
        with pytest.raises(InvalidBarError, match=field):
            cache.upsert("600519.SH", [self._bar(**{field: 0.0})])
        with pytest.raises(InvalidBarError, match=field):
            cache.upsert("600519.SH", [self._bar(**{field: float("nan")})])
        with pytest.raises(InvalidBarError, match=field):
            cache.upsert("600519.SH", [self._bar(**{field: float("inf")})])

    def test_rejects_high_lower_than_low(self, cache):
        with pytest.raises(InvalidBarError, match="high must be >= low"):
            cache.upsert("600519.SH", [self._bar(high=0.8, low=1.0)])

    def test_rejects_high_lower_than_open_or_close(self, cache):
        with pytest.raises(InvalidBarError, match="high must be >= open and close"):
            cache.upsert("600519.SH", [self._bar(high=1.0, open=1.05)])

    def test_rejects_low_higher_than_open_or_close(self, cache):
        with pytest.raises(InvalidBarError, match="low must be <= open and close"):
            cache.upsert("600519.SH", [self._bar(low=1.0, close=0.95)])

    def test_rejects_negative_volume(self, cache):
        with pytest.raises(InvalidBarError, match="volume"):
            cache.upsert("600519.SH", [self._bar(volume=-1)])

    def test_rejects_non_finite_volume(self, cache):
        with pytest.raises(InvalidBarError, match="volume"):
            cache.upsert("600519.SH", [self._bar(volume=float("inf"))])

    def test_rejects_entire_batch_if_any_bar_invalid(self, cache):
        with pytest.raises(InvalidBarError):
            cache.upsert("600519.SH", [self._bar(), self._bar(date="2024-01-03", open=0)])
        assert cache.get_range("600519.SH", "2024-01-01", "2024-01-31") == []

    def test_error_includes_code_date_and_reason(self, cache):
        with pytest.raises(InvalidBarError) as exc_info:
            cache.upsert("600519.SH", [self._bar(open=float("nan"))])
        assert exc_info.value.code == "600519.SH"
        assert exc_info.value.bar_date == "2024-01-02"
        assert "open" in exc_info.value.reason


class TestBusyTimeout:
    def test_non_collector_cache_sets_busy_timeout(self, tmp_path):
        cache = Cache(tmp_path / "busy.sqlite")
        timeout = cache._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 30_000

    def test_busy_timeout_applies_to_concurrent_writer(self, tmp_path):
        path = tmp_path / "concurrent.sqlite"
        cache1 = Cache(path)
        cache2 = Cache(path)
        cache1.upsert("600519.SH", BARS_JAN)
        # 第二个连接应能等待而不是立即 OperationalError
        cache2.upsert("000001.SZ", BARS_JAN)
        assert len(cache1.get_range("600519.SH", "2024-01-01", "2024-01-31")) == 3
        assert len(cache2.get_range("000001.SZ", "2024-01-01", "2024-01-31")) == 3
