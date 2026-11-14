"""Fail-open reliability. None of this decides a trade or blocks the loop.

Heartbeat pings, the Discord outbox, the SESSION row, and the battery
warning all log and continue. A missing URL, a down network, or a
database error is not a halt.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date, datetime, timezone

import config

_HEARTBEAT_UNSET_LOGGED = False
_BATTERY_WARNED: set[str] = set()
_FLUSH_LIMIT = 8
_FLUSH_TIMEOUT_S = 3


def _live_news_db() -> str:
    root = os.path.dirname(os.path.abspath(config.__file__))
    return os.path.abspath(os.path.join(root, "data", "news_room.db"))


def _under_test() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return any(
        name.startswith("tests.") or name.startswith("test_")
        for name in sys.modules
    )


def _writes_allowed(path: str) -> bool:
    """Unit tests must not insert into the live ledger. A temp path is fine."""
    if os.path.abspath(path) != _live_news_db():
        return True
    return not _under_test()


def _connect(path: str | None = None) -> sqlite3.Connection:
    db = path or config.NEWS_DB_PATH
    parent = os.path.dirname(db)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_lines (
            session_date TEXT PRIMARY KEY,
            line TEXT NOT NULL,
            stored_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS discord_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            critical INTEGER NOT NULL DEFAULT 0
        )"""
    )
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(discord_outbox)")
    }
    if "critical" not in cols:
        conn.execute(
            "ALTER TABLE discord_outbox ADD COLUMN critical INTEGER NOT NULL DEFAULT 0"
        )


def _table_exists(name: str, path: str | None = None) -> bool:
    db = path or config.NEWS_DB_PATH
    if not db or not os.path.exists(db):
        return False
    uri = "file:" + os.path.abspath(db) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def ping_heartbeat(where: str) -> None:
    """GET HEARTBEAT_URL. A failure is logged and ignored."""
    global _HEARTBEAT_UNSET_LOGGED
    url = (os.environ.get("HEARTBEAT_URL") or "").strip()
    if not url:
        if not _HEARTBEAT_UNSET_LOGGED:
            _HEARTBEAT_UNSET_LOGGED = True
            print("[heartbeat] HEARTBEAT_URL unset — dead-man ping skipped")
        return
    try:
        import requests
        requests.get(url, timeout=_FLUSH_TIMEOUT_S)
    except Exception as exc:
        print(f"[heartbeat] {where} ping failed: {exc}")


def warn_if_on_battery(session_day: date | None = None) -> None:
    """One Discord warning per Chicago session if the Mac is unplugged.

    Market-hours caller only. Not a critical and not a trading halt.
    """
    day = (session_day or date.today()).isoformat()
    if day in _BATTERY_WARNED:
        return
    try:
        import subprocess
        proc = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        print(f"[power] battery check failed: {exc}")
        return
    if "Now drawing from 'Battery Power'" not in text:
        return
    _BATTERY_WARNED.add(day)
    msg = (
        "⚠️ **MAC ON BATTERY** during market hours. "
        "Sleep has cut sessions short before. Plug into AC power. "
        "Trading is not halted."
    )
    print(f"[power] {msg}")
    try:
        import broadcaster
        broadcaster.send_discord_alert(msg, critical=False)
    except Exception as exc:
        print(f"[power] Discord warn failed: {exc}")


def persist_session_line(line: str) -> bool:
    """Store the SESSION line. True when the row is in the database."""
    text = (line or "").strip()
    parts = text.split("|")
    if len(parts) < 3 or parts[0] != "SESSION":
        return False
    day = parts[2].strip()
    if not day:
        return False
    path = config.NEWS_DB_PATH
    if not _writes_allowed(path):
        return False
    try:
        with _connect(path) as conn:
            _ensure(conn)
            conn.execute(
                """INSERT INTO session_lines (session_date, line, stored_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(session_date) DO UPDATE SET
                     line=excluded.line,
                     stored_at=excluded.stored_at""",
                (day, text, datetime.now(timezone.utc).isoformat()),
            )
        return True
    except Exception as exc:
        print(f"[session] persist failed: {exc}")
        return False


def session_line_exists(day: str) -> bool:
    if not _table_exists("session_lines"):
        return False
    try:
        uri = "file:" + os.path.abspath(config.NEWS_DB_PATH) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM session_lines WHERE session_date=?",
            (day,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def enqueue_discord(bodies: list[str], critical: bool = False) -> None:
    """Queue chunks Discord did not accept. Does not count a critical.

    critical=True is stored so a later flush sends those rows first.
    A resend must not call note_critical again.
    """
    rows = [b for b in bodies if b and str(b).strip()]
    if not rows:
        return
    path = config.NEWS_DB_PATH
    if not _writes_allowed(path):
        return
    now = datetime.now(timezone.utc).isoformat()
    flag = 1 if critical else 0
    try:
        with _connect(path) as conn:
            _ensure(conn)
            conn.executemany(
                """INSERT INTO discord_outbox (body, created_at, critical)
                   VALUES (?, ?, ?)""",
                [(body, now, flag) for body in rows],
            )
    except Exception as exc:
        print(f"[discord] outbox insert failed: {exc}")


def flush_discord_outbox(limit: int = _FLUSH_LIMIT) -> None:
    """One short POST per queued row. No retry sleep. Leave the row on failure."""
    if not _table_exists("discord_outbox"):
        return
    url = (os.environ.get("DISCORD_WEBHOOK") or "").strip()
    path = config.NEWS_DB_PATH
    if not _writes_allowed(path):
        return
    try:
        with _connect(path) as conn:
            _ensure(conn)
            rows = conn.execute(
                """SELECT id, body FROM discord_outbox
                   ORDER BY critical DESC, id ASC LIMIT ?""",
                (int(limit),),
            ).fetchall()
            if not rows:
                return
            if not url:
                print("[discord] outbox holding — DISCORD_WEBHOOK unset")
                return
            import requests
            for row in rows:
                try:
                    resp = requests.post(
                        url,
                        json={"content": row["body"], "username": "Options AI 🤖"},
                        timeout=_FLUSH_TIMEOUT_S,
                    )
                    if resp.status_code in (200, 204):
                        conn.execute(
                            "DELETE FROM discord_outbox WHERE id=?",
                            (row["id"],),
                        )
                        continue
                    print(
                        f"[discord] outbox id={row['id']} "
                        f"HTTP {resp.status_code} — will retry"
                    )
                    break
                except Exception as exc:
                    print(f"[discord] outbox id={row['id']} failed: {exc}")
                    break
    except Exception as exc:
        print(f"[discord] outbox flush failed: {exc}")
