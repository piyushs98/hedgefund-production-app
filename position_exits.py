"""
position_exits.py — Deterministic scan-path exits (Stage 4 B1–B4).

Runs inside the 30-minute scan. No tracker daemon, no Gemini, no new process.

  B1  EOD flatten at/after EXIT_EOD_FLATTEN_CDT (default 14:45 America/Chicago)
  B2  Expiry flatten when contract expiration date is at or past session date
  B3  Stop-loss / take-profit vs live option mark each scan
  B4  Persist a mark row every scan for every still-open (and pre-close) position

Every close: virtual_broker.paper_sell → tracker_agent.remove_active_trade
(which calls signal_gate.on_close so the concurrent slot frees).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, time, timezone
from typing import Any

import config
import virtual_broker

try:
    import pytz
except ImportError:  # pragma: no cover
    pytz = None

# Once-per-process-day EOD flag (also checked against empty book).
_eod_flatten_dates: set[str] = set()


def _chicago_now(dt: datetime | None = None) -> datetime:
    if dt is not None:
        if pytz is None:
            return dt
        tz = pytz.timezone("America/Chicago")
        if dt.tzinfo is None:
            return tz.localize(dt)
        return dt.astimezone(tz)
    if pytz is None:
        return datetime.now()
    return datetime.now(pytz.timezone("America/Chicago"))


def _et_session_date(dt: datetime | None = None) -> date:
    if pytz is None:
        return (dt or datetime.now()).date()
    tz = pytz.timezone("America/New_York")
    if dt is None:
        return datetime.now(tz).date()
    if dt.tzinfo is None:
        return tz.localize(dt).date()
    return dt.astimezone(tz).date()


def is_eod_flatten_window(dt: datetime | None = None) -> bool:
    """True when America/Chicago clock is at or after configured EOD flatten time."""
    now = _chicago_now(dt)
    tt = now.time()
    if getattr(tt, "tzinfo", None) is not None:
        tt = tt.replace(tzinfo=None)
    boundary = time(
        int(config.EXIT_EOD_FLATTEN_HOUR),
        int(config.EXIT_EOD_FLATTEN_MINUTE),
    )
    return tt >= boundary


def eod_already_done(session_day: date | None = None) -> bool:
    day = session_day or _chicago_now().date()
    return day.isoformat() in _eod_flatten_dates


def mark_eod_done(session_day: date | None = None) -> None:
    day = session_day or _chicago_now().date()
    _eod_flatten_dates.add(day.isoformat())


def reset_eod_flags_for_tests() -> None:
    """Test helper — clear in-process EOD-done set."""
    _eod_flatten_dates.clear()


def _f(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        out = float(val)
        if out != out:  # NaN
            return None
        return out
    except (TypeError, ValueError):
        return None


def _entry_premium(trade: dict[str, Any]) -> float | None:
    return _f(trade.get("entry_price") if trade.get("entry_price") is not None
              else trade.get("entry_premium"))


def _trade_strike(trade: dict[str, Any]) -> float | None:
    s = trade.get("strike")
    if s is None and isinstance(trade.get("option_contract"), dict):
        s = trade["option_contract"].get("strike")
    return _f(s)


def _trade_expiration(trade: dict[str, Any]) -> str | None:
    exp = trade.get("expiration")
    if not exp and isinstance(trade.get("option_contract"), dict):
        exp = trade["option_contract"].get("expiration")
    return str(exp) if exp else None


def _trade_direction(trade: dict[str, Any]) -> str:
    d = trade.get("direction")
    if not d and isinstance(trade.get("option_contract"), dict):
        d = trade["option_contract"].get("direction")
    return str(d or "").upper()


def _parse_exp_date(exp_str: str | None) -> date | None:
    if not exp_str:
        return None
    text = str(exp_str).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def lookup_option_mark(
    trade: dict[str, Any],
    options_dict: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Pull bid/ask/mark/spot for an open trade from a scan-fetched options_dict.

    Mark = mid(bid, ask) when both > 0; else lastPrice; else None.
    """
    out: dict[str, Any] = {
        "bid": None,
        "ask": None,
        "last": None,
        "mark": None,
        "spot": None,
        "found": False,
    }
    if not isinstance(options_dict, dict) or "error" in options_dict:
        return out

    spot = _f(options_dict.get("current_price"))
    out["spot"] = spot

    exp = _trade_expiration(trade)
    strike = _trade_strike(trade)
    direction = _trade_direction(trade)
    side_key = "puts" if "PUT" in direction else "calls"
    chains = options_dict.get("chains") or {}
    if not exp or strike is None or not isinstance(chains, dict):
        return out

    sides = chains.get(exp) or chains.get(str(exp))
    if not isinstance(sides, dict):
        # Fallback: search all loaded expiries for matching strike
        for _e, s in chains.items():
            if not isinstance(s, dict):
                continue
            contracts = s.get(side_key) or []
            hit = _match_contract(contracts, strike)
            if hit:
                return _mark_from_contract(hit, spot)
        return out

    contracts = sides.get(side_key) or []
    hit = _match_contract(contracts, strike)
    if not hit:
        return out
    return _mark_from_contract(hit, spot)


