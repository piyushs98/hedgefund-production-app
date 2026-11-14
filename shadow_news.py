"""Shadow news store. The scorer does not read these tables.

Fed and BLS RSS confirm that a release was published. Treasury is not
polled. First Squawk runs only when calendar_events has a due row,
through the X API, not a page scrape. A due event also fetches one
^TNX window and one SPY and QQQ window. This module does not write
headlines and does not score.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import config
import first_squawk

CT = ZoneInfo("America/Chicago")
EAST = ZoneInfo("America/New_York")

TICK_S = 20
RSS_EVERY = timedelta(minutes=5)
RSS_TIMEOUT_S = 15
RETRY_GRACE = timedelta(minutes=3)
RSS_USER_AGENT = "hedgefund-shadow-news/1.0"

FEEDS = (
    {
        "source": "fed",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
    },
    {
        "source": "bls",
        "url": "https://www.bls.gov/feed/bls_latest.rss",
    },
)
# One ^TNX download per calendar event. The span is the release window
# from the macro plan (30 minutes before through 15 minutes after). It
# bounds this download only. It does not block entries.
# 2026-11-13: Yahoo ^TNX 1-minute last close was 5.154 and that day's
# daily close was 5.154. A print of 4.25 is 4.25 percent.
YIELD_SYMBOL = "^TNX"
YIELD_POINTS_PER_PERCENT = 1.0
REACTION_SYMBOLS = ("SPY", "QQQ")
REACTION_BEFORE = timedelta(minutes=30)
REACTION_AFTER = timedelta(minutes=15)
YIELD_LOOKBACK = timedelta(days=8)
_YAHOO_HEALTH = "yahoo"
_YAHOO_SHADOW = "yahoo_shadow"
_YAHOO_REACTION = "yahoo_reaction"
_FEED_SOURCES = tuple(feed["source"] for feed in FEEDS)
_SQUAWK_HEALTH = "firstsquawk"

_ENV_KEY = object()
_bad_release_logged: set[str] = set()
_started = False
_start_lock = threading.Lock()


@dataclass(frozen=True)
class Slot:
    name: str
    call_at: datetime
    retry_at: datetime
    window_start: datetime
    window_end: datetime


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(source: str, url: str, raw_text: str) -> str:
    payload = f"{source}\n{url}\n{raw_text}".encode()
    return hashlib.sha256(payload).hexdigest()


def _db_path(db_path) -> str:
    return str(db_path or config.NEWS_DB_PATH)


def connect(db_path) -> sqlite3.Connection:
    path = _db_path(db_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def connect_ro(db_path) -> sqlite3.Connection | None:
    path = Path(_db_path(db_path))
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def ensure_schema(db_path) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                ticker TEXT,
                event_id TEXT,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                published_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                actual TEXT,
                forecast TEXT,
                prior TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                rows_new INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER,
                posts_returned INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                cached_tokens INTEGER,
                posts_fetched INTEGER,
                users_fetched INTEGER,
                estimated_cost_usd REAL,
                fetched_at TEXT NOT NULL,
                detail TEXT,
                event_id TEXT,
                slot TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS source_health_source_time
            ON source_health (source, fetched_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_events (
                event_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                release_at TEXT NOT NULL,
                keywords TEXT,
                kind TEXT,
                ticker TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS squawk_calls (
                event_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (event_id, slot)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS x_call_halt (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                event_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                http_status INTEGER,
                halted_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS yield_bars (
                event_id TEXT NOT NULL,
                bar_at TEXT NOT NULL,
                yield_pct REAL,
                PRIMARY KEY (event_id, bar_at)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reaction_bars (
                event_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                bar_at TEXT NOT NULL,
                close REAL,
                PRIMARY KEY (event_id, symbol, bar_at)
            )
            """
        )
        _ensure_column(conn, "shadow_items", "component", "TEXT")
        _ensure_column(conn, "calendar_events", "tier", "INTEGER")
        _ensure_column(conn, "calendar_events", "tz_source", "TEXT")
        _ensure_column(conn, "calendar_events", "window_after_min", "INTEGER")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, decl: str) -> None:
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _record_health(conn, **fields) -> None:
    columns = (
        "source",
        "status",
        "http_status",
        "rows_new",
        "latency_ms",
        "posts_returned",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "posts_fetched",
        "users_fetched",
        "estimated_cost_usd",
        "fetched_at",
        "detail",
        "event_id",
        "slot",
    )
    values = [fields.get(name) for name in columns]
    if values[3] is None:
        values[3] = 0
    conn.execute(
        f"INSERT INTO source_health ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        values,
    )


