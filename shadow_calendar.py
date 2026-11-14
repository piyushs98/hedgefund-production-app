"""Confirmed release rows for the shadow calendar.

US federal agencies publish on Washington time. A clock whose official
page does not say Eastern is still stored as ET, with tz_source
"agency convention". A page that does say Eastern or EST keeps that
wording in tz_source. Weekly claims use "DOL weekly schedule".

Tier 1 is searched on X and can set would_block. Tier 2 is searched
and does not block. Anything else is not stored. release_at is UTC.
This module does not call X, does not score, and does not write the
trade ledger.

Names do not contain "fomc". A name or kind with those letters also
arms the 13:35 CT presser slot. FOMC may appear in keywords.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

EAST = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

# Higher-than-forecast on these families is bearish for equities.
# Other confirmed releases stay off this map, so their bias stays null.
INFLATION_FAMILIES = ("ppi", "cpi", "pce", "import_prices")

ADMIT_THRESHOLD = 70
ADMIT_LINE_80 = 80
CONTEXT_POINTS = 8

TIER_SEARCH_AND_BLOCK = 1
TIER_SEARCH_ONLY = 2

AGENCY = "agency convention"
PAGE_EASTERN = "page: Eastern Time"
PAGE_EST = "page: EST"
DOL_WEEKLY = "DOL weekly schedule"


def utc_stamp(year: int, month: int, day: int, hour: int, minute: int) -> str:
    """Eastern civil time to a UTC stamp. November 2026 is EST (UTC-5)."""
    local = datetime(year, month, day, hour, minute, tzinfo=EAST)
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(
    event_id: str,
    name: str,
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    keywords: list[str],
    kind: str | None,
    family: str | None,
    source_url: str,
    tier: int,
    tz_source: str,
    window_after_min: int | None = None,
) -> dict:
    release_at = utc_stamp(year, month, day, hour, minute)
    release = datetime(year, month, day, hour, minute, tzinfo=EAST)
    return {
        "event_id": event_id,
        "name": name,
        "release_at": release_at,
        "et": release.strftime("%Y-%m-%d %H:%M ET"),
        "ct": release.astimezone(CT).strftime("%Y-%m-%d %H:%M CT"),
        "keywords": list(keywords),
        "kind": kind,
        "family": family,
        "source_url": source_url,
        "tier": tier,
        "tz_source": tz_source,
        "window_after_min": window_after_min,
    }


# Official pages checked 2026-11-14. Nov 25 jobless claims is omitted:
# dol.gov did not confirm a Thanksgiving move.
CONFIRMED = (
    _row(
        "fed-warsh-2026-11-16",
        "Chair Warsh remarks",
        2026, 11, 16, 12, 0,
        ["Warsh", "Fed"],
        "fed",
        None,
        "https://www.federalreserve.gov/newsevents/2026-november.htm",
        TIER_SEARCH_ONLY,
        AGENCY,
        30,
    ),
    _row(
        "census-retail-2026-11-17",
        "Advance Monthly Sales for Retail and Food Services, October 2026",
        2026, 11, 17, 8, 30,
        ["retail"],
        "macro",
        None,
        "https://www.census.gov/retail/release_schedule.html",
        TIER_SEARCH_AND_BLOCK,
        AGENCY,
    ),
    _row(
        "bls-mxp-2026-11-17",
        "U.S. Import and Export Price Indexes for October 2026",
        2026, 11, 17, 8, 30,
        ["import", "export"],
        "macro",
        "import_prices",
        "https://www.bls.gov/schedule/2026/11_sched_list.htm",
        TIER_SEARCH_ONLY,
        PAGE_EASTERN,
    ),
    _row(
        "fed-g17-2026-11-17",
        "Industrial Production and Capacity Utilization",
        2026, 11, 17, 9, 15,
        ["industrial", "capacity"],
        "macro",
        None,
        "https://www.federalreserve.gov/newsevents/2026-november.htm",
        TIER_SEARCH_ONLY,
        AGENCY,
    ),
    _row(
        "census-starts-2026-11-18",
        "New Residential Construction, October 2026",
        2026, 11, 18, 8, 30,
        ["housing", "permits"],
        "macro",
        None,
        "https://www.census.gov/economic-indicators/calendar-listview-2026.html",
        TIER_SEARCH_ONLY,
        AGENCY,
    ),
    _row(
        "fed-minutes-2026-11-18",
        "Minutes of the October 27-28 meeting",
        2026, 11, 18, 14, 0,
        ["minutes", "FOMC", "Fed"],
        "fed",
        None,
        "https://www.federalreserve.gov/newsevents/2026-november.htm",
        TIER_SEARCH_AND_BLOCK,
        AGENCY,
    ),
    _row(
        "dol-claims-2026-11-19",
        "Unemployment Insurance Weekly Claims",
        2026, 11, 19, 8, 30,
        ["claims"],
        "macro",
        None,
        "https://www.dol.gov/ui/data.pdf",
        TIER_SEARCH_AND_BLOCK,
        DOL_WEEKLY,
    ),
    _row(
        "bea-gdp-2026-11-25",
        "GDP (Second Estimate) and Corporate Profits, 3rd Quarter 2026",
        2026, 11, 25, 8, 30,
        ["GDP"],
        "macro",
        None,
        "https://www.bea.gov/news/2026/gdp-advance-estimate-3rd-quarter-2026",
        TIER_SEARCH_AND_BLOCK,
        PAGE_EST,
    ),
    _row(
        "bea-income-2026-11-25",
        "Personal Income and Outlays, October 2026",
        2026, 11, 25, 8, 30,
        ["PCE", "income"],
        "macro",
        "pce",
        "https://www.bea.gov/news/2026/personal-income-and-outlays-september-2026",
        TIER_SEARCH_AND_BLOCK,
        PAGE_EST,
    ),
    _row(
        "census-durable-2026-11-25",
        "Advance Durable Goods, October 2026",
        2026, 11, 25, 8, 30,
        ["durable"],
        "macro",
        None,
        "https://www.census.gov/economic-indicators/calendar-listview-2026.html",
        TIER_SEARCH_AND_BLOCK,
        AGENCY,
    ),
    _row(
        "census-homes-2026-11-25",
        "New Residential Sales, October 2026",
        2026, 11, 25, 10, 0,
        ["home sales", "new home"],
        "macro",
        None,
        "https://www.census.gov/economic-indicators/calendar-listview-2026.html",
        TIER_SEARCH_ONLY,
        AGENCY,
    ),
    _row(
        "fed-beige-2026-11-25",
        "Beige Book",
        2026, 11, 25, 14, 0,
        ["Beige"],
        "fed",
        None,
        "https://www.federalreserve.gov/newsevents/2026-november.htm",
        TIER_SEARCH_ONLY,
        AGENCY,
    ),
)


def seed_confirmed(db_path) -> int:
    """Upsert the tier rows and drop anything else. Does not call X.

    ensure_schema adds missing columns. A second call does not change
    the row count. Returns the net change in calendar_events.
    """
    import shadow_news

    shadow_news.ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        before = conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]
        keep = [row["event_id"] for row in CONFIRMED]
        conn.execute(
            f"DELETE FROM calendar_events WHERE event_id NOT IN ({','.join('?' for _ in keep)})",
            keep,
        )
        for row in CONFIRMED:
            conn.execute(
                """
                INSERT INTO calendar_events (
                    event_id, name, release_at, keywords, kind, ticker,
                    tier, tz_source, window_after_min
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    name=excluded.name,
                    release_at=excluded.release_at,
                    keywords=excluded.keywords,
                    kind=excluded.kind,
                    tier=excluded.tier,
                    tz_source=excluded.tz_source,
                    window_after_min=excluded.window_after_min
                """,
                (
                    row["event_id"],
                    row["name"],
                    row["release_at"],
                    json.dumps(row["keywords"]),
                    row["kind"],
                    row["tier"],
                    row["tz_source"],
                    row["window_after_min"],
                ),
            )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]
    finally:
        conn.close()
    return int(after) - int(before)