def _match_contract(contracts: list, strike: float) -> dict | None:
    best = None
    best_dist = 1e18
    for c in contracts:
        if not isinstance(c, dict):
            continue
        cs = _f(c.get("strike"))
        if cs is None:
            continue
        dist = abs(cs - strike)
        # Exact or within half-cent (float noise)
        if dist < best_dist and dist <= 0.051:
            best_dist = dist
            best = c
    return best


def _mark_from_contract(c: dict, spot: float | None) -> dict[str, Any]:
    bid = _f(c.get("bid"))
    ask = _f(c.get("ask"))
    last = _f(c.get("lastPrice"))
    mark = None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mark = round((bid + ask) / 2.0, 4)
    elif last is not None and last > 0:
        mark = round(last, 4)
    elif bid is not None and bid > 0:
        mark = round(bid, 4)
    elif ask is not None and ask > 0:
        mark = round(ask, 4)
    return {
        "bid": bid,
        "ask": ask,
        "last": last,
        "mark": mark,
        "spot": spot,
        "found": mark is not None,
    }


def _pnl_pct(entry: float | None, mark: float | None) -> float | None:
    if entry is None or mark is None or entry <= 0:
        return None
    return round((mark - entry) / entry * 100.0, 2)


def ensure_marks_table(db_path: str | None = None) -> None:
    path = db_path or config.NEWS_DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with sqlite3.connect(path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS position_marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                scan_id TEXT,
                trade_id TEXT,
                ticker TEXT,
                bid REAL,
                ask REAL,
                mark REAL,
                spot REAL,
                pnl_pct REAL,
                live_score REAL,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_position_marks_ticker_ts "
            "ON position_marks(ticker, ts)"
        )
        conn.commit()