def note_yahoo_health(
    *,
    kind: str,
    symbol: str,
    status: str,
    http_status,
    rows: int,
    latency_ms: int,
    detail: str,
    db_path=None,
) -> None:
    """Append one live chain or quote row. Never creates the file or a table.

    A locked database or a missing table returns. The scan does not wait
    on this write.
    """
    try:
        path = _db_path(db_path)
        if not os.path.isfile(path):
            return
        conn = sqlite3.connect(path, timeout=0.25)
        try:
            conn.execute("PRAGMA busy_timeout=250")
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "source_health" not in names:
                return
            _record_health(
                conn,
                source=_YAHOO_HEALTH,
                status=status,
                http_status=http_status,
                rows_new=int(rows or 0),
                latency_ms=latency_ms,
                fetched_at=utc_iso(datetime.now(timezone.utc)),
                detail=(detail or "")[:240],
                slot=kind,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return


def _insert_item(conn, row: dict) -> bool:
    digest = content_hash(row["source"], row["url"], row["raw_text"])
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO shadow_items (
            source, ticker, event_id, title, url, published_at, fetched_at,
            raw_text, content_hash, actual, forecast, prior, component
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["source"],
            row.get("ticker"),
            row.get("event_id"),
            row["title"],
            row["url"],
            row["published_at"],
            row["fetched_at"],
            row["raw_text"],
            digest,
            row.get("actual"),
            row.get("forecast"),
            row.get("prior"),
            row.get("component"),
        ),
    )
    return cur.rowcount > 0


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _child_text(node, name: str) -> str:
    for child in list(node):
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _text_only(raw: str) -> str:
    stripped = re.sub(r"<[^>]+>", " ", raw or "")
    text = " ".join(html.unescape(stripped).split())
    return re.sub(r"\s+([.,;:)])", r"\1", text)


def _published(node) -> datetime | None:
    for name in ("pubDate", "date", "updated", "published"):
        raw = _child_text(node, name)
        if not raw:
            continue
        parsed = first_squawk.parse_time(raw)
        if parsed is not None:
            return parsed
        try:
            mailed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if mailed.tzinfo is None:
            continue
        return mailed.astimezone(timezone.utc)
    return None


def parse_rss_items(body: bytes, source: str) -> list[dict]:
    """RSS 2.0 items that have a URL and a timezone-aware publish time.

    Items missing either are skipped. actual, forecast, and prior stay
    empty: those columns are filled only from a First Squawk post.
    The Fed document starts with a UTF-8 BOM. Strip it before parsing.
    """
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    root = ET.fromstring(body)
    if _local_name(root.tag) != "rss":
        raise ValueError("not rss")
    channel = next((child for child in list(root) if _local_name(child.tag) == "channel"), None)
    if channel is None:
        raise ValueError("not rss")
    items = []
    for node in list(channel):
        if _local_name(node.tag) != "item":
            continue
        title = _text_only(_child_text(node, "title"))
        url = _child_text(node, "link")
        if not url.startswith(("https://", "http://")):
            guid = _child_text(node, "guid")
            url = guid if guid.startswith(("https://", "http://")) else ""
        published = _published(node)
        raw_text = _text_only(_child_text(node, "description")) or title
        if not url or published is None or not title or not raw_text:
            continue
        items.append(
            {
                "source": source,
                "ticker": None,
                "event_id": None,
                "title": title,
                "url": url,
                "published_at": utc_iso(published),
                "raw_text": raw_text,
                "actual": None,
                "forecast": None,
                "prior": None,
            }
        )
    return items


def in_rss_window(now: datetime, holidays) -> bool:
    """07:00 through 15:00 CT inclusive, weekdays that are not NYSE holidays."""
    local = now.astimezone(CT)
    if local.weekday() >= 5:
        return False
    if local.strftime("%Y-%m-%d") in set(holidays or ()):
        return False
    # The 15:00 minute is inside the window. 15:01 is outside.
    minutes = local.hour * 60 + local.minute
    return 7 * 60 <= minutes <= 15 * 60


def _last_fetch(conn, source: str) -> datetime | None:
    row = conn.execute(
        """
        SELECT fetched_at FROM source_health
        WHERE source=?
        ORDER BY fetched_at DESC, id DESC
        LIMIT 1
        """,
        (source,),
    ).fetchone()
    if row is None:
        return None
    return first_squawk.parse_time(row["fetched_at"])


