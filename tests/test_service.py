"""Slice 6: HistoryService 编排（多源 + 缓存 + 增量）。

用依赖注入的 fake fetcher 测真实编排逻辑（查缓存/算缺口/写回/兜底/今日bar），
不打网络。fake 只是替身数据源，断言针对 service 的编排决策而非 fake 本身。
"""
import pytest

from stockdata.cache import Cache
from stockdata.service import HistoryService


BARS_H1 = [
    {"date": "2024-01-02", "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100},
    {"date": "2024-01-03", "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2, "volume": 200},
    {"date": "2024-01-04", "open": 1.2, "high": 1.4, "low": 1.1, "close": 1.3, "volume": 300},
]


class RecordingPrimary:
    """记录被请求的区间；返回预设 bars（按请求区间过滤）。"""
    def __init__(self, bars, fail=False):
        self.bars = bars
        self.calls = []
        self.fail = fail

    def __call__(self, code, start, end):
        self.calls.append((code, start, end))
        if self.fail:
            raise RuntimeError("primary 挂了")
        return [b for b in self.bars if start <= b["date"] <= end]


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "svc.sqlite")


class TestCacheHit:
    def test_no_fetch_when_fully_cached(self, cache):
        cache.upsert("600519.SH", BARS_H1)
        primary = RecordingPrimary(BARS_H1)
        svc = HistoryService(cache, primary_fetch=primary)
        rows = svc.get_history("600519.SH", "2024-01-02", "2024-01-04", today="2024-01-10")
        assert len(rows) == 3
        assert primary.calls == []   # 命中缓存，不拉取


class TestIncrementalGap:
    def test_fetch_only_gap(self, cache):
        # 缓存已有 01-02~01-04，请求 01-02~01-10 → 只拉右侧缺口
        cache.upsert("600519.SH", BARS_H1)
        more = BARS_H1 + [
            {"date": "2024-01-08", "open": 1.3, "high": 1.5, "low": 1.2, "close": 1.4, "volume": 400},
        ]
        primary = RecordingPrimary(more)
        svc = HistoryService(cache, primary_fetch=primary)
        rows = svc.get_history("600519.SH", "2024-01-02", "2024-01-10", today="2024-02-01")
        assert len(primary.calls) == 1
        _, gstart, _ = primary.calls[0]
        assert gstart == "2024-01-05"     # 缺口从已覆盖 max+1 开始
        assert len(rows) == 4


class TestOfflineFallback:
    def test_returns_cache_when_all_sources_down(self, cache):
        cache.upsert("600519.SH", BARS_H1)
        primary = RecordingPrimary(BARS_H1, fail=True)   # 主源挂
        svc = HistoryService(cache, primary_fetch=primary, fallback_fetch=None)
        # 请求超出缓存区间 → 触发拉取 → 主源抛异常 → 仍返回缓存已有部分
        rows = svc.get_history("600519.SH", "2024-01-02", "2024-06-30", today="2024-07-01")
        assert len(rows) == 3   # 缓存里的 3 天


class TestPrimaryFailFallback:
    def test_fallback_used_when_primary_fails(self, cache):
        primary = RecordingPrimary([], fail=True)
        fb_calls = []

        def fallback(code, offset=100):
            fb_calls.append((code, offset))
            return BARS_H1

        svc = HistoryService(cache, primary_fetch=primary, fallback_fetch=fallback)
        rows = svc.get_history("600519.SH", "2024-01-02", "2024-01-04", today="2024-01-10")
        assert len(fb_calls) == 1        # 主源失败 → 用兜底
        assert len(rows) == 3            # 兜底数据落缓存后返回


class TestTodayBar:
    def test_appends_today_bar(self, cache):
        cache.upsert("600519.SH", BARS_H1)
        primary = RecordingPrimary(BARS_H1)
        today_bar = {"date": "2024-01-05", "open": 1.3, "high": 1.6,
                     "low": 1.2, "close": 1.5, "volume": 500}
        svc = HistoryService(cache, primary_fetch=primary,
                             today_fetch=lambda code: today_bar)
        rows = svc.get_history("600519.SH", "2024-01-02", "2024-01-05", today="2024-01-05")
        assert rows[-1]["date"] == "2024-01-05"
        assert rows[-1]["close"] == 1.5

    def test_no_today_bar_when_end_before_today(self, cache):
        cache.upsert("600519.SH", BARS_H1)
        primary = RecordingPrimary(BARS_H1)
        called = []
        svc = HistoryService(cache, primary_fetch=primary,
                             today_fetch=lambda code: called.append(code))
        svc.get_history("600519.SH", "2024-01-02", "2024-01-04", today="2024-06-01")
        assert called == []   # end < today，不取今日 bar

    def test_today_bar_none_gracefully_skipped(self, cache):
        cache.upsert("600519.SH", BARS_H1)
        primary = RecordingPrimary(BARS_H1)
        svc = HistoryService(cache, primary_fetch=primary, today_fetch=lambda code: None)
        rows = svc.get_history("600519.SH", "2024-01-02", "2024-01-06", today="2024-01-06")
        assert len(rows) == 3   # 今日无成交占位 → None → 不追加


class TestNormalization:
    def test_accepts_bare_and_prefixed_code(self, cache):
        cache.upsert("600519.SH", BARS_H1)
        primary = RecordingPrimary(BARS_H1)
        svc = HistoryService(cache, primary_fetch=primary)
        rows = svc.get_history("sh600519", "2024-01-02", "2024-01-04", today="2024-01-10")
        assert len(rows) == 3   # sh600519 归一化到 600519.SH 命中缓存
