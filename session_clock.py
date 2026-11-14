"""America/Chicago clocks for the session.

Schedule decisions use ZoneInfo("America/Chicago"). They do not use a
fixed UTC-5 offset. The NYSE night / prep / cash windows stay on
America/New_York in the macro loop, because the exchange session is Eastern.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")

# Half days. The market is open and closes at 12:00 CT. Not holidays.
EARLY_CLOSE_DATES = {
    date(2026, 11, 27),
    date(2026, 12, 24),
}
EARLY_FLATTEN = time(11, 45)
# The 13:45 CT scan may still admit. The next slot is 14:15.
ADMIT_CUTOFF = time(14, 15)
SHADOW_OPEN = time(8, 30)


def chicago_now(dt: datetime | None = None) -> datetime:
    """Aware America/Chicago time. A naive datetime is Chicago wall time."""
    if dt is None:
        return datetime.now(CHICAGO)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CHICAGO)
    return dt.astimezone(CHICAGO)


def chicago_stamp(dt: datetime | None = None) -> str:
    """YYYYMMDD-HHMMSS in America/Chicago. Scan and exit ids use this."""
    return chicago_now(dt).strftime("%Y%m%d-%H%M%S")


def is_early_close(day: date | None) -> bool:
    return day in EARLY_CLOSE_DATES


def admit_block(now: datetime | None) -> str | None:
    """None, early_close, or late_session.

    early_close blocks the whole session. late_session blocks at and
    after 14:15 CT, so the 13:45 scan still admits.
    """
    ct = chicago_now(now)
    if is_early_close(ct.date()):
        return "early_close"
    tt = ct.timetz().replace(tzinfo=None)
    if tt >= ADMIT_CUTOFF:
        return "late_session"
    return None


def next_session(day: date, holidays: set[str] | frozenset[str] | None = None) -> date:
    """Next weekday that is not a full-day NYSE holiday."""
    closed = holidays or set()
    nxt = day + timedelta(days=1)
    for _ in range(14):
        if nxt.weekday() < 5 and nxt.isoformat() not in closed:
            return nxt
        nxt += timedelta(days=1)
    return nxt
