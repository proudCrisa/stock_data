"""交易日历对 finalization 与 forward_context 的影响。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stockdata.cache import Cache
from stockdata.finalization import latest_finalized_date


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _cal(cache, rows):
    cache.refresh_trading_calendar(
        rows[0]["date"], rows[-1]["date"], fetcher=lambda _s, _e: rows
    )
    return cache.trading_calendar


class TestLatestFinalizedDate:
    def test_trading_day_after_close_is_finalized(self, tmp_path):
        cache = Cache(tmp_path / "cal.sqlite")
        cal = _cal(cache, [
            {"date": "2024-01-08", "is_trading_day": True},
        ])
        now = datetime(2024, 1, 8, 16, 30, tzinfo=_SHANGHAI)
        assert latest_finalized_date(now, calendar=cal) == "2024-01-08"

    def test_trading_day_before_close_uses_previous_session(self, tmp_path):
        cache = Cache(tmp_path / "cal.sqlite")
        cal = _cal(cache, [
            {"date": "2024-01-05", "is_trading_day": True},
            {"date": "2024-01-08", "is_trading_day": True},
        ])
        now = datetime(2024, 1, 8, 15, 30, tzinfo=_SHANGHAI)
        assert latest_finalized_date(now, calendar=cal) == "2024-01-05"

    def test_holiday_uses_previous_trading_day(self, tmp_path):
        cache = Cache(tmp_path / "cal.sqlite")
        cal = _cal(cache, [
            {"date": "2024-01-05", "is_trading_day": True},
            {"date": "2024-01-08", "is_trading_day": False},  # 假期
        ])
        now = datetime(2024, 1, 8, 17, 0, tzinfo=_SHANGHAI)
        assert latest_finalized_date(now, calendar=cal) == "2024-01-05"

    def test_calendar_missing_falls_back_to_heuristic(self, tmp_path):
        cache = Cache(tmp_path / "cal.sqlite")
        cal = cache.trading_calendar
        now = datetime(2024, 1, 8, 15, 0, tzinfo=_SHANGHAI)
        # fallback: before 16:00 -> previous calendar day
        assert latest_finalized_date(now, calendar=cal) == "2024-01-07"

    def test_no_calendar_uses_legacy_heuristic(self):
        now = datetime(2024, 1, 8, 15, 0, tzinfo=_SHANGHAI)
        assert latest_finalized_date(now) == "2024-01-07"
