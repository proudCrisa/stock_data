"""Shared exchange-close rules for finalized daily bars."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def latest_finalized_date(now: datetime | date | None = None) -> str:
    """Return the latest date eligible as final in Asia/Shanghai."""
    timezone = ZoneInfo("Asia/Shanghai")
    if now is None:
        current = datetime.now(timezone)
    elif isinstance(now, datetime):
        current = (
            now.replace(tzinfo=timezone)
            if now.tzinfo is None
            else now.astimezone(timezone)
        )
    else:
        current = datetime.combine(now, time.min, timezone)
    finalized = (
        current.date()
        if current.time() >= time(16, 0)
        else current.date() - timedelta(days=1)
    )
    return finalized.isoformat()
