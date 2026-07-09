"""Slice 2: SQLite 缓存层。

存日线 OHLCV，(code, date) 唯一。查区间、增量缺口、离线读取。
缺口语义: 相对已覆盖区间 [min,max] 的左右延伸段（往前/往后扩历史），
停牌造成的中间空洞不视为缺口。
"""
import pytest

from stockdata.cache import Cache


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
            {"date": "2024-01-02", "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.9, "volume": 1}
        ])
        rows = cache.get_range("600519.SH", "2024-01-01", "2024-01-31")
        assert len(rows) == 3
        assert rows[0]["close"] == 9.9  # 被覆盖更新

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


class TestPersistenceOffline:
    def test_reopen_same_file_keeps_data(self, tmp_path):
        p = tmp_path / "persist.sqlite"
        c1 = Cache(p)
        c1.upsert("600519.SH", BARS_JAN)
        # 重新打开同一文件（模拟断网后离线读取）
        c2 = Cache(p)
        rows = c2.get_range("600519.SH", "2024-01-01", "2024-01-31")
        assert len(rows) == 3
