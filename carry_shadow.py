"""Logging-only replay of the old carry for 80+ names flattened at 14:45.

No paper sell, no gate slot, no open position. The next session is marked
from 08:30 CT under stop, target, and the time stop. The selective
calendar-DTE carry rule is not applied. A missing quote stays unresolved.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date, datetime, time, timezone
from typing import Any

import config
import session_clock

_TABLE = "carry_shadow"


def _writes_allowed(path: str) -> bool:
    root = os.path.dirname(os.path.abspath(config.__file__))
    live = os.path.abspath(os.path.join(root, "data", "news_room.db"))
    if os.path.abspath(path) != live:
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return not any(
        name.startswith("tests.") or name.startswith("test_")
        for name in sys.modules
    )


def _connect() -> sqlite3.Connection:
    path = config.NEWS_DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT,
            ticker TEXT,
            direction TEXT,
            strike REAL,
            expiration TEXT,
            qty INTEGER,
            entry_premium REAL,
            entry_ask REAL,
            entry_score REAL,
            stop_loss REAL,
            take_profit REAL,
            planned_risk REAL,
            flatten_px REAL,
            flatten_bid REAL,
            flatten_pnl REAL,
            flatten_r REAL,
            flatten_on TEXT,
            next_session TEXT,
            status TEXT,
            shadow_reason TEXT,
            shadow_px REAL,
            shadow_pnl REAL,
            shadow_r REAL,
            last_mark REAL,
            last_mark_at TEXT,
            created_at TEXT
        )"""
    )


