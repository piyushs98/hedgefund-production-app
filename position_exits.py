"""
position_exits.py — Deterministic scan-path exits (Stage 4 B1–B5 + C-D + carry).

Runs inside the 30-minute scan. No tracker daemon, no Gemini, no new process.

  B1  Selective EOD flatten at EXIT_EOD_FLATTEN_CDT (default 14:45 CDT):
      only calendar_dte < CARRY_MIN_DTE (default 2) — multi-day may overnight
  B2  Expiry flatten when expiration date is strictly before session date
  B3  Stop-loss / take-profit vs live option mark each scan
  B4  Persist a mark row every scan for every still-open (and pre-close) position
  B5  Breakeven lock / trailing giveback / time stop (uses peak_pnl_pct from B4)
  C-D 0DTE hard flatten at/after EXIT_ZERO_DTE_FLATTEN_CDT (default 13:00 CDT)
  Carry morning re-eval once/day before admits (score/pivot/mark + Discord)

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

# Once-per-process-day flags (Chicago session date).
_eod_flatten_dates: set[str] = set()
_carry_review_dates: set[str] = set()


def _parse_entry_time(trade: dict[str, Any]) -> datetime | None:
    raw = trade.get("entry_timestamp") or trade.get("entry_time")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


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


def is_zero_dte_flatten_window(dt: datetime | None = None) -> bool:
    """C-D: True at/after EXIT_ZERO_DTE_FLATTEN_CDT (default 13:00 America/Chicago)."""
    now = _chicago_now(dt)
    tt = now.time()
    if getattr(tt, "tzinfo", None) is not None:
        tt = tt.replace(tzinfo=None)
    boundary = time(
        int(getattr(config, "EXIT_ZERO_DTE_FLATTEN_HOUR", 13)),
        int(getattr(config, "EXIT_ZERO_DTE_FLATTEN_MINUTE", 0)),
    )
    return tt >= boundary


def eod_already_done(session_day: date | None = None) -> bool:
    day = session_day or _chicago_now().date()
    return day.isoformat() in _eod_flatten_dates


def mark_eod_done(session_day: date | None = None) -> None:
    day = session_day or _chicago_now().date()
    _eod_flatten_dates.add(day.isoformat())


def reset_eod_flags_for_tests() -> None:
    """Test helper — clear in-process EOD / carry-review day sets."""
    _eod_flatten_dates.clear()
    _carry_review_dates.clear()


def carry_review_already_done(session_day: date | None = None) -> bool:
    day = session_day or _chicago_now().date()
    return day.isoformat() in _carry_review_dates


def mark_carry_review_done(session_day: date | None = None) -> None:
    day = session_day or _chicago_now().date()
    _carry_review_dates.add(day.isoformat())


def _calendar_dte_for_trade(trade: dict[str, Any], sess: date) -> int | None:
    exp_d = _parse_exp_date(_trade_expiration(trade))
    if exp_d is None:
        return None
    return (exp_d - sess).days


def _carry_min_dte() -> int:
    return int(getattr(config, "CARRY_MIN_DTE", 2))


def _fmt_contract_label(trade: dict[str, Any]) -> str:
    ticker = trade.get("ticker") or "?"
    direction = _trade_direction(trade)
    strike = _trade_strike(trade)
    exp = _trade_expiration(trade) or "?"
    letter = "P" if "PUT" in direction else "C"
    try:
        strike_s = f"{float(strike):g}" if strike is not None else "?"
    except (TypeError, ValueError):
        strike_s = str(strike or "?")
    exp_s = str(exp)[5:] if exp and len(str(exp)) >= 10 else str(exp)
    return f"{ticker} {strike_s}{letter} {exp_s}"


def evaluate_exit_reason_for_mark(
    trade: dict[str, Any],
    mark: float | None,
    *,
    sess: date,
    now: datetime,
    include_time_stop: bool = True,
    do_eod: bool = False,
) -> tuple[str | None, float | None]:
    """
    Shared exit decision for scan exits and morning carry review.

    EOD is selective: only cal_dte < CARRY_MIN_DTE is forced flat.
    Morning carry review typically sets include_time_stop=False.
    """
    entry = _entry_premium(trade)
    exit_px = mark
    exp_d = _parse_exp_date(_trade_expiration(trade))
    cal_dte = (exp_d - sess).days if exp_d is not None else None
    is_0dte = exp_d is not None and exp_d == sess
    carry_min = _carry_min_dte()

    # Selective EOD — short-dated only (1DTE and under)
    if do_eod and cal_dte is not None and cal_dte < carry_min:
        if exit_px is None and entry is not None:
            exit_px = entry
        return "EOD_FLATTEN", exit_px

    # 0DTE hard flatten
    if is_0dte and is_zero_dte_flatten_window(now):
        if exit_px is None and entry is not None:
            exit_px = entry
        return "ZERO_DTE_FLATTEN", exit_px

    # Past expiry
    if exp_d is not None and exp_d < sess:
        if exit_px is None and entry is not None:
            exit_px = entry
        return "EXPIRY_FLATTEN", exit_px

    if mark is None:
        return None, None

    sl = _f(trade.get("stop_loss"))
    tp = _f(trade.get("take_profit") or trade.get("target_price"))
    if _sl_breached(mark, sl):
        return "STOP_LOSS", mark
    if _tp_breached(mark, tp):
        return "TAKE_PROFIT", mark

    pnl = _pnl_pct(entry, mark)
    # Path rules (optional time stop)
    if include_time_stop:
        b5 = _b5_exit_reason(trade, mark, entry, pnl, datetime.now(timezone.utc))
        if b5:
            return b5, mark
    else:
        # Morning review: BE lock + trailing only (no thesis-blind time stop)
        peak = _f(trade.get("peak_pnl_pct"))
        if peak is None:
            peak = pnl
        if entry is not None and pnl is not None and peak is not None:
            be_peak = float(getattr(config, "EXIT_BREAKEVEN_PEAK_PCT", 25.0))
            trail_peak = float(getattr(config, "EXIT_TRAIL_PEAK_PCT", 40.0))
            giveback = float(getattr(config, "EXIT_TRAIL_GIVEBACK_FRAC", 0.30))
            if peak >= be_peak and mark <= entry:
                return "BREAKEVEN_LOCK", mark
            if peak >= trail_peak and peak > 0 and pnl <= peak * (1.0 - giveback):
                return "TRAILING_GIVEBACK", mark

    return None, None


def run_morning_carry_review(
    open_trades: list[dict[str, Any]],
    scored_by_ticker: dict[str, dict[str, Any]],
    *,
    scan_id: str | None = None,
    now_cdt: datetime | None = None,
    session_date: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    First scan of the day: re-mark carried positions against TODAY's pivot/score.

    Runs before gate admits. Posts Discord CARRY REVIEW. Applies SL/TP /
    BE / trail / expiry (not time stop). Once per Chicago session date.
    """
    summary: dict[str, Any] = {
        "ran": False,
        "lines": [],
        "closed": [],
        "held": [],
    }
    now = _chicago_now(now_cdt)
    if not force and carry_review_already_done(now.date()):
        return summary
    if not open_trades:
        mark_carry_review_done(now.date())
        summary["ran"] = True
        return summary

    sess = session_date or _et_session_date()
    summary["ran"] = True
    lines: list[str] = []

    for trade in list(open_trades):
        if not isinstance(trade, dict) or not trade.get("ticker"):
            continue
        ticker = str(trade["ticker"])
        ctx = scored_by_ticker.get(ticker) or scored_by_ticker.get(ticker.upper()) or {}
        options_dict = ctx.get("options_dict")
        card = ctx.get("card")
        pivot_data = ctx.get("pivot_data") or {}

        mark_info = lookup_option_mark(trade, options_dict)
        entry = _entry_premium(trade)
        mark = _f(mark_info.get("mark"))
        pnl = _pnl_pct(entry, mark)

        live_score = _f(getattr(card, "total_score", None)) if card is not None else None
        today_pivot = _f(pivot_data.get("pivot")) if isinstance(pivot_data, dict) else None
        entry_score = _f(trade.get("entry_score"))
        entry_pivot = _f(trade.get("entry_pivot"))
        entry_dte = _f(trade.get("entry_dte") if trade.get("entry_dte") is not None
                       else trade.get("entry_calendar_dte"))
        today_cal = _calendar_dte_for_trade(trade, sess)

        # Persist mark / peak for B4/B5 continuity
        if mark is not None:
            record_position_mark(
                trade, mark_info, live_score=live_score, scan_id=scan_id
            )
            if pnl is not None:
                prev_peak = _f(trade.get("peak_pnl_pct"))
                if prev_peak is None or pnl > prev_peak:
                    trade["peak_pnl_pct"] = pnl
                trade["last_mark"] = mark
                trade["last_mark_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    from tracker_agent import save_active_trade
                    save_active_trade(trade)
                except Exception:
                    pass

        reason, exit_px = evaluate_exit_reason_for_mark(
            trade,
            mark,
            sess=sess,
            now=now,
            include_time_stop=False,
            do_eod=False,
        )

        # Format line
        label = _fmt_contract_label(trade)
        e_s = f"{entry:.2f}" if entry is not None else "?"
        m_s = f"{mark:.2f}" if mark is not None else "n/a"
        pnl_s = f"{pnl:+.1f}%" if pnl is not None else "n/a"
        sc_s = (
            f"{entry_score:.0f}->{live_score:.0f}"
            if entry_score is not None and live_score is not None
            else (
                f"?->{live_score:.0f}" if live_score is not None
                else f"{entry_score:.0f}->?" if entry_score is not None else "?->?"
            )
        )
        pv_s = (
            f"{entry_pivot:.2f}->{today_pivot:.2f}"
            if entry_pivot is not None and today_pivot is not None
            else (
                f"?->{today_pivot:.2f}" if today_pivot is not None
                else f"{entry_pivot:.2f}->?" if entry_pivot is not None else "?->?"
            )
        )
        dte_s = (
            f"{entry_dte:g}->{today_cal}"
            if entry_dte is not None and today_cal is not None
            else (
                f"?->{today_cal}" if today_cal is not None
                else f"{entry_dte:g}->?" if entry_dte is not None else "?->?"
            )
        )

        if reason and exit_px is not None:
            closed = close_open_position(trade, float(exit_px), f"CARRY_{reason}")
            summary["closed"].append(closed)
            action = f"CLOSE {reason}"
        else:
            summary["held"].append(ticker)
            action = "HOLD"
            if mark is None:
                action = "HOLD (no mark)"

        line = (
            f"CARRY {label} | entry {e_s} -> {m_s} ({pnl_s}) | "
            f"score {sc_s} | pivot {pv_s} | dte {dte_s} | {action}"
        )
        lines.append(line)
        print(f"[CarryReview] {line}")

    summary["lines"] = lines
    mark_carry_review_done(now.date())

    if lines:
        try:
            import broadcaster
            msg = "📋 **CARRY REVIEW**\n" + "\n".join(lines)
            if len(msg) > 1900:
                msg = msg[:1850] + "\n…(truncated)"
            broadcaster.send_discord_alert(msg)
        except Exception as e:
            print(f"[CarryReview] Discord warn: {e}")

    return summary


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


def _b5_exit_reason(
    trade: dict[str, Any],
    mark: float,
    entry: float | None,
    pnl: float | None,
    now_utc: datetime,
) -> str | None:
    """
    B5 rules (checked after hard SL/TP):
      * breakeven lock: peak >= +25% and mark back to entry
      * trailing giveback: peak >= +40% and pnl <= peak * (1 - giveback_frac)
      * time stop: open > 90m and |pnl| < 10%
    """
    if entry is None or pnl is None:
        return None

    peak = _f(trade.get("peak_pnl_pct"))
    if peak is None:
        peak = pnl

    be_peak = float(getattr(config, "EXIT_BREAKEVEN_PEAK_PCT", 25.0))
    trail_peak = float(getattr(config, "EXIT_TRAIL_PEAK_PCT", 40.0))
    giveback = float(getattr(config, "EXIT_TRAIL_GIVEBACK_FRAC", 0.30))
    t_min = int(getattr(config, "EXIT_TIME_STOP_MINUTES", 90))
    t_band = float(getattr(config, "EXIT_TIME_STOP_PNL_ABS_PCT", 10.0))

    # Breakeven lock: was up enough, mark back to entry (or worse)
    if peak >= be_peak and mark <= entry:
        return "BREAKEVEN_LOCK"

    # Trailing giveback of peak gain
    if peak >= trail_peak and peak > 0:
        floor_pnl = peak * (1.0 - giveback)
        if pnl <= floor_pnl:
            return "TRAILING_GIVEBACK"

    # Time stop: dead money
    ent = _parse_entry_time(trade)
    if ent is not None:
        age_min = (now_utc - ent.astimezone(timezone.utc)).total_seconds() / 60.0
        if age_min >= t_min and abs(pnl) < t_band:
            return "TIME_STOP"

    return None


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

        # B1 selective EOD (cal_dte < CARRY_MIN_DTE only), C-D 0DTE, B2–B5
        reason, exit_px = evaluate_exit_reason_for_mark(
            trade,
            mark,
            sess=sess,
            now=now,
            include_time_stop=True,
            do_eod=do_eod,
        )

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
        f"eod={summary['eod_triggered']} carry_min_dte={_carry_min_dte()} "
        f"open {summary['open_before']}→{summary['open_after']}"
    )
    return summary
