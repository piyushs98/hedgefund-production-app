"""
Fill vs mid accounting and Discord TRADE / SESSION records.

Triggers, sizing, entry, stops, exits, and scoring are unchanged.
Paper still decides on mid. This module records what a real fill
(buy ask / sell bid) would have paid, and that fill series is what
BOOK equity and the SESSION line report.

Discord is the durable store (Render deploys wipe the filesystem).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any

import config

CONTRACT_MULTIPLIER = 100

_NA = "n/a"
_version_cache: str | None = None

# Chicago-session counters. Discord SESSION line is the durable copy.
_session: dict[str, Any] = {
    "session_date": None,
    "scans": 0,
    "entries": 0,
    "closes": 0,
    "criticals": 0,
    "realized_mid": 0.0,
    "realized_fill": 0.0,
    "planned_risk_closed": 0.0,
    "spy_open": None,
    "spy_high": None,
    "spy_low": None,
    "spy_close": None,
}


def reset_session_for_tests() -> None:
    """Test helper — wipe in-process session counters."""
    _session["session_date"] = None
    _session["scans"] = 0
    _session["entries"] = 0
    _session["closes"] = 0
    _session["criticals"] = 0
    _session["realized_mid"] = 0.0
    _session["realized_fill"] = 0.0
    _session["planned_risk_closed"] = 0.0
    _session["spy_open"] = None
    _session["spy_high"] = None
    _session["spy_low"] = None
    _session["spy_close"] = None
    global _version_cache
    _version_cache = None


def code_version() -> str:
    """v + first 7 of the commit hash, read once per process at first call."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    raw = (
        os.environ.get("RENDER_GIT_COMMIT")
        or os.environ.get("GIT_COMMIT")
        or os.environ.get("SOURCE_VERSION")
        or ""
    ).strip()
    if len(raw) >= 7:
        _version_cache = "v" + raw[:7]
        return _version_cache
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        text = out.decode("utf-8", errors="replace").strip()
        if len(text) >= 7:
            _version_cache = "v" + text[:7]
            return _version_cache
    except Exception:
        pass
    _version_cache = "vn/a"
    return _version_cache


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


