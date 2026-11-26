"""Executor clocks. Entry cutoff is stricter than the paper admit cutoff."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import session_clock

ENTRY_CUTOFF = time(13, 45)
FLATTEN = time(14, 45)
SWEEP_LAG = timedelta(minutes=5)
SESSION_OPEN = time(8, 30)


def now_ct(now: datetime | None = None) -> datetime:
    return session_clock.chicago_now(now)


def flat_at(day) -> time:
    if session_clock.is_early_close(day):
        return session_clock.EARLY_FLATTEN
    return FLATTEN


def sweep_at(day) -> datetime:
    """Aware Chicago datetime of the safety sweep on ``day``."""
    start = datetime.combine(day, flat_at(day), tzinfo=session_clock.CHICAGO)
    return start + SWEEP_LAG


def entry_block(now: datetime | None) -> str | None:
    """None when a new entry is allowed."""
    ct = now_ct(now)
    if ct.weekday() >= 5:
        return "weekend"
    if session_clock.is_early_close(ct.date()):
        return "early_close"
    wall = ct.timetz().replace(tzinfo=None)
    if wall < SESSION_OPEN:
        return "before_open"
    if wall >= ENTRY_CUTOFF:
        return "after_1345"
    return None


def at_or_after(now: datetime, clock: time) -> bool:
    ct = now_ct(now)
    return ct.timetz().replace(tzinfo=None) >= clock
