"""End-of-day shadow reconciliation. It does not decide a trade.

Runs when the main loop enters night mode after the Chicago flatten.
It writes SESSION_FINAL beside the 14:45 SESSION row and one close tag
per calendar event released while a paper trade was open. A failure
logs and sends one non-critical Discord note. It does not change the
score, the gate, a fill, or the 14:45 line.

Trade counts and realized dollars come from that day's ledger.
Scans, the SPY range, the critical count, and the feed tokens stay on
the in-process snapshot because the ledger does not store them.
Equity, buying power, open value, and peak deployed are the broker
figures at reconciliation time.
"""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import config

_CHICAGO = ZoneInfo("America/Chicago")


def reconcile_session(now, holidays=(), *, timeout_s: float = 20) -> None:
    """Write SESSION_FINAL and the close tags. Never raises.

    A weekend, a holiday, or a clock still before the flatten writes
    nothing. A second call for a day that already has a complete final
    row does not insert another row and does not post again. An
    incomplete row is updated when a later call sees a flat book.
    """
    try:
        if not _eligible(now, holidays):
            return
        day = _session_day(now)
        if _final_is_complete(day):
            return
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            pool.submit(_write, now, day).result(timeout=timeout_s)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        _fail_note(f"{type(exc).__name__}: {exc}")


def _eligible(now, holidays) -> bool:
    if now is None:
        return False
    day = _session_day(now)
    if day.weekday() >= 5:
        return False
    closed = {str(item) for item in (holidays or ())}
    if day.isoformat() in closed:
        return False
    import position_exits
    return bool(position_exits.is_eod_flatten_window(now))


def _session_day(now) -> date:
    import fill_accounting
    return fill_accounting.chicago_now(now).date()


def _final_is_complete(day: date) -> bool:
    path = config.NEWS_DB_PATH
    if not path or not os.path.exists(path):
        return False
    uri = "file:" + os.path.abspath(path) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return False
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "session_lines" not in names:
            return False
        cols = {row[1] for row in conn.execute("PRAGMA table_info(session_lines)")}
        if "kind" not in cols or "incomplete" not in cols:
            return False
        row = conn.execute(
            """
            SELECT incomplete FROM session_lines
            WHERE session_date=? AND kind='final'
            """,
            (day.isoformat(),),
        ).fetchone()
        return row is not None and int(row[0] or 0) == 0
    except (sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        conn.close()


def _write(now, day: date) -> None:
    import fill_accounting
    import reliability
    import shadow_context

    totals = _ledger_totals(day)
    incomplete = _open_count() > 0
    equity, peak, open_value, bp = _book_figures()
    snap = fill_accounting.session_snapshot()
    snap["entries"] = totals["entries"]
    snap["closes"] = totals["closes"]
    snap["realized_mid"] = totals["realized_mid"]
    snap["realized_fill"] = totals["realized_fill"]
    snap["planned_risk_closed"] = totals["planned_risk_closed"]
    line = fill_accounting.format_session_line(
        equity_fill=equity,
        peak_deployed=peak,
        open_value=open_value,
        bp=bp,
        now=now,
        snap=snap,
    )
    if not line.startswith("SESSION|"):
        raise RuntimeError("session line was not built")
    final = "SESSION_FINAL|" + line[len("SESSION|"):]
    if incomplete:
        final += "|incomplete=1"
    # Tags first. A stored complete row would skip a later retry, so a
    # tag failure must happen before that row exists.
    shadow_context.record_close_tags(day)
    if not reliability.persist_session_final(final, incomplete=incomplete):
        raise RuntimeError("SESSION_FINAL was not stored")
    print(f"[reconcile] {final}")
    _post(final, incomplete)


def _post(line: str, incomplete: bool) -> None:
    import broadcaster

    delivered = broadcaster.send_discord_alert(line, critical=False)
    if not delivered:
        print("[reconcile] SESSION_FINAL Discord was not delivered")
    if not incomplete:
        return
    note = (
        "CRITICAL: SESSION_FINAL incomplete — "
        "open positions remain at night mode."
    )
    print(f"[reconcile] {note}")
    broadcaster.send_discord_alert(note, critical=True)


def _ledger_totals(day: date) -> dict:
    empty = {
        "entries": 0,
        "closes": 0,
        "realized_mid": 0.0,
        "realized_fill": 0.0,
        "planned_risk_closed": 0.0,
    }
    path = config.NEWS_DB_PATH
    if not path or not os.path.exists(path):
        return empty
    uri = "file:" + os.path.abspath(path) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "trade_history" not in names:
            return empty
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_history)")}
        rows = conn.execute("SELECT * FROM trade_history").fetchall()
    finally:
        conn.close()
    entries = 0
    closes = 0
    mid = 0.0
    fill = 0.0
    planned = 0.0
    for row in rows:
        moment = _parse_time(row["closed_at"] if "closed_at" in cols else None)
        if moment is None or moment.astimezone(_CHICAGO).date() != day:
            continue
        notes = str(row["notes"] or "") if "notes" in cols else ""
        if notes == "PAPER_BUY_OPEN":
            entries += 1
            continue
        if not notes.startswith("EXIT:"):
            continue
        closes += 1
        mid += _num(row, "pnl_mid", "pnl")
        fill += _num(row, "pnl_fill", "pnl_mid", "pnl")
        risk = _planned(row)
        if risk is not None:
            planned += risk
    return {
        "entries": entries,
        "closes": closes,
        "realized_mid": mid,
        "realized_fill": fill,
        "planned_risk_closed": planned,
    }


def _planned(row) -> float | None:
    import fill_accounting

    raw = row["contract_json"] if "contract_json" in row.keys() else None
    meta = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            meta = parsed
    try:
        qty = int(meta.get("quantity") or meta.get("qty") or 1)
    except (TypeError, ValueError):
        qty = 1
    entry = row["entry_price"] if "entry_price" in row.keys() else None
    return fill_accounting.planned_risk_dollars(entry, meta.get("stop_loss"), qty)


def _open_count() -> int:
    from tracker_agent import load_active_trades

    rows = load_active_trades() or []
    return sum(1 for row in rows if isinstance(row, dict))


def _book_figures():
    import virtual_broker

    virtual_broker.ensure_session_book()
    peak = float(virtual_broker._book.get("peak_deployed") or 0.0)
    peak = max(peak, float(virtual_broker._deployed_from_open_trades() or 0.0))
    port = virtual_broker.get_portfolio()
    return (
        virtual_broker.fill_equity(),
        peak,
        virtual_broker.open_mark_value(),
        port.get("buying_power"),
    )


def _parse_time(raw) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _num(row, *keys) -> float:
    for key in keys:
        if key not in row.keys() or row[key] is None:
            continue
        try:
            return float(row[key])
        except (TypeError, ValueError):
            continue
    return 0.0


def _fail_note(reason: str) -> None:
    text = f"SESSION_FINAL reconciliation failed: {reason}"
    print(f"[reconcile] {text}")
    try:
        import broadcaster
        broadcaster.send_discord_alert(text, critical=False)
    except Exception as exc:
        print(f"[reconcile] failure note was not sent: {type(exc).__name__}: {exc}")
