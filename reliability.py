"""Fail-open reliability. None of this decides a trade or blocks the loop.

Heartbeat pings, the Discord outbox, the SESSION row, and the battery
warning all log and continue. A missing URL, a down network, or a
database error is not a halt. A confirmed Discord send keeps the outbox
row and sets delivered_at. SESSION_FINAL's CLEAN field reads scan marks,
exit-pass gaps, and the ledger seal. None of that decides a trade.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import config

_CT = ZoneInfo("America/Chicago")
EXPECTED_SCANS = 12
_EXIT_START = time(8, 30)
_EXIT_END = time(14, 55)
_EXIT_GAP_S = 15 * 60

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
    cols = {row[1] for row in conn.execute("PRAGMA table_info(session_lines)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE session_lines RENAME TO session_lines_old")
        conn.execute(
            """CREATE TABLE session_lines (
                session_date TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'session',
                line TEXT NOT NULL,
                stored_at TEXT NOT NULL,
                incomplete INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (session_date, kind)
            )"""
        )
        conn.execute(
            """INSERT INTO session_lines
                   (session_date, kind, line, stored_at, incomplete)
               SELECT session_date, 'session', line, stored_at, 0
               FROM session_lines_old"""
        )
        conn.execute("DROP TABLE session_lines_old")
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
    if "delivered_at" not in cols:
        conn.execute("ALTER TABLE discord_outbox ADD COLUMN delivered_at TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_date TEXT NOT NULL,
            kind TEXT NOT NULL,
            at_ct TEXT NOT NULL,
            fetched INTEGER,
            universe_n INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ledger_seal (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            digest TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        )"""
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
                """INSERT INTO session_lines
                       (session_date, kind, line, stored_at, incomplete)
                   VALUES (?, 'session', ?, ?, 0)
                   ON CONFLICT(session_date, kind) DO UPDATE SET
                     line=excluded.line,
                     stored_at=excluded.stored_at""",
                (day, text, datetime.now(timezone.utc).isoformat()),
            )
        return True
    except Exception as exc:
        print(f"[session] persist failed: {exc}")
        return False