def _extract_quote(meta: dict[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(meta, dict):
        return None
    sources = [meta]
    oc = meta.get("option_contract")
    if isinstance(oc, dict):
        sources.append(oc)
    for source in sources:
        for key in keys:
            val = _f(source.get(key))
            if val is not None and val > 0:
                return val
    return None


def extract_ask(meta: dict[str, Any] | None) -> float | None:
    return _extract_quote(meta, "entry_ask", "ask")


def extract_bid(meta: dict[str, Any] | None) -> float | None:
    return _extract_quote(meta, "last_bid", "exit_bid", "bid")


def stamp_entry_quotes(contract: dict[str, Any] | None, entry_mid: float) -> dict[str, Any]:
    """
    Record selected-contract entry_mid / entry_ask on the trade dict.

    Does not change the debit. Missing ask → entry_ask = mid, fill=est.
    A more-precise entry_mid already on the contract (unrounded
    (bid+ask)/2) is kept; the trading premium may be round(mid, 2).
    """
    recorded_mid = float(entry_mid)
    if isinstance(contract, dict):
        existing = _f(contract.get("entry_mid"))
        if existing is not None and existing > 0:
            recorded_mid = existing
    out: dict[str, Any] = {
        "entry_mid": recorded_mid,
        "entry_ask": recorded_mid,
        "fill_est": True,
    }
    if not isinstance(contract, dict):
        return out
    ask = extract_ask(contract)
    if ask is None:
        ask = _f(contract.get("ask"))
    contract["entry_mid"] = recorded_mid
    if ask is not None and ask > 0:
        contract["entry_ask"] = float(ask)
        contract["fill_est"] = False
        out["entry_ask"] = float(ask)
        out["fill_est"] = False
    else:
        contract["entry_ask"] = recorded_mid
        contract["fill_est"] = True
    return out


def pnl_dollars(exit_px: float, entry_px: float, qty: int) -> float:
    return (float(exit_px) - float(entry_px)) * CONTRACT_MULTIPLIER * int(qty)


def planned_risk_dollars(entry_mid: float | None, stop_loss: float | None, qty: int) -> float | None:
    """(entry_mid - SL) * 100 * qty. Triggers stay on mid, so planned risk does too."""
    entry = _f(entry_mid)
    sl = _f(stop_loss)
    if entry is None or sl is None or entry <= 0:
        return None
    return (entry - sl) * CONTRACT_MULTIPLIER * int(qty)


def resolve_fill_prices(
    *,
    entry_mid: float,
    exit_mid: float,
    entry_ask: float | None = None,
    exit_bid: float | None = None,
) -> dict[str, Any]:
    """
    Selected-contract fill pair. Missing bid/ask fall back to mid and tag fill=est.

    No double-count: fill P&L is (exit_bid - entry_ask)*100*qty. The mid
    series is kept separately. Do not also subtract theoretical_slippage.
    """
    ask = _f(entry_ask)
    bid = _f(exit_bid)
    est = False
    if ask is None or ask <= 0:
        ask = float(entry_mid)
        est = True
    if bid is None or bid <= 0:
        bid = float(exit_mid)
        est = True
    return {
        "entry_mid": float(entry_mid),
        "entry_ask": float(ask),
        "exit_mid": float(exit_mid),
        "exit_bid": float(bid),
        "fill_est": est,
    }


def chicago_now(dt: datetime | None = None) -> datetime:
    try:
        import pytz
        tz = pytz.timezone("America/Chicago")
        if dt is None:
            return datetime.now(tz)
        if dt.tzinfo is None:
            return tz.localize(dt)
        return dt.astimezone(tz)
    except Exception:
        return dt or datetime.now()


def _ensure_session_day() -> None:
    day = chicago_now().date().isoformat()
    if _session.get("session_date") == day:
        return
    _session["session_date"] = day
    _session["scans"] = 0
    _session["entries"] = 0
    _session["closes"] = 0
    _session["criticals"] = 0
    _session["realized_mid"] = 0.0
    _session["realized_fill"] = 0.0
    _session["planned_risk_closed"] = 0.0
    _session["spy_open"] = None
    _session["spy_high"] = None
    _session["spy_low"] = None
    _session["spy_close"] = None


def note_scan() -> None:
    _ensure_session_day()
    _session["scans"] = int(_session.get("scans") or 0) + 1


def note_entry() -> None:
    _ensure_session_day()
    _session["entries"] = int(_session.get("entries") or 0) + 1


def note_close(*, pnl_mid: float, pnl_fill: float, planned_risk: float | None) -> None:
    _ensure_session_day()
    _session["closes"] = int(_session.get("closes") or 0) + 1
    _session["realized_mid"] = float(_session.get("realized_mid") or 0.0) + float(pnl_mid)
    _session["realized_fill"] = float(_session.get("realized_fill") or 0.0) + float(pnl_fill)
    if planned_risk is not None:
        _session["planned_risk_closed"] = (
            float(_session.get("planned_risk_closed") or 0.0) + float(planned_risk)
        )


def note_critical() -> None:
    _ensure_session_day()
    _session["criticals"] = int(_session.get("criticals") or 0) + 1


def note_spy(spot: float | None) -> None:
    px = _f(spot)
    if px is None or px <= 0:
        return
    _ensure_session_day()
    if _session.get("spy_open") is None:
        _session["spy_open"] = px
        _session["spy_high"] = px
        _session["spy_low"] = px
    else:
        hi = _session.get("spy_high")
        lo = _session.get("spy_low")
        _session["spy_high"] = px if hi is None else max(float(hi), px)
        _session["spy_low"] = px if lo is None else min(float(lo), px)
    _session["spy_close"] = px


def session_snapshot() -> dict[str, Any]:
    _ensure_session_day()
    return dict(_session)


def _hhmm(raw: Any) -> str:
    if raw is None or raw == "":
        return _NA
    if isinstance(raw, datetime):
        dt = chicago_now(raw)
        return dt.strftime("%H:%M")
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        dt = chicago_now(dt)
        return dt.strftime("%H:%M")
    except ValueError:
        return _NA


def _session_date_str(raw: Any = None) -> str:
    if isinstance(raw, datetime):
        return chicago_now(raw).date().isoformat()
    if raw:
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            return chicago_now(dt).date().isoformat()
        except ValueError:
            pass
    return chicago_now().date().isoformat()


def _fmt_px(label: str, val: Any, *, est: bool = False) -> str:
    """Up to 4 decimals so unrounded mid/ask survive Discord (min 2)."""
    n = _f(val)
    if n is None:
        body = f"{label} {_NA}"
    else:
        s = f"{n:.4f}".rstrip("0")
        if "." not in s:
            s += ".00"
        elif s.endswith("."):
            s += "00"
        elif len(s.split(".", 1)[1]) < 2:
            s += "0"
        body = f"{label} {s}"
    if est:
        body += " fill=est"
    return body


def _fmt_int(label: str, val: Any, *, signed: bool = False) -> str:
    n = _f(val)
    if n is None:
        return f"{label} {_NA}"
    i = int(round(n))
    if signed:
        return f"{label} {i:+d}"
    return f"{label} {i}"


def _fmt_float(label: str, val: Any, digits: int, *, signed: bool = False) -> str:
    n = _f(val)
    if n is None:
        return f"{label} {_NA}"
    if signed:
        return f"{label} {n:+.{digits}f}"
    return f"{label} {n:.{digits}f}"


def _cp(direction: Any) -> str:
    d = str(direction or "").upper()
    if "PUT" in d or d == "P":
        return "P"
    if "CALL" in d or d == "C":
        return "C"
    return _NA


def _strike_s(val: Any) -> str:
    n = _f(val)
    if n is None:
        return _NA
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:g}"


def _exp_s(val: Any) -> str:
    if not val:
        return _NA
    text = str(val).strip()[:10]
    return text if text else _NA


def _carried_flag(trade: dict[str, Any] | None, close_date: str) -> str:
    if not isinstance(trade, dict):
        return _NA
    raw = trade.get("entry_timestamp") or trade.get("entry_time")
    if not raw:
        return _NA
    entry_day = _session_date_str(raw)
    if entry_day == close_date:
        return "n"
    return "y"


def mfe_mae_from_marks(
    trade: dict[str, Any] | None,
    *,
    db_path: str | None = None,
    close_pnl_pct: float | None = None,
) -> tuple[float | None, float | None]:
    """
    Peak / trough mid P&L fraction over the life of the trade.

    position_marks.pnl_pct is percent of entry (61.0 = +61%).
    Returned as fractions (0.61, -0.08) to match the TRADE line.
    """
    pcts: list[float] = []
    if close_pnl_pct is not None:
        pcts.append(float(close_pnl_pct))
    peak = None
    trough = None
    if isinstance(trade, dict):
        peak = _f(trade.get("peak_pnl_pct"))
        trough = _f(trade.get("trough_pnl_pct"))
        if peak is not None:
            pcts.append(peak)
        if trough is not None:
            pcts.append(trough)
        trade_id = trade.get("trade_id")
        ticker = trade.get("ticker")
    else:
        trade_id = None
        ticker = None
    path = db_path or getattr(config, "NEWS_DB_PATH", None)
    if path and (trade_id or ticker):
        try:
            with sqlite3.connect(path, timeout=10.0) as conn:
                if trade_id:
                    rows = conn.execute(
                        "SELECT pnl_pct FROM position_marks WHERE trade_id = ?",
                        (str(trade_id),),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT pnl_pct FROM position_marks WHERE ticker = ?",
                        (str(ticker),),
                    ).fetchall()
            for r in rows:
                n = _f(r[0] if not isinstance(r, sqlite3.Row) else r["pnl_pct"])
                if n is not None:
                    pcts.append(n)
        except Exception:
            pass
    if not pcts:
        return None, None
    return max(pcts) / 100.0, min(pcts) / 100.0


def hold_minutes(trade: dict[str, Any] | None, closed_at: datetime | None = None) -> int | None:
    if not isinstance(trade, dict):
        return None
    raw = trade.get("entry_timestamp") or trade.get("entry_time")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        ent = datetime.fromisoformat(text)
        if ent.tzinfo is None:
            ent = ent.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    end = closed_at or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    mins = int(round((end - ent.astimezone(timezone.utc)).total_seconds() / 60.0))
    return max(0, mins)


def format_trade_line(
    trade: dict[str, Any],
    *,
    reason: str,
    entry_mid: float | None,
    entry_ask: float | None,
    exit_mid: float | None,
    exit_bid: float | None,
    qty: int,
    pnl_mid: float | None,
    pnl_fill: float | None,
    planned_risk: float | None,
    fill_est: bool = False,
    closed_at: datetime | None = None,
    db_path: str | None = None,
) -> str:
    """
    One pipe-delimited TRADE line. Missing fields write n/a so columns
    never shift. 26 fields, fixed order.
    """
    close_dt = chicago_now(closed_at)
    close_date = close_dt.date().isoformat()
    r_fill = None
    if pnl_fill is not None and planned_risk is not None and abs(planned_risk) > 1e-9:
        r_fill = float(pnl_fill) / float(planned_risk)
    close_pnl_pct = None
    if entry_mid and exit_mid and float(entry_mid) > 0:
        close_pnl_pct = (float(exit_mid) - float(entry_mid)) / float(entry_mid) * 100.0
    mfe, mae = mfe_mae_from_marks(
        trade, db_path=db_path, close_pnl_pct=close_pnl_pct
    )
    hold = hold_minutes(trade, closed_at)
    entry_score = _f(trade.get("entry_score")) if isinstance(trade, dict) else None
    exit_score = None
    if isinstance(trade, dict):
        exit_score = _f(trade.get("last_live_score"))
        if exit_score is None:
            exit_score = _f(trade.get("exit_score"))
    dte = None
    if isinstance(trade, dict):
        dte = _f(trade.get("entry_dte"))
        if dte is None:
            dte = _f(trade.get("days_to_expiration"))
        if dte is None:
            dte = _f(trade.get("entry_calendar_dte"))
        if dte is None:
            dte = _f(trade.get("calendar_dte"))
    oc = trade.get("option_contract") if isinstance(trade, dict) else None
    direction = None
    strike = None
    expiration = None
    ticker = "?"
    if isinstance(trade, dict):
        ticker = str(trade.get("ticker") or "?")
        direction = trade.get("direction")
        strike = trade.get("strike")
        expiration = trade.get("expiration")
        if isinstance(oc, dict):
            direction = direction or oc.get("direction")
            strike = strike if strike is not None else oc.get("strike")
            expiration = expiration or oc.get("expiration")
    ask_est = bool(fill_est and (entry_ask is None or _f(entry_ask) == _f(entry_mid)))
    bid_est = bool(fill_est)
    fields = [
        "TRADE",
        code_version(),
        close_date,
        ticker,
        _cp(direction),
        _strike_s(strike),
        _exp_s(expiration),
        f"qty{int(qty)}" if qty is not None else f"qty{_NA}",
        _hhmm(
            (trade or {}).get("entry_timestamp")
            or (trade or {}).get("entry_time")
        ),
        _fmt_px("entry_mid", entry_mid),
        _fmt_px("entry_ask", entry_ask if entry_ask is not None else entry_mid, est=ask_est),
        _hhmm(close_dt),
        _fmt_px("exit_mid", exit_mid),
        _fmt_px("exit_bid", exit_bid if exit_bid is not None else exit_mid, est=bid_est),
        str(reason or _NA),
        _fmt_int("pnl_mid", pnl_mid, signed=True),
        _fmt_int("pnl_fill", pnl_fill, signed=True),
        _fmt_int("planned_risk", planned_risk, signed=False),
        _fmt_float("R_fill", r_fill, 2, signed=True),
        _fmt_float("mfe_pct", mfe, 2, signed=False),
        _fmt_float("mae_pct", mae, 2, signed=True),
        _fmt_int("hold_min", hold, signed=False),
        _fmt_int("entry_score", entry_score, signed=False),
        _fmt_int("exit_score", exit_score, signed=False),
        _fmt_float("dte_entry", dte, 1, signed=False),
        f"carried {_carried_flag(trade, close_date)}",
    ]
    return "|".join(fields)


def format_session_line(
    *,
    equity_fill: float | None,
    peak_deployed: float | None,
    open_value: float | None,
    bp: float | None = None,
    now: datetime | None = None,
) -> str:
    """
    One pipe-delimited SESSION line at 14:45. 18 fields, fixed order.
    Columns 1–17 unchanged; bp is trailing column 18 (mid-cash buying power).
    """
    snap = session_snapshot()
    planned = _f(snap.get("planned_risk_closed"))
    fill = _f(snap.get("realized_fill")) or 0.0
    mid = _f(snap.get("realized_mid")) or 0.0
    spread_cost = mid - fill
    r_fill = None
    if planned is not None and abs(planned) > 1e-9:
        r_fill = fill / planned
    spy_open = _f(snap.get("spy_open"))
    spy_close = _f(snap.get("spy_close"))
    spy_high = _f(snap.get("spy_high"))
    spy_low = _f(snap.get("spy_low"))
    spy_range = None
    if spy_open and spy_high is not None and spy_low is not None and spy_open > 0:
        spy_range = (spy_high - spy_low) / spy_open * 100.0
    day = chicago_now(now).date().isoformat()
    fields = [
        "SESSION",
        code_version(),
        day,
        _fmt_int("scans", snap.get("scans"), signed=False),
        _fmt_int("entries", snap.get("entries"), signed=False),
        _fmt_int("closes", snap.get("closes"), signed=False),
        _fmt_int("realized_mid", mid, signed=True),
        _fmt_int("realized_fill", fill, signed=True),
        _fmt_int("spread_cost", spread_cost, signed=False),
        _fmt_float("R_fill", r_fill, 2, signed=True),
        _fmt_int("equity_fill", equity_fill, signed=False),
        _fmt_int("peak_deployed", peak_deployed, signed=False),
        _fmt_int("open_value", open_value, signed=False),
        _fmt_float("spy_open", spy_open, 2, signed=False),
        _fmt_float("spy_close", spy_close, 2, signed=False),
        _fmt_float("spy_range_pct", spy_range, 2, signed=False),
        _fmt_int("criticals", snap.get("criticals"), signed=False),
        _fmt_int("bp", bp, signed=False),
    ]
    return "|".join(fields)


TRADE_FIELD_COUNT = 26
SESSION_FIELD_COUNT = 18