def http_get(url: str):
    response = requests.get(
        url,
        headers={
            "User-Agent": RSS_USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
        timeout=RSS_TIMEOUT_S,
    )
    return response.status_code, response.content


def _poll_one(conn, feed: dict, fetch, now: datetime) -> None:
    fetched_at = utc_iso(now)
    source = feed["source"]
    if not feed.get("url"):
        return
    started = time.perf_counter()
    try:
        status, body = fetch(feed["url"])
    except Exception as exc:
        _record_health(
            conn,
            source=source,
            status="fail",
            rows_new=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            fetched_at=fetched_at,
            detail=type(exc).__name__,
        )
        return
    latency_ms = int((time.perf_counter() - started) * 1000)
    if status != 200:
        _record_health(
            conn,
            source=source,
            status="fail",
            http_status=status,
            rows_new=0,
            latency_ms=latency_ms,
            fetched_at=fetched_at,
            detail=f"HTTP {status}",
        )
        return
    try:
        items = parse_rss_items(body or b"", source)
    except Exception:
        _record_health(
            conn,
            source=source,
            status="fail",
            http_status=status,
            rows_new=0,
            latency_ms=latency_ms,
            fetched_at=fetched_at,
            detail="not rss",
        )
        return
    new = 0
    for item in items:
        item["fetched_at"] = fetched_at
        if _insert_item(conn, item):
            new += 1
    _record_health(
        conn,
        source=source,
        status="ok",
        http_status=status,
        rows_new=new,
        latency_ms=latency_ms,
        fetched_at=fetched_at,
    )


def poll_rss(now: datetime, holidays, db_path=None, fetch=None) -> None:
    """Poll each feed that is due. One failure does not touch the others."""
    if not in_rss_window(now, holidays):
        return
    ensure_schema(db_path)
    getter = fetch or http_get
    conn = connect(db_path)
    try:
        for feed in FEEDS:
            last = _last_fetch(conn, feed["source"])
            if last is not None and now - last < RSS_EVERY:
                continue
            try:
                _poll_one(conn, feed, getter, now)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                print(f"[Shadow] {feed['source']} poll failed: {type(exc).__name__}")
    finally:
        conn.close()


def _keywords(raw) -> list[str]:
    """calendar_events.keywords is a JSON array of strings. Anything else is no search."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [item.strip() for item in loaded if isinstance(item, str) and item.strip()]


def _is_fomc(kind, name) -> bool:
    if (kind or "").strip().lower() == "fomc":
        return True
    return "fomc" in (name or "").lower()


def slots_for(release_at: datetime, kind, name, window_after_min=None) -> list[Slot]:
    """One statement slot. The standard window ends 5 minutes after release.

    A longer window, used for a Chair speech, closes at release plus
    window_after_min. The call is 15 seconds after that close so the
    search end_time is not in the future, and the retry is 5 minutes
    later. The 13:35 CT presser slot is unchanged and only arms for FOMC.
    """
    after = 5
    if window_after_min is not None:
        try:
            after = int(window_after_min)
        except (TypeError, ValueError):
            after = 5
        if after < 1:
            after = 5
    if after == 5:
        call_at = release_at + timedelta(minutes=3)
        retry_at = release_at + timedelta(minutes=8)
        window_end = release_at + timedelta(minutes=5)
    else:
        call_at = release_at + timedelta(minutes=after, seconds=15)
        retry_at = call_at + timedelta(minutes=5)
        window_end = release_at + timedelta(minutes=after)
    statement = Slot(
        name="statement",
        call_at=call_at,
        retry_at=retry_at,
        window_start=release_at - timedelta(minutes=1),
        window_end=window_end,
    )
    slots = [statement]
    if _is_fomc(kind, name):
        # 13:32 CT anchor: the call is 13:35, the post window is 13:31–13:37,
        # and the retry is 13:40. Flatten at 14:45 is unchanged.
        local = release_at.astimezone(CT)
        anchor = local.replace(hour=13, minute=32, second=0, microsecond=0)
        slots.append(
            Slot(
                name="presser",
                call_at=anchor + timedelta(minutes=3),
                retry_at=anchor + timedelta(minutes=8),
                window_start=anchor - timedelta(minutes=1),
                window_end=anchor + timedelta(minutes=5),
            )
        )
    return slots


def decide(now: datetime, spec: Slot, status: str | None) -> str | None:
    """primary, retry, miss, or None.

    A wake after the retry grace records a miss and does not call.
    calling_primary means the first attempt was already spent.
    calling_retry means the retry was already spent.
    """
    retry_end = spec.retry_at + RETRY_GRACE
    if status in ("called", "miss", "skipped"):
        return None
    if status is None:
        if now < spec.call_at:
            return None
        if now < spec.retry_at:
            return "primary"
        if now < retry_end:
            return "retry"
        return "miss"
    if status in ("calling_primary", "empty"):
        if now < spec.retry_at:
            return None
        if now < retry_end:
            return "retry"
        return "miss"
    if status == "calling_retry":
        return "miss"
    return None


def _call_status(conn, event_id: str, slot: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM squawk_calls WHERE event_id=? AND slot=?",
        (event_id, slot),
    ).fetchone()
    if row is None:
        return None
    return row["status"]


def _set_status(conn, event_id: str, slot: str, status: str, now: datetime) -> None:
    conn.execute(
        """
        INSERT INTO squawk_calls (event_id, slot, status, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(event_id, slot) DO UPDATE SET
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (event_id, slot, status, utc_iso(now)),
    )


def _log_call(event_id, slot, status, posts, new, cost) -> None:
    cost_text = "n/a" if cost is None else f"{cost:.6f}"
    posts_text = "n/a" if posts is None else str(posts)
    print(
        f"[Shadow] firstsquawk {event_id} {slot} {status} "
        f"posts={posts_text} new={new} cost={cost_text}"
    )


def _read_halt(conn):
    return conn.execute(
        "SELECT event_id, reason, http_status FROM x_call_halt WHERE id=1"
    ).fetchone()


def _write_halt(conn, event_id: str, reason: str, http_status, now: datetime) -> None:
    conn.execute(
        """
        INSERT INTO x_call_halt (id, event_id, reason, http_status, halted_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            event_id=excluded.event_id,
            reason=excluded.reason,
            http_status=excluded.http_status,
            halted_at=excluded.halted_at
        """,
        (event_id, reason, http_status, utc_iso(now)),
    )


def _clear_halt(conn) -> None:
    conn.execute("DELETE FROM x_call_halt WHERE id=1")


def _finish_call(conn, event, spec: Slot, action: str, now: datetime, http, text, latency, error) -> bool:
    """Record one search. Returns True when X must not be called again this tick."""
    payload = None
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
            if error is None and http == 200:
                error = "not json"
    stop = None
    if error is None and http is not None:
        stop = first_squawk.stop_reason(http, text or "")
    posts = []
    if error is None and http == 200 and stop is None:
        posts = first_squawk.posts_from_search(
            payload, spec.window_start, spec.window_end
        )
    if error is not None and http is None:
        returned = None
        cost = None
    else:
        returned = first_squawk.posts_returned_count(payload)
        cost = first_squawk.estimate_cost_usd(returned)
    fetched_at = utc_iso(now)
    new = 0
    for post in posts:
        if _insert_item(
            conn,
            {
                "source": first_squawk.ITEM_SOURCE,
                "ticker": event["ticker"],
                "event_id": event["event_id"],
                "title": post["text"],
                "url": post["url"],
                "published_at": utc_iso(post["created_at"]),
                "fetched_at": fetched_at,
                "raw_text": post["text"],
                "actual": post["actual"],
                "forecast": post["forecast"],
                "prior": post["prior"],
                "component": post.get("component"),
            },
        ):
            new += 1
    retry = action == "retry"
    halted = False
    if stop == "rate":
        health = "fail"
        final = "skipped"
        detail = first_squawk.error_detail(http, text or "")
        halted = True
    elif stop == "spend":
        health = "fail"
        final = "skipped"
        detail = first_squawk.error_detail(http, text or "")
        halted = True
    elif error is not None or http != 200:
        health = "fail"
        final = "miss" if retry else "empty"
        detail = error or first_squawk.error_detail(http, text or "")
    elif not posts:
        health = "miss" if retry else "ok"
        final = "miss" if retry else "empty"
        detail = "no posts" if retry else None
    else:
        health = "ok"
        final = "called"
        detail = None
    _record_health(
        conn,
        source=_SQUAWK_HEALTH,
        status=health,
        http_status=http,
        rows_new=new,
        latency_ms=latency,
        posts_returned=returned,
        estimated_cost_usd=cost,
        fetched_at=fetched_at,
        detail=detail,
        event_id=event["event_id"],
        slot=spec.name,
    )
    _set_status(conn, event["event_id"], spec.name, final, now)
    if halted:
        reason = "HTTP 429" if stop == "rate" else "spend cap"
        _write_halt(conn, event["event_id"], reason, http, now)
    _log_call(event["event_id"], spec.name, health, returned, new, cost)
    conn.commit()
    return halted


def _skip_halted_slot(conn, event, spec: Slot, now: datetime, halt) -> None:
    """Same-event slot that became due after a stop. No HTTP."""
    status = _call_status(conn, event["event_id"], spec.name)
    if decide(now, spec, status) is None:
        return
    detail = f"halted: {halt['reason']}"
    _record_health(
        conn,
        source=_SQUAWK_HEALTH,
        status="fail",
        rows_new=0,
        posts_returned=0,
        fetched_at=utc_iso(now),
        detail=detail,
        event_id=event["event_id"],
        slot=spec.name,
    )
    _set_status(conn, event["event_id"], spec.name, "skipped", now)
    conn.commit()
    print(
        f"[Shadow] firstsquawk {event['event_id']} {spec.name} skipped {detail}"
    )


def _one_slot(conn, event, spec: Slot, now: datetime, key: str, client) -> bool:
    """One slot. True means this response halted further X calls."""
    status = _call_status(conn, event["event_id"], spec.name)
    action = decide(now, spec, status)
    if action is None:
        return False
    fetched_at = utc_iso(now)
    if action == "miss":
        _record_health(
            conn,
            source=_SQUAWK_HEALTH,
            status="miss",
            rows_new=0,
            posts_returned=0,
            fetched_at=fetched_at,
            detail="window missed",
            event_id=event["event_id"],
            slot=spec.name,
        )
        _set_status(conn, event["event_id"], spec.name, "miss", now)
        conn.commit()
        print(
            f"[Shadow] firstsquawk {event['event_id']} {spec.name} "
            "miss window missed"
        )
        return False
    if not key:
        _record_health(
            conn,
            source=_SQUAWK_HEALTH,
            status="disabled: no token",
            rows_new=0,
            posts_returned=0,
            fetched_at=fetched_at,
            detail="disabled: no token",
            event_id=event["event_id"],
            slot=spec.name,
        )
        _set_status(conn, event["event_id"], spec.name, "skipped", now)
        conn.commit()
        print(
            f"[Shadow] firstsquawk {event['event_id']} {spec.name} "
            "disabled: no token"
        )
        return False
    keywords = _keywords(event["keywords"])
    if not keywords:
        _record_health(
            conn,
            source=_SQUAWK_HEALTH,
            status="fail",
            rows_new=0,
            posts_returned=0,
            fetched_at=fetched_at,
            detail="no keywords",
            event_id=event["event_id"],
            slot=spec.name,
        )
        _set_status(conn, event["event_id"], spec.name, "skipped", now)
        conn.commit()
        return False
    phase = "calling_primary" if action == "primary" else "calling_retry"
    _set_status(conn, event["event_id"], spec.name, phase, now)
    conn.commit()
    params = first_squawk.search_params(keywords, spec.window_start, spec.window_end)
    try:
        http, text, latency = client(params, key)
        error = None
    except Exception as exc:
        http, text, latency, error = None, "", None, type(exc).__name__
    return _finish_call(conn, event, spec, action, now, http, text, latency, error)


def consider_squawk(now: datetime, db_path=None, key=_ENV_KEY, client=None) -> None:
    """One pass over calendar_events. An empty calendar makes no calls.

    A 429 or spend-cap stop blocks every later call in this pass. A later
    pass may call a different event. The same event's other slots are
    marked skipped and are not called.
    """
    ensure_schema(db_path)
    if key is _ENV_KEY:
        secret = (os.environ.get("X_BEARER_TOKEN") or "").strip()
    else:
        secret = (key or "").strip()
    searcher = client or first_squawk.search_recent
    conn = connect(db_path)
    try:
        halt = _read_halt(conn)
        stopped = False
        rows = conn.execute(
            """
            SELECT event_id, name, release_at, keywords, kind, ticker,
                   window_after_min
            FROM calendar_events
            ORDER BY release_at, event_id
            """
        ).fetchall()
        for row in rows:
            if stopped:
                break
            event_id = row["event_id"]
            release_at = first_squawk.parse_time(row["release_at"])
            if release_at is None:
                if event_id not in _bad_release_logged:
                    _bad_release_logged.add(str(event_id))
                    print(f"[Shadow] skip event {event_id}: release_at is not timezone-aware")
                continue
            event = {
                "event_id": event_id,
                "name": row["name"],
                "keywords": row["keywords"],
                "ticker": row["ticker"],
            }
            for spec in slots_for(
                release_at, row["kind"], row["name"], row["window_after_min"]
            ):
                if halt is not None and halt["event_id"] == event_id:
                    try:
                        _skip_halted_slot(conn, event, spec, now, halt)
                    except Exception as exc:
                        conn.rollback()
                        print(
                            f"[Shadow] {event_id} {spec.name} failed: {type(exc).__name__}"
                        )
                    continue
                if halt is not None and halt["event_id"] != event_id:
                    action = decide(
                        now, spec, _call_status(conn, event_id, spec.name)
                    )
                    if action is None:
                        continue
                    _clear_halt(conn)
                    conn.commit()
                    halt = None
                try:
                    if _one_slot(conn, event, spec, now, secret, searcher):
                        halt = _read_halt(conn)
                        stopped = True
                        break
                except Exception as exc:
                    conn.rollback()
                    print(
                        f"[Shadow] {event_id} {spec.name} failed: {type(exc).__name__}"
                    )
    finally:
        conn.close()


def run_tick(now: datetime, holidays, db_path=None, key=_ENV_KEY, fetch=None, client=None) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    moment = now.astimezone(timezone.utc)
    poll_rss(moment, holidays, db_path=db_path, fetch=fetch)
    consider_squawk(moment, db_path=db_path, key=key, client=client)
    consider_yields(moment, db_path=db_path)
    consider_reactions(moment, db_path=db_path)


def _latest_health(conn, source: str, since: str | None = None):
    if since is None:
        row = conn.execute(
            """
            SELECT status, http_status, rows_new, fetched_at
            FROM source_health
            WHERE source=?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (source,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT status, http_status, rows_new, fetched_at
            FROM source_health
            WHERE source=? AND fetched_at>=?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (source, since),
        ).fetchone()
    return row


def _health_bit(source: str, row) -> str:
    if row is None:
        return f"{source} no fetch"
    status = row["status"]
    if status == "ok":
        return f"{source} ok {int(row['rows_new'] or 0)} new"
    if status == "fail":
        if row["http_status"] is None:
            return f"{source} fail"
        return f"{source} fail HTTP {int(row['http_status'])}"
    if status == "miss":
        return f"{source} miss"
    return f"{source} {status}"


def brief_line(now: datetime | None = None, db_path=None) -> str:
    """One line for the 08:15 brief. Does not create tables or raise."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(CT)
    day_start = utc_iso(local.replace(hour=0, minute=0, second=0, microsecond=0))
    try:
        conn = connect_ro(db_path)
        if conn is None:
            return "Shadow feeds: unavailable. Events: unavailable."
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            bits = []
            yahoo_fails = "unavailable"
            if "source_health" not in names:
                bits = [f"{source} no fetch" for source in _FEED_SOURCES]
            else:
                for source in _FEED_SOURCES:
                    bits.append(
                        _health_bit(source, _latest_health(conn, source, since=day_start))
                    )
                squawk = _latest_health(conn, _SQUAWK_HEALTH, since=day_start)
                if squawk is not None:
                    bits.append(_health_bit(_SQUAWK_HEALTH, squawk))
                yahoo_fails = str(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM source_health
                        WHERE source=? AND status='fail' AND fetched_at>=?
                        """,
                        (_YAHOO_HEALTH, day_start),
                    ).fetchone()[0]
                )
            events = "none scheduled"
            if "calendar_events" in names:
                rows = conn.execute(
                    "SELECT name, release_at FROM calendar_events ORDER BY release_at, name"
                ).fetchall()
                shown = []
                for row in rows:
                    release = first_squawk.parse_time(row["release_at"])
                    if release is None or release.astimezone(CT).date() != local.date():
                        continue
                    shown.append(
                        f"{row['name']} "
                        f"{release.astimezone(EAST).strftime('%H:%M')} ET / "
                        f"{release.astimezone(CT).strftime('%H:%M')} CT"
                    )
                if shown:
                    events = "; ".join(shown)
        finally:
            conn.close()
    except Exception as exc:
        print(f"[Shadow] brief line failed: {type(exc).__name__}")
        return "Shadow feeds: unavailable. Events: unavailable."
    return (
        f"Shadow feeds: {', '.join(bits)}. "
        f"Yahoo failures: {yahoo_fails}. Events: {events}."
    )


def chicago_week(now: datetime) -> tuple[datetime, datetime]:
    """Monday 00:00 CT through the next Monday, exclusive."""
    local = now.astimezone(CT)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=local.weekday()
    )
    return start, start + timedelta(days=7)


def x_spend_lines(db_path, now: datetime | None = None) -> list[str]:
    """Read-only Chicago-week X spend. A missing table does not raise."""
    header = "X SPEND (this Chicago week; posts × $0.005)"
    prices = "  Recent search is $0.005 per post returned. This total is an estimate."
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    empty = [
        header,
        "  shadow source_health is not on this disk yet.",
        "  estimated cost: $0.000000",
        prices,
    ]
    conn = connect_ro(db_path)
    if conn is None:
        return empty
    try:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "source_health" not in names:
            return empty
        start, end = chicago_week(moment)
        rows = conn.execute(
            """
            SELECT http_status, posts_returned, estimated_cost_usd
            FROM source_health
            WHERE source=? AND fetched_at>=? AND fetched_at<?
            """,
            (_SQUAWK_HEALTH, utc_iso(start), utc_iso(end)),
        ).fetchall()
    finally:
        conn.close()
    calls = 0
    posts = 0
    cost = 0.0
    for row in rows:
        if row["http_status"] is not None:
            calls += 1
        posts += int(row["posts_returned"] or 0)
        if row["estimated_cost_usd"] is not None:
            cost += float(row["estimated_cost_usd"])
    return [
        header,
        f"  calls: {calls}",
        f"  posts returned: {posts}",
        f"  estimated cost: ${cost:.6f}",
        prices,
    ]


def _yield_done(conn, event_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM source_health
        WHERE source=? AND event_id=?
        LIMIT 1
        """,
        (_YAHOO_SHADOW, event_id),
    ).fetchone()
    return row is not None


def _bars_in_window(
    frame, start: datetime, end: datetime, scale: float = YIELD_POINTS_PER_PERCENT
) -> list[tuple[datetime, float]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    columns = getattr(frame, "columns", [])
    if "Close" not in columns:
        return []
    bars = []
    for ts, row in frame.iterrows():
        moment = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            continue
        moment = moment.astimezone(timezone.utc)
        if moment < start or moment > end:
            continue
        try:
            close = float(row["Close"])
        except (TypeError, ValueError):
            continue
        if close != close:
            continue
        bars.append((moment, close / scale))
    return bars


def _tnx_history(start: datetime, end: datetime) -> dict:
    import yf_client

    started = time.perf_counter()
    hits = yf_client.crumb_429_hits()
    frame = None
    error = None
    try:
        frame = yf_client.ticker(YIELD_SYMBOL).history(
            start=start,
            end=end + timedelta(minutes=1),
            interval="1m",
            auto_adjust=False,
        )
    except Exception as exc:
        error = type(exc).__name__
    return {
        "frame": frame,
        "error": error,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "crumb429": yf_client.crumb_429_hits() > hits,
    }


def _store_yield(event_id, end, now, result, db_path) -> None:
    frame = result.get("frame")
    error = result.get("error")
    crumb = "yes" if result.get("crumb429") else "no"
    bars = [] if error else _bars_in_window(frame, result["start"], end)
    status = "ok" if bars else "fail"
    if error:
        detail = f"{error}; crumb429={crumb}"
    elif not bars:
        detail = f"empty; crumb429={crumb}"
    else:
        detail = f"crumb429={crumb}"
    conn = connect(db_path)
    try:
        if bars:
            for moment, yield_pct in bars:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO yield_bars (event_id, bar_at, yield_pct)
                    VALUES (?, ?, ?)
                    """,
                    (event_id, utc_iso(moment), yield_pct),
                )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO yield_bars (event_id, bar_at, yield_pct)
                VALUES (?, ?, NULL)
                """,
                (event_id, utc_iso(end)),
            )
        _record_health(
            conn,
            source=_YAHOO_SHADOW,
            status=status,
            rows_new=len(bars),
            latency_ms=result.get("latency_ms"),
            fetched_at=utc_iso(now),
            detail=detail[:240],
            event_id=event_id,
            slot="tnx",
        )
        conn.commit()
    finally:
        conn.close()
    print(
        f"[Shadow] yahoo_shadow {event_id} {status} bars={len(bars)} crumb429={crumb}"
    )


def consider_yields(now: datetime, db_path=None, history=None) -> None:
    """One ^TNX call per event after its reaction window ends.

    An empty calendar makes no call. A fetch that fails stores NULL.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    moment = now.astimezone(timezone.utc)
    ensure_schema(db_path)
    getter = history or _tnx_history
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT event_id, release_at FROM calendar_events
            ORDER BY release_at, event_id
            """
        ).fetchall()
        due = []
        for row in rows:
            release = first_squawk.parse_time(row["release_at"])
            if release is None:
                continue
            start = release - REACTION_BEFORE
            end = release + REACTION_AFTER
            if moment < end or moment - end > YIELD_LOOKBACK:
                continue
            if _yield_done(conn, row["event_id"]):
                continue
            due.append((row["event_id"], start, end))
    finally:
        conn.close()
    for event_id, start, end in due:
        try:
            result = getter(start, end)
        except Exception as exc:
            result = {
                "frame": None,
                "error": type(exc).__name__,
                "latency_ms": None,
                "crumb429": False,
            }
        if not isinstance(result, dict):
            result = {
                "frame": None,
                "error": "bad yield result",
                "latency_ms": None,
                "crumb429": False,
            }
        result["start"] = start
        try:
            _store_yield(event_id, end, moment, result, db_path)
        except Exception as exc:
            print(f"[Shadow] yahoo_shadow {event_id} store failed: {type(exc).__name__}")


def _reaction_done(conn, event_id: str, symbol: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM source_health
        WHERE source=? AND event_id=? AND slot=?
        LIMIT 1
        """,
        (_YAHOO_REACTION, event_id, symbol),
    ).fetchone()
    return row is not None


def _symbol_history(symbol: str, start: datetime, end: datetime, *, prepost: bool) -> dict:
    import yf_client

    started = time.perf_counter()
    hits = yf_client.crumb_429_hits()
    frame = None
    error = None
    try:
        frame = yf_client.ticker(symbol).history(
            start=start,
            end=end + timedelta(minutes=1),
            interval="1m",
            auto_adjust=False,
            prepost=prepost,
        )
    except Exception as exc:
        error = type(exc).__name__
    return {
        "frame": frame,
        "error": error,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "crumb429": yf_client.crumb_429_hits() > hits,
    }


def _store_reaction(event_id, symbol, end, now, result, db_path) -> None:
    frame = result.get("frame")
    error = result.get("error")
    crumb = "yes" if result.get("crumb429") else "no"
    bars = [] if error else _bars_in_window(frame, result["start"], end, scale=1.0)
    status = "ok" if bars else "fail"
    if error:
        detail = f"{error}; crumb429={crumb}"
    elif not bars:
        detail = f"empty; crumb429={crumb}"
    else:
        detail = f"crumb429={crumb}"
    conn = connect(db_path)
    try:
        if bars:
            for moment, close in bars:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO reaction_bars
                        (event_id, symbol, bar_at, close)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event_id, symbol, utc_iso(moment), close),
                )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO reaction_bars
                    (event_id, symbol, bar_at, close)
                VALUES (?, ?, ?, NULL)
                """,
                (event_id, symbol, utc_iso(end)),
            )
        _record_health(
            conn,
            source=_YAHOO_REACTION,
            status=status,
            rows_new=len(bars),
            latency_ms=result.get("latency_ms"),
            fetched_at=utc_iso(now),
            detail=detail[:240],
            event_id=event_id,
            slot=symbol,
        )
        conn.commit()
    finally:
        conn.close()
    print(
        f"[Shadow] yahoo_reaction {event_id} {symbol} {status} "
        f"bars={len(bars)} crumb429={crumb}"
    )


def consider_reactions(now: datetime, db_path=None, history=None) -> None:
    """One SPY and one QQQ download per event after its reaction window ends.

    An empty calendar makes no call. A fetch that fails stores NULL.
    The window bounds the download. It does not block entries.
    Equity history includes pre/post so an 08:30 ET window is not empty
    only because the cash session has not opened. ^TNX stays on its own path.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    moment = now.astimezone(timezone.utc)
    ensure_schema(db_path)
    getter = history or (
        lambda symbol, start, end: _symbol_history(symbol, start, end, prepost=True)
    )
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT event_id, release_at FROM calendar_events
            ORDER BY release_at, event_id
            """
        ).fetchall()
        due = []
        for row in rows:
            release = first_squawk.parse_time(row["release_at"])
            if release is None:
                continue
            start = release - REACTION_BEFORE
            end = release + REACTION_AFTER
            if moment < end or moment - end > YIELD_LOOKBACK:
                continue
            for symbol in REACTION_SYMBOLS:
                if _reaction_done(conn, row["event_id"], symbol):
                    continue
                due.append((row["event_id"], symbol, start, end))
    finally:
        conn.close()
    for event_id, symbol, start, end in due:
        try:
            result = getter(symbol, start, end)
        except Exception as exc:
            result = {
                "frame": None,
                "error": type(exc).__name__,
                "latency_ms": None,
                "crumb429": False,
            }
        if not isinstance(result, dict):
            result = {
                "frame": None,
                "error": "bad reaction result",
                "latency_ms": None,
                "crumb429": False,
            }
        result["start"] = start
        try:
            _store_reaction(event_id, symbol, end, moment, result, db_path)
        except Exception as exc:
            print(
                f"[Shadow] yahoo_reaction {event_id} {symbol} "
                f"store failed: {type(exc).__name__}"
            )