def _f(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        out = float(val)
        if out != out:
            return None
        return out
    except (TypeError, ValueError):
        return None


def _holidays() -> set[str]:
    try:
        from master_bot import NYSE_HOLIDAYS
        return set(NYSE_HOLIDAYS)
    except Exception:
        return set()


def record_flatten(
    trade: dict[str, Any],
    closed: dict[str, Any],
    flattened_on: date,
) -> None:
    """Remember an 80+ EOD flatten. Scores below 80 are not shadowed."""
    score = _f(trade.get("entry_score"))
    if score is None or score < 80:
        return
    path = config.NEWS_DB_PATH
    if not _writes_allowed(path):
        return
    entry = _f(trade.get("entry_premium") or trade.get("entry_price"))
    entry_ask = _f(closed.get("entry_ask"))
    if entry_ask is None:
        entry_ask = _f(trade.get("entry_ask"))
    qty = closed.get("quantity") or trade.get("quantity") or 1
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        qty = 1
    stop = _f(trade.get("stop_loss"))
    target = _f(trade.get("take_profit") or trade.get("target_price"))
    planned = None
    if entry is not None and stop is not None and entry > stop and qty > 0:
        planned = (entry - stop) * 100.0 * qty
    flatten_pnl = _f(closed.get("pnl_fill"))
    if flatten_pnl is None:
        flatten_pnl = _f(closed.get("pnl"))
    flatten_r = None
    if flatten_pnl is not None and planned is not None and planned > 1e-9:
        flatten_r = flatten_pnl / planned
    nxt = session_clock.next_session(flattened_on, _holidays())
    try:
        with _connect() as conn:
            _ensure(conn)
            conn.execute(
                f"""INSERT INTO {_TABLE} (
                    trade_id, ticker, direction, strike, expiration, qty,
                    entry_premium, entry_ask, entry_score, stop_loss, take_profit,
                    planned_risk, flatten_px, flatten_bid, flatten_pnl, flatten_r,
                    flatten_on, next_session, status, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(trade.get("trade_id") or ""),
                    trade.get("ticker"),
                    trade.get("direction"),
                    _f(trade.get("strike")),
                    trade.get("expiration"),
                    qty,
                    entry,
                    entry_ask,
                    score,
                    stop,
                    target,
                    planned,
                    _f(closed.get("exit_price")),
                    _f(closed.get("exit_bid")),
                    flatten_pnl,
                    flatten_r,
                    flattened_on.isoformat(),
                    nxt.isoformat(),
                    "open",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception as exc:
        print(f"[shadow] record failed: {exc}")


def _quote(row: sqlite3.Row) -> dict[str, Any] | None:
    ticker = row["ticker"]
    exp = row["expiration"]
    strike = row["strike"]
    direction = row["direction"] or "CALL"
    if not ticker or not exp or strike is None:
        return None
    try:
        from data_engineer import fetch_contract_quote
        import json
        raw = fetch_contract_quote(ticker, exp, strike, direction)
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        print(f"[shadow] {ticker} quote failed: {exc}")
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    chains = payload.get("chains") or {}
    sides = chains.get(exp) or chains.get(str(exp)) or {}
    key = "puts" if "PUT" in str(direction).upper() else "calls"
    hit = None
    for contract in sides.get(key) or []:
        if not isinstance(contract, dict):
            continue
        cs = _f(contract.get("strike"))
        if cs is not None and abs(cs - float(strike)) <= 0.051:
            hit = contract
            break
    if hit is None:
        return None
    bid = _f(hit.get("bid"))
    ask = _f(hit.get("ask"))
    last = _f(hit.get("lastPrice"))
    mark = None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mark = round((bid + ask) / 2.0, 4)
    elif last is not None and last > 0:
        mark = round(last, 4)
    if mark is None:
        return None
    return {"bid": bid, "ask": ask, "mark": mark}


def _apply(row: sqlite3.Row, quote: dict[str, Any], now: datetime) -> dict[str, Any]:
    """SL, TP, then time stop from 08:30, then the session flatten.

    The live time-stop score exempt is not applied. There is no fresh
    score on a shadow, and the entry score is >= 80 by construction, so
    using it would turn the time stop off.
    """
    mark = quote["mark"]
    entry = _f(row["entry_premium"])
    stop = _f(row["stop_loss"])
    target = _f(row["take_profit"])
    reason = None
    if stop is not None and mark <= stop:
        reason = "STOP_LOSS"
    elif target is not None and mark >= target:
        reason = "TAKE_PROFIT"
    else:
        t_min = int(getattr(config, "EXIT_TIME_STOP_MINUTES", 90))
        band = float(getattr(config, "EXIT_TIME_STOP_PNL_ABS_PCT", 10.0))
        start = datetime.combine(
            now.date(), session_clock.SHADOW_OPEN, tzinfo=session_clock.CHICAGO
        )
        age = (now - start).total_seconds() / 60.0
        pnl_pct = None
        if entry is not None and entry > 0:
            pnl_pct = (mark - entry) / entry * 100.0
        if age >= t_min and pnl_pct is not None and abs(pnl_pct) < band:
            reason = "TIME_STOP"
        else:
            boundary = session_clock.EARLY_FLATTEN if session_clock.is_early_close(now.date()) else time(
                int(config.EXIT_EOD_FLATTEN_HOUR),
                int(config.EXIT_EOD_FLATTEN_MINUTE),
            )
            clock = now.timetz().replace(tzinfo=None)
            if clock >= boundary:
                reason = "SESSION_END"
    bid = _f(quote.get("bid"))
    fill = bid if bid is not None and bid > 0 else mark
    return {"reason": reason, "mark": mark, "fill": fill}


def advance(now: datetime | None = None) -> None:
    """Update open shadows. Read-only when the table is absent."""
    path = config.NEWS_DB_PATH
    if not path or not os.path.exists(path):
        return
    uri = "file:" + os.path.abspath(path) + "?mode=ro"
    try:
        probe = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return
    try:
        found = probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_TABLE,),
        ).fetchone()
    except sqlite3.Error:
        return
    finally:
        probe.close()
    if not found or not _writes_allowed(path):
        return
    ct = session_clock.chicago_now(now)
    try:
        with _connect() as conn:
            _ensure(conn)
            rows = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE status='open'"
            ).fetchall()
            for row in rows:
                nxt = date.fromisoformat(str(row["next_session"]))
                if ct.date() < nxt:
                    continue
                if ct.date() > nxt:
                    conn.execute(
                        f"""UPDATE {_TABLE}
                            SET status='missed', shadow_reason='MISSED_SESSION'
                            WHERE id=?""",
                        (row["id"],),
                    )
                    print(
                        f"[shadow] {row['ticker']} next session {nxt.isoformat()} "
                        "missed — no shadow fill invented"
                    )
                    continue
                clock = ct.timetz().replace(tzinfo=None)
                if clock < session_clock.SHADOW_OPEN:
                    continue
                quote = _quote(row)
                if quote is None:
                    continue
                decision = _apply(row, quote, ct)
                mark = decision["mark"]
                conn.execute(
                    f"""UPDATE {_TABLE}
                        SET last_mark=?, last_mark_at=?
                        WHERE id=?""",
                    (mark, ct.isoformat(), row["id"]),
                )
                reason = decision["reason"]
                if not reason:
                    continue
                entry_ask = _f(row["entry_ask"])
                if entry_ask is None:
                    entry_ask = _f(row["entry_premium"])
                qty = int(row["qty"] or 1)
                fill = decision["fill"]
                pnl = None
                if entry_ask is not None and fill is not None:
                    pnl = (fill - entry_ask) * 100.0 * qty
                planned = _f(row["planned_risk"])
                r_fill = None
                if pnl is not None and planned is not None and planned > 1e-9:
                    r_fill = pnl / planned
                conn.execute(
                    f"""UPDATE {_TABLE}
                        SET status='closed', shadow_reason=?, shadow_px=?,
                            shadow_pnl=?, shadow_r=?
                        WHERE id=?""",
                    (reason, fill, pnl, r_fill, row["id"]),
                )
                print(
                    f"[shadow] {row['ticker']} {reason} @ {fill} "
                    f"pnl={pnl} (flatten was {row['flatten_pnl']})"
                )
    except Exception as exc:
        print(f"[shadow] advance failed: {exc}")
