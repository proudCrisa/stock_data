"""Availability windows for close-to-next-open daily evidence."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo


def price_availability_error(
    effective_day: str,
    available_at: datetime,
    next_session_day: str | None,
) -> str | None:
    """Return a stable blocker when a finalized bar misses its use window."""
    timezone = ZoneInfo("Asia/Shanghai")
    effective = date.fromisoformat(effective_day)
    observed = available_at.astimezone(timezone)
    finalized_at = datetime.combine(effective, time(15, 0), timezone)
    if observed < finalized_at:
        return "availability_precedes_finalization"
    if observed.date() == effective:
        return None
    if next_session_day is None:
        return "unknown_next_session"
    next_session = date.fromisoformat(next_session_day)
    cutoff = datetime.combine(next_session, time(9, 25), timezone)
    if observed.date() != next_session or observed >= cutoff:
        return "post_hoc_availability"
    return None