def persist_session_final(line: str, *, incomplete: bool = False) -> bool:
    """Store SESSION_FINAL beside the 14:45 SESSION row. Does not replace it.

    One row per Chicago day. A second call updates that row.
    """
    text = (line or "").strip()
    parts = text.split("|")
    if len(parts) < 3 or parts[0] != "SESSION_FINAL":
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
                """INSERT INTO session_lines
                       (session_date, kind, line, stored_at, incomplete)
                   VALUES (?, 'final', ?, ?, ?)
                   ON CONFLICT(session_date, kind) DO UPDATE SET
                     line=excluded.line,
                     stored_at=excluded.stored_at,
                     incomplete=excluded.incomplete""",
                (
                    day,
                    text,
                    datetime.now(timezone.utc).isoformat(),
                    1 if incomplete else 0,
                ),
            )
        return True
    except Exception as exc:
        print(f"[session] final persist failed: {exc}")
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
        cols = {row[1] for row in conn.execute("PRAGMA table_info(session_lines)")}
        if "kind" in cols:
            row = conn.execute(
                """SELECT 1 FROM session_lines
                   WHERE session_date=? AND kind='session'""",
                (day,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM session_lines WHERE session_date=?",
                (day,),
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def note_delivered_critical(body: str) -> None:
    """Record a CRITICAL that Discord accepted on the first try.

    A queued chunk gets its delivered_at later, when the flush POST
    returns HTTP 200 or 204. The row stays so the time can be read.
    """
    text = (body or "").strip()
    if not text:
        return
    path = config.NEWS_DB_PATH
    if not path or not _writes_allowed(path):
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect(path) as conn:
            _ensure(conn)
            conn.execute(
                """INSERT INTO discord_outbox
                       (body, created_at, critical, delivered_at)
                   VALUES (?, ?, 1, ?)""",
                (text, now, now),
            )
    except Exception as exc:
        print(f"[discord] delivered stamp failed: {exc}")


def note_completed_scan(when, fetched: int, universe_n: int) -> None:
    """One scheduled scan that finished its fetch loop."""
    _insert_mark("scan", when, int(fetched), int(universe_n))


def note_exit_pass(when) -> None:
    """One exit-only pass. The clock is the pass's own time."""
    _insert_mark("exit", when, None, None)


def _write_seal(conn: sqlite3.Connection) -> None:
    digest = _ledger_digest(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO ledger_seal (id, digest, sealed_at)
           VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             digest=excluded.digest,
             sealed_at=excluded.sealed_at""",
        (digest, now),
    )


def seal_ledger() -> None:
    """Hash the ledger after the bot's own writes. A later edit will not match."""
    path = config.NEWS_DB_PATH
    if not path or not _writes_allowed(path):
        return
    try:
        with _connect(path) as conn:
            _ensure(conn)
            _write_seal(conn)
    except Exception as exc:
        print(f"[session] ledger seal failed: {exc}")


def clean_field(day: date) -> str:
    """`CLEAN yes` or `CLEAN no` plus the failed clauses. Never raises."""
    try:
        reasons = _clean_reasons(day)
    except Exception as exc:
        print(f"[session] clean check failed: {exc}")
        text = str(exc).replace("|", " ")
        return "CLEAN no clean check failed " + text
    if not reasons:
        return "CLEAN yes"
    return "CLEAN no " + "; ".join(reasons)


def _as_ct(when) -> datetime:
    if when is None:
        return datetime.now(_CT)
    if when.tzinfo is None:
        return when.replace(tzinfo=_CT)
    return when.astimezone(_CT)


def _insert_mark(kind: str, when, fetched, universe_n) -> None:
    path = config.NEWS_DB_PATH
    if not path or not _writes_allowed(path):
        return
    ct = _as_ct(when)
    try:
        with _connect(path) as conn:
            _ensure(conn)
            conn.execute(
                """INSERT INTO session_marks
                       (session_date, kind, at_ct, fetched, universe_n)
                   VALUES (?, ?, ?, ?, ?)""",
                (ct.date().isoformat(), kind, ct.isoformat(), fetched, universe_n),
            )
            _write_seal(conn)
    except Exception as exc:
        print(f"[session] {kind} mark failed: {exc}")


def _clean_reasons(day: date) -> list[str]:
    reasons = []
    path = config.NEWS_DB_PATH
    scans = 0
    zeros = 0
    exits: list[datetime] = []
    digest = None
    sealed = None
    if path and os.path.exists(path):
        uri = "file:" + os.path.abspath(path) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            scans, zeros, exits = _marks_for(conn, day)
            sealed, digest = _seal_status(conn)
        finally:
            conn.close()
    else:
        reasons.append("no bot seal")
    if scans != EXPECTED_SCANS:
        reasons.append(f"scans {scans}/{EXPECTED_SCANS}")
    gap = _exit_gap_reason(exits, day)
    if gap:
        reasons.append(gap)
    if zeros:
        reasons.append(f"scan 0/10 x{zeros}")
    if path and os.path.exists(path):
        if sealed is None:
            reasons.append("no bot seal")
        elif sealed != digest:
            reasons.append("ledger edited after the last bot mark")
    if _rehearsal_hitting_live_db():
        reasons.append("simulated edit of the production database")
    return reasons


def _marks_for(conn: sqlite3.Connection, day: date):
    names = _table_names(conn)
    if "session_marks" not in names:
        return 0, 0, []
    scans = 0
    zeros = 0
    exits: list[datetime] = []
    rows = conn.execute(
        """SELECT kind, at_ct, fetched, universe_n
           FROM session_marks WHERE session_date=?""",
        (day.isoformat(),),
    )
    for kind, at_ct, fetched, universe_n in rows:
        if kind == "scan":
            scans += 1
            if fetched == 0 and universe_n == 10:
                zeros += 1
        elif kind == "exit":
            moment = _parse_ct(at_ct)
            if moment is not None:
                exits.append(moment)
    return scans, zeros, exits


def _seal_status(conn: sqlite3.Connection):
    digest = _ledger_digest(conn)
    names = _table_names(conn)
    if "ledger_seal" not in names:
        return None, digest
    row = conn.execute("SELECT digest FROM ledger_seal WHERE id=1").fetchone()
    if row is None:
        return None, digest
    return row[0], digest


def _exit_gap_reason(moments: list[datetime], day: date) -> str | None:
    start = datetime.combine(day, _EXIT_START, tzinfo=_CT)
    end = datetime.combine(day, _EXIT_END, tzinfo=_CT)
    inside = [moment for moment in moments if start <= moment <= end]
    inside.sort()
    points = [start, *inside, end]
    worst = None
    worst_s = _EXIT_GAP_S
    for left, right in zip(points, points[1:]):
        gap = (right - left).total_seconds()
        if gap > worst_s:
            worst_s = gap
            worst = (left, right, gap)
    if worst is None:
        return None
    left, right, gap = worst
    return (
        f"exit gap {_minutes(gap)} min "
        f"{left.strftime('%H:%M')}-{right.strftime('%H:%M')}"
    )


def _minutes(seconds: float) -> str:
    minutes = seconds / 60.0
    if abs(minutes - round(minutes)) < 0.05:
        return str(int(round(minutes)))
    return f"{minutes:.1f}"


def _parse_ct(raw) -> datetime | None:
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return _as_ct(moment)


def _rehearsal_hitting_live_db() -> bool:
    argv = " ".join(sys.argv)
    if "rehearsal" not in argv:
        return False
    path = os.path.abspath(config.NEWS_DB_PATH or "")
    return path == _live_news_db()


def _ledger_digest(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    names = _table_names(conn)
    _hash_table(digest, conn, names, "trade_history", _trade_sql(conn, names))
    _hash_table(digest, conn, names, "session_lines", _session_sql(conn, names))
    _hash_table(
        digest,
        conn,
        names,
        "session_marks",
        "SELECT id, session_date, kind, at_ct, fetched, universe_n "
        "FROM session_marks ORDER BY id",
    )
    return digest.hexdigest()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _hash_table(digest, conn, names: set[str], table: str, sql: str) -> None:
    digest.update(table.encode())
    digest.update(b"\0")
    if table not in names:
        digest.update(b"missing\0")
        return
    for row in conn.execute(sql):
        digest.update(repr(tuple(row)).encode())
        digest.update(b"\n")


def _trade_sql(conn: sqlite3.Connection, names: set[str]) -> str:
    if "trade_history" not in names:
        return ""
    cols = _columns(conn, "trade_history")
    pnl = "pnl_fill" if "pnl_fill" in cols else "pnl"
    ticker = "ticker" if "ticker" in cols else "''"
    notes = "notes" if "notes" in cols else "''"
    key = "id" if "id" in cols else "rowid"
    closed = "closed_at" if "closed_at" in cols else "''"
    return (
        f"SELECT {key}, {closed}, {ticker}, {notes}, {pnl} "
        "FROM trade_history ORDER BY 1"
    )


def _session_sql(conn: sqlite3.Connection, names: set[str]) -> str:
    if "session_lines" not in names:
        return ""
    cols = _columns(conn, "session_lines")
    kind = "kind" if "kind" in cols else "'session'"
    incomplete = "incomplete" if "incomplete" in cols else "0"
    return (
        f"SELECT session_date, {kind}, line, {incomplete} "
        "FROM session_lines ORDER BY 1, 2"
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


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
    """One short POST per undelivered row. No retry sleep.

    HTTP 200 or 204 sets delivered_at and keeps the row. A failure
    leaves delivered_at empty and stops the batch.
    """
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
                   WHERE delivered_at IS NULL
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
                            """UPDATE discord_outbox
                               SET delivered_at=? WHERE id=?""",
                            (datetime.now(timezone.utc).isoformat(), row["id"]),
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
