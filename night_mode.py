"""Night harvest schedule.

The 16:00–09:14 ET branch used to sleep in 60s slices and break as soon as
``clock.time() >= 09:15``. That comparison is true all evening, so the
process harvested, slept one minute, and harvested again (~95s once the
Yahoo work is included). Sleep targets the next 09:15 ET datetime, and a
harvest runs once per that pre-market date.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, time, timedelta

import pytz

import config

PREMARKET_TIME = time(9, 15, 0)
NIGHT_START = time(16, 0, 0)
# State checks during the overnight wait are an hour apart. The last slice
# may be shorter so a 09:13 wake still reaches 09:15 instead of overshooting
# the open by a full hour. Never a 60-second poll.
MIN_NIGHT_CHECK_S = 60 * 60

_EASTERN = pytz.timezone("America/New_York")
_memory_keys: set[str] = set()

_FLAG_SQL = """
CREATE TABLE IF NOT EXISTS night_harvest_flag (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    harvested_for_date TEXT NOT NULL
)
"""


def reset_for_tests() -> None:
    _memory_keys.clear()


def as_eastern(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(_EASTERN)
    if now.tzinfo is None:
        return _EASTERN.localize(now)
    return now.astimezone(_EASTERN)


def next_premarket(now: datetime | None = None, holidays: set[str] | None = None) -> datetime:
    """Next weekday 09:15 ET that is not a full-day NYSE holiday."""
    closed = {str(day) for day in (holidays or set())}
    current = as_eastern(now)
    day = current.date()
    for _ in range(14):
        candidate = _EASTERN.localize(datetime.combine(day, PREMARKET_TIME))
        if (
            candidate > current
            and day.weekday() <= 4
            and day.isoformat() not in closed
        ):
            return candidate
        day += timedelta(days=1)
    raise RuntimeError("no NYSE pre-market within 14 days")


def harvest_session_key(now: datetime | None = None, holidays: set[str] | None = None) -> str:
    """ISO date of the pre-market this overnight stretch is feeding.

    Friday 20:00, Saturday, and Sunday all share Monday's date, so the
    weekend harvests once. A holiday Monday pushes the key to Tuesday.
    """
    return next_premarket(now, holidays).date().isoformat()


def harvest_still_ahead(
    now: datetime | None = None,
    holidays: set[str] | None = None,
) -> bool:
    """True when night mode will still run a harvest before the cash session.

    Boot uses this so an empty earnings calendar during the day pages
    immediately, while an evening boot waits for the harvest that is about
    to run.
    """
    current = as_eastern(now)
    closed = {str(day) for day in (holidays or set())}
    if current.weekday() > 4 or current.strftime("%Y-%m-%d") in closed:
        return True
    clock = current.time()
    return clock >= NIGHT_START or clock < PREMARKET_TIME


def iter_night_sleep_chunks(
    now: datetime | None = None,
    holidays: set[str] | None = None,
    *,
    min_check_s: float = MIN_NIGHT_CHECK_S,
):
    """Yield sleep lengths that end at the next pre-market.

    Callers must not harvest between chunks. The chunk size is the minimum
    state-check gap, except the tail (or a pre-market that is already closer
    than that gap), which sleeps the exact remainder.
    """
    current = as_eastern(now)
    remaining = (next_premarket(current, holidays) - current).total_seconds()
    floor = float(min_check_s)
    if remaining <= 0:
        yield floor
        return
    if remaining <= floor:
        yield remaining
        return
    while remaining > 1e-3:
        chunk = min(floor, remaining)
        yield chunk
        remaining -= chunk


def resolve_cadence(exit_seconds, full_scan_seconds) -> tuple[int, int]:
    """Same floors the macro loop has always applied. Fallback is the config default."""
    exit_s = int(exit_seconds or 300)
    full_s = int(full_scan_seconds or 1800)
    if exit_s < 60:
        exit_s = 60
    if full_s < exit_s:
        full_s = exit_s
    return exit_s, full_s


def format_interval(seconds: int) -> tuple[str, str]:
    """Return ``('5-min', '5m')`` from 300. Other values stay in seconds."""
    seconds = int(seconds)
    if seconds > 0 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes}-min", f"{minutes}m"
    return f"{seconds}s", f"{seconds}s"


def boot_headline(exit_seconds: int, full_scan_seconds: int) -> str:
    exit_long, _ = format_interval(exit_seconds)
    full_long, _ = format_interval(full_scan_seconds)
    return (
        f"({exit_long} exits / {full_long} full scan "
        "+ 11:00 CDT midday macro)"
    )


def intraday_mode_line(full_llm: bool, exit_seconds: int, full_scan_seconds: int) -> str:
    if full_llm:
        return "FULL LLM escape hatch"
    _, exit_short = format_interval(exit_seconds)
    _, full_short = format_interval(full_scan_seconds)
    return (
        f"split cadence: EXIT every {exit_short}, "
        f"FULL score/admit every {full_short}"
    )


def exit_only_line(exit_seconds: int) -> str:
    exit_long, _ = format_interval(exit_seconds)
    return (
        f"[System State] EXIT-ONLY PASS ({exit_long}) — mark open book, "
        "no new admits."
    )


def yahoo_calls_per_harvest(universe) -> int:
    """Ticker-level Yahoo calls in one night harvest.

    Does not include the shared cookie or crumb request. Disabled gov/China
    scrapers add nothing. A futures name with no percent can add one history
    call on top of this budget.
    """
    from sector_scrapers import (
        FUTURES_SYMBOLS,
        MACRO_NEWS_TICKERS,
        POLITICS_NEWS_SOURCES,
        TECH_NEWS_TICKERS,
    )

    earnings = [
        str(ticker).upper()
        for ticker in universe
        if str(ticker).upper() not in {"SPY", "QQQ", "IWM"}
    ]
    return (
        len(TECH_NEWS_TICKERS)
        + len(MACRO_NEWS_TICKERS)
        + len(POLITICS_NEWS_SOURCES)
        + len(FUTURES_SYMBOLS)
        + len(earnings)
    )


def _flag_path(db_path: str | None) -> str:
    return db_path or config.NEWS_DB_PATH


def _connect_flag(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute(_FLAG_SQL)
    return conn


def already_harvested(key: str, db_path: str | None = None) -> bool:
    key = str(key)
    if key in _memory_keys:
        return True
    path = _flag_path(db_path)
    if not os.path.exists(path):
        return False
    try:
        with _connect_flag(path) as conn:
            row = conn.execute(
                "SELECT harvested_for_date FROM night_harvest_flag WHERE id = 1"
            ).fetchone()
    except sqlite3.Error as err:
        print(f"[System] night harvest flag unreadable: {err}")
        return False
    if row and row[0] == key:
        _memory_keys.add(key)
        return True
    return False


def mark_harvested(key: str, db_path: str | None = None) -> None:
    """Remember this pre-market date in memory and in the news database.

    A process restart the same night must not harvest again. A deploy wipes
    the database, so the first boot after a deploy harvests once.
    """
    key = str(key)
    _memory_keys.add(key)
    path = _flag_path(db_path)
    try:
        with _connect_flag(path) as conn:
            conn.execute(
                "INSERT INTO night_harvest_flag (id, harvested_for_date) "
                "VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "harvested_for_date = excluded.harvested_for_date",
                (key,),
            )
    except sqlite3.Error as err:
        print(f"[System] night harvest flag not persisted: {err}")
