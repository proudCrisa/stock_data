"""Shared exchange-close rules for finalized daily bars."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .cache import TradingCalendar


_CLOSE = time(16, 0)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def latest_finalized_date(
    now: datetime | date | None = None,
    calendar: TradingCalendar | None = None,
) -> str:
    """Return the latest date eligible as final in Asia/Shanghai.

    当提供 ``calendar`` 且覆盖当天时，按交易日历判断：
    - 当天是交易日且已过收盘时间 -> 当天；
    - 当天是交易日但未收盘 -> 上一个交易日；
    - 当天是非交易日 -> 上一个交易日。

    日历缺失时保留原有 16:00 启发式（fail-closed：宁可误判为已收盘，
    也不跳过可能已收盘的交易日）。
    """
    if now is None:
        current = datetime.now(_SHANGHAI)
    elif isinstance(now, datetime):
        current = (
            now.replace(tzinfo=_SHANGHAI)
            if now.tzinfo is None
            else now.astimezone(_SHANGHAI)
        )
    else:
        current = datetime.combine(now, time.min, _SHANGHAI)

    today = current.date()
    if calendar is not None and calendar.has_data():
        today_flag = calendar.is_trading_day(today.isoformat())
        if today_flag is not None:
            if today_flag and current.time() >= _CLOSE:
                return today.isoformat()
            prev = calendar.previous_trading_day_before(today.isoformat())
            if prev is not None:
                return prev

    # Fallback heuristic (pre-calendar behavior).
    finalized = (
        today
        if current.time() >= _CLOSE
        else today - timedelta(days=1)
    )
    return finalized.isoformat()