def yahoo_week_lines(db_path, now: datetime | None = None) -> list[str]:
    """Read-only Chicago-week Yahoo failure counts. A missing table does not raise."""
    header = "YAHOO (this Chicago week)"
    missing = [
        header,
        "  live chain/quote failures: unavailable",
        "  shadow ^TNX calls: unavailable",
    ]
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    conn = connect_ro(db_path)
    if conn is None:
        return missing
    try:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "source_health" not in names:
            return missing
        start, end = chicago_week(moment)
        live = conn.execute(
            """
            SELECT COUNT(*) FROM source_health
            WHERE source=? AND status='fail' AND fetched_at>=? AND fetched_at<?
            """,
            (_YAHOO_HEALTH, utc_iso(start), utc_iso(end)),
        ).fetchone()[0]
        shadow = conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END)
            FROM source_health
            WHERE source=? AND fetched_at>=? AND fetched_at<?
            """,
            (_YAHOO_SHADOW, utc_iso(start), utc_iso(end)),
        ).fetchone()
    finally:
        conn.close()
    calls = int(shadow[0] or 0)
    fails = int(shadow[1] or 0)
    return [
        header,
        f"  live chain/quote failures: {int(live or 0)}",
        f"  shadow ^TNX calls: {calls} failures: {fails}",
    ]


def _loop(holidays) -> None:
    while True:
        try:
            run_tick(datetime.now(timezone.utc), holidays)
        except Exception as exc:
            print(f"[Shadow] tick failed: {type(exc).__name__}")
        time.sleep(TICK_S)


def start_shadow_thread(holidays) -> None:
    """Daemon thread. A second call does nothing. Does not run a fetch itself."""
    global _started
    with _start_lock:
        if _started:
            return
        days = frozenset(holidays or ())
        thread = threading.Thread(
            target=_loop,
            args=(days,),
            name="shadow-news",
            daemon=True,
        )
        thread.start()
        _started = True
    print("[Shadow] thread started")