def record_position_mark(
    trade: dict[str, Any],
    mark_info: dict[str, Any],
    *,
    live_score: float | None,
    scan_id: str | None,
    db_path: str | None = None,
) -> bool:
    """B4: append one mark observation for an open position."""
    try:
        ensure_marks_table(db_path)
        entry = _entry_premium(trade)
        mark = mark_info.get("mark")
        pnl = _pnl_pct(entry, _f(mark))
        ts = datetime.now(timezone.utc).isoformat()
        path = db_path or config.NEWS_DB_PATH
        with sqlite3.connect(path, timeout=30.0) as conn:
            conn.execute(
                """
                INSERT INTO position_marks
                    (ts, scan_id, trade_id, ticker, bid, ask, mark, spot,
                     pnl_pct, live_score, entry_price, stop_loss, take_profit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    scan_id,
                    trade.get("trade_id"),
                    trade.get("ticker"),
                    mark_info.get("bid"),
                    mark_info.get("ask"),
                    mark,
                    mark_info.get("spot"),
                    pnl,
                    live_score,
                    entry,
                    _f(trade.get("stop_loss")),
                    _f(trade.get("take_profit") or trade.get("target_price")),
                ),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[Exits] WARNING: record_position_mark failed: {e}")
        return False


def close_open_position(
    trade: dict[str, Any],
    exit_price: float,
    reason: str,
) -> dict[str, Any]:
    """
    Paper sell + remove from durable book + free gate slot.

    remove_active_trade already invokes signal_gate.on_close.
    """
    from tracker_agent import remove_active_trade

    entry = _entry_premium(trade)
    direction = _trade_direction(trade) or None
    ticker = trade.get("ticker") or "?"
    result: dict[str, Any] = {
        "ticker": ticker,
        "reason": reason,
        "exit_price": exit_price,
        "entry_price": entry,
        "ok": False,
    }
    try:
        sell = virtual_broker.paper_sell(
            trade,
            exit_price,
            direction,
            entry,
            notes=f"EXIT:{reason}",
        )
        result["sell"] = sell
        result["ok"] = bool(sell.get("ok"))
        if sell.get("pnl") is not None:
            result["pnl"] = sell["pnl"]
    except Exception as e:
        print(f"[Exits] paper_sell failed for {ticker}: {e}")
        result["sell_error"] = str(e)

    try:
        removed = remove_active_trade(trade)
        result["removed"] = bool(removed)
    except Exception as e:
        print(f"[Exits] remove_active_trade failed for {ticker}: {e}")
        result["remove_error"] = str(e)
        # Still try to free the gate if remove path failed before on_close
        try:
            import signal_gate
            if ticker and ticker != "?":
                signal_gate.get_gate().on_close(ticker)
        except Exception:
            pass

    print(
        f"[Exits] CLOSED {ticker} reason={reason} "
        f"exit=${exit_price:.4f} entry={entry} ok={result.get('ok')}"
    )
    return result


def _sl_breached(mark: float, stop_loss: float | None) -> bool:
    if stop_loss is None:
        return False
    return mark <= stop_loss


def _tp_breached(mark: float, take_profit: float | None) -> bool:
    if take_profit is None:
        return False
    return mark >= take_profit


def run_scan_exits(
    open_trades: list[dict[str, Any]],
    scored_by_ticker: dict[str, dict[str, Any]],
    *,
    scan_id: str | None = None,
    now_cdt: datetime | None = None,
    session_date: date | None = None,
    force_eod: bool | None = None,
) -> dict[str, Any]:
    """
    Evaluate every open trade once per scan. Closes mutate the durable book.

    scored_by_ticker: map ticker -> {options_dict, card, ...} from phase-1.
    Returns summary with closed list and marks_recorded count.
    """
    summary: dict[str, Any] = {
        "closed": [],
        "marks_recorded": 0,
        "skipped_no_mark": [],
        "eod_triggered": False,
        "open_before": len(open_trades),
        "open_after": None,
    }
    if not open_trades:
        summary["open_after"] = 0
        return summary

    now = _chicago_now(now_cdt)
    sess = session_date or _et_session_date()
    do_eod = force_eod if force_eod is not None else (
        is_eod_flatten_window(now) and not eod_already_done(now.date())
    )

    # Work on a shallow copy of the list; closes remove from durable store.
    for trade in list(open_trades):
        if not isinstance(trade, dict) or not trade.get("ticker"):
            continue
        ticker = str(trade["ticker"])
        ctx = scored_by_ticker.get(ticker) or scored_by_ticker.get(ticker.upper()) or {}
        options_dict = ctx.get("options_dict")
        card = ctx.get("card")
        live_score = None
        if card is not None:
            live_score = _f(getattr(card, "total_score", None))

        mark_info = lookup_option_mark(trade, options_dict)
        entry = _entry_premium(trade)
        mark = _f(mark_info.get("mark"))

        # Always try to record a mark when we have one (B4), including
        # the pre-close observation so the path is complete.
        if mark is not None:
            if record_position_mark(
                trade, mark_info, live_score=live_score, scan_id=scan_id
            ):
                summary["marks_recorded"] += 1
            # Track peak for future B5 (no exit rules here)
            pnl = _pnl_pct(entry, mark)
            if pnl is not None:
                prev_peak = _f(trade.get("peak_pnl_pct"))
                if prev_peak is None or pnl > prev_peak:
                    trade["peak_pnl_pct"] = pnl
                trade["last_mark"] = mark
                trade["last_mark_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    from tracker_agent import save_active_trade
                    save_active_trade(trade)
                except Exception as pe:
                    print(f"[Exits] peak/mark persist warn {ticker}: {pe}")

        reason: str | None = None
        exit_px: float | None = mark

        # B1 — EOD flatten (highest priority when window active)
        if do_eod:
            reason = "EOD_FLATTEN"
            if exit_px is None and entry is not None:
                exit_px = entry  # flat if no mark
        else:
            # B2 — expiry
            exp_d = _parse_exp_date(_trade_expiration(trade))
            if exp_d is not None and exp_d <= sess:
                reason = "EXPIRY_FLATTEN"
                if exit_px is None and entry is not None:
                    exit_px = entry
            # B3 — SL / TP (need a live mark)
            elif mark is not None:
                sl = _f(trade.get("stop_loss"))
                tp = _f(trade.get("take_profit") or trade.get("target_price"))
                if _sl_breached(mark, sl):
                    reason = "STOP_LOSS"
                    exit_px = mark
                elif _tp_breached(mark, tp):
                    reason = "TAKE_PROFIT"
                    exit_px = mark

        if reason is None:
            if mark is None:
                summary["skipped_no_mark"].append(ticker)
            continue

        if exit_px is None:
            summary["skipped_no_mark"].append(ticker)
            print(f"[Exits] {ticker} would close ({reason}) but no mark/entry — skip")
            continue

        closed = close_open_position(trade, float(exit_px), reason)
        summary["closed"].append(closed)

    if do_eod:
        mark_eod_done(now.date())
        summary["eod_triggered"] = True

    try:
        from tracker_agent import load_active_trades
        summary["open_after"] = len(load_active_trades())
    except Exception:
        summary["open_after"] = None

    n_closed = len(summary["closed"])
    print(
        f"[Exits] scan done: closed={n_closed} marks={summary['marks_recorded']} "
        f"eod={summary['eod_triggered']} "
        f"open {summary['open_before']}→{summary['open_after']}"
    )
    return summary
