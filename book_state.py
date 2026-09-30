"""
Discord BOOK_STATE — durable open-book snapshot.

Render free-tier spin-down wipes the filesystem. TRADE/SESSION lines already
live in Discord; this module writes one reconstructable BOOK_STATE line and
reads the most recent one back before the first scan.

Does not change scoring, gating, sizing, stops, exits, or cadence.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import config
import fill_accounting
import virtual_broker

# Discord content hard limit. Numbered parts use this; unsplit lines stay under it.
DISCORD_CONTENT_LIMIT = 2000
_PART_PREFIX_BUDGET = len("BOOK_STATE_99of99|w=0000000000|")
BOOK_STATE_CHUNK = DISCORD_CONTENT_LIMIT - _PART_PREFIX_BUDGET  # 1968

STALE_CALENDAR_DAYS = 4
FETCH_PAGE_SIZE = 100
FETCH_MAX_PAGES = 25
FETCH_BUDGET_S = 20.0
FETCH_PAGE_SLEEP_S = 0.35
HTTP_TIMEOUT_S = 8.0
DISCORD_API = "https://discord.com/api/v10"

_NA = "n/a"

_lock = threading.Lock()
_sessions_elapsed: int = 0
_eod_counted_dates: set[str] = set()
# ticker -> ISO timestamp of last real close (post-exit cooldown).
_gate_exits: dict[str, str] = {}
_restore_done: bool = False
_trading_blocked: bool = False
_block_reason: str | None = None
_unrecovered: bool = False
_last_emitted_line: str | None = None
_shutdown_emitted: bool = False
_restoring: bool = False
_last_snapshot: dict[str, Any] | None = None
_version_mismatch: bool = False

# Typical fully-populated POS (used to report the 2000-char split threshold).
# Keep in sync with format_pos_segment field set.
_TYPICAL_POS_FOR_SPLIT = (
    "POS~NVDA~P~210~2026-09-18~qty1~trade_id 550e8400-e29b-41d4-a716-446655440000"
    "~entry_mid 6.9000~entry_ask 6.9800~entry_price 6.9000~entry_spot 209.33"
    "~entry_score 77~entry_pivot 208.50~entry_dte 3~sl 5.52~tp 10.35"
    "~trailing n/a~peak_pnl 12.00~trough_pnl -8.00"
    "~opened 2026-09-15T14:29:00+00:00~stop_spot 210.4000~target_spot 206.1000"
    "~stop_entry_spot 209.33~delta 0.4500~delta_est 0~last_mark 6.8500"
    "~last_bid 6.8000~last_ask 6.9000~last_mark_at 2026-09-15T19:40:00+00:00"
    "~last_live_score 72~last_spot 209.10~fill_est 0~thesis_below 0"
    "~mark_fail_streak 0~unmarked_since n/a"
)


def reset_for_tests() -> None:
    """Test helper — wipe in-process BOOK_STATE bookkeeping."""
    global _sessions_elapsed, _restore_done, _trading_blocked, _block_reason
    global _unrecovered, _last_emitted_line, _shutdown_emitted, _restoring
    global _last_snapshot, _version_mismatch
    _sessions_elapsed = 0
    _eod_counted_dates.clear()
    _gate_exits.clear()
    _restore_done = False
    _trading_blocked = False
    _block_reason = None
    _unrecovered = False
    _last_emitted_line = None
    _shutdown_emitted = False
    _restoring = False
    _last_snapshot = None
    _version_mismatch = False


def trading_blocked() -> bool:
    return bool(_trading_blocked)


def block_reason() -> str | None:
    return _block_reason


def unrecovered() -> bool:
    return bool(_unrecovered)


def last_snapshot() -> dict[str, Any] | None:
    return _last_snapshot


def sessions_elapsed() -> int:
    return int(_sessions_elapsed)


def current_session_number() -> int:
    """1-based session we are in / about to run (completed + 1)."""
    return int(_sessions_elapsed) + 1


def set_sessions_elapsed(n: int) -> None:
    global _sessions_elapsed
    _sessions_elapsed = max(0, int(n))


def note_exit(ticker: Any, when: datetime | None = None) -> None:
    """Record a real close so post-exit cooldown survives a recycle."""
    key = str(ticker or "").upper().strip()
    if not key:
        return
    dt = when or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    with _lock:
        _gate_exits[key] = dt.astimezone(timezone.utc).isoformat()


def _parse_iso_dt(raw: Any) -> datetime | None:
    if raw is None or raw == "" or raw == _NA:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_gate_exits() -> str:
    with _lock:
        items = sorted(_gate_exits.items())
    if not items:
        return f"gate_exits {_NA}"
    body = ",".join(f"{k}@{v}" for k, v in items)
    return f"gate_exits {body}"


def _parse_gate_exits(val: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not val or val == _NA:
        return out
    for part in str(val).split(","):
        bit = part.strip()
        if "@" not in bit:
            continue
        ticker, ts = bit.split("@", 1)
        ticker = ticker.strip().upper()
        ts = ts.strip()
        if ticker and ts and ts != _NA:
            out[ticker] = ts
    return out


def _day_flag(name: str, *, now: datetime | None = None) -> str:
    """Read position_exits once-per-Chicago-day flags into the snapshot."""
    day, _hh = _header_now(now)
    try:
        import position_exits as pex
        from datetime import date as _date
        sess = _date.fromisoformat(day)
        if name == "eod_done" and pex.eod_already_done(sess):
            return f"eod_done {day}"
        if name == "carry_done" and pex.carry_review_already_done(sess):
            return f"carry_done {day}"
        if name == "eod_book_done" and pex.eod_book_already_done(sess):
            return f"eod_book_done {day}"
    except Exception:
        pass
    return f"{name} {_NA}"


# ---------------------------------------------------------------------------
# Format / parse
# ---------------------------------------------------------------------------

def _f(val: Any) -> float | None:
    try:
        if val is None or val == "" or val == _NA:
            return None
        out = float(val)
        if out != out:
            return None
        return out
    except (TypeError, ValueError):
        return None


def _i(val: Any) -> int | None:
    n = _f(val)
    if n is None:
        return None
    return int(round(n))


def _s(val: Any) -> str:
    if val is None or val == "":
        return _NA
    text = str(val).strip()
    return text if text else _NA


def _fmt_px(label: str, val: Any) -> str:
    n = _f(val)
    if n is None:
        return f"{label} {_NA}"
    s = f"{n:.4f}".rstrip("0")
    if "." not in s:
        s += ".00"
    elif s.endswith("."):
        s += "00"
    elif len(s.split(".", 1)[1]) < 2:
        s += "0"
    return f"{label} {s}"


def _fmt_num(label: str, val: Any, *, signed: bool = False, digits: int | None = None) -> str:
    n = _f(val)
    if n is None:
        return f"{label} {_NA}"
    if digits is None:
        i = int(round(n))
        return f"{label} {i:+d}" if signed else f"{label} {i}"
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


def _direction_from_cp(cp: str) -> str:
    return "PUT" if str(cp).upper() == "P" else "CALL"


def _chicago_now(dt: datetime | None = None) -> datetime:
    return fill_accounting.chicago_now(dt)


def _header_now(now: datetime | None = None) -> tuple[str, str]:
    cdt = _chicago_now(now)
    return cdt.date().isoformat(), cdt.strftime("%H:%M")


def format_pos_segment(trade: dict[str, Any]) -> str:
    """One POS~... segment. Verbose on purpose — missing fields break exits."""
    oc = trade.get("option_contract") if isinstance(trade.get("option_contract"), dict) else {}
    ticker = str(trade.get("ticker") or "?").upper()
    cp = _cp(trade.get("direction") or oc.get("direction"))
    strike = trade.get("strike")
    if strike is None:
        strike = oc.get("strike")
    exp = trade.get("expiration") or oc.get("expiration")
    qty = virtual_broker.resolve_quantity(trade)
    opened = (
        trade.get("entry_timestamp")
        or trade.get("entry_time")
        or _NA
    )
    trailing = trade.get("trailing_stop")
    fill_est = trade.get("fill_est")
    try:
        fill_est_s = "1" if fill_est in (True, 1, "1", "true", "True") else "0"
    except Exception:
        fill_est_s = "0"
    delta_est = trade.get("underlying_delta_est") or trade.get("delta_est")
    try:
        delta_est_s = "1" if delta_est in (True, 1, "1", "true", "True") else "0"
    except Exception:
        delta_est_s = "0"
    delta = trade.get("delta")
    if delta is None:
        delta = trade.get("underlying_delta")
    if delta is None:
        delta = oc.get("delta")
    parts = [
        "POS",
        ticker,
        cp,
        fill_accounting._strike_s(strike),
        fill_accounting._exp_s(exp),
        f"qty{int(qty)}",
        f"trade_id {_s(trade.get('trade_id'))}",
        _fmt_px("entry_mid", trade.get("entry_mid") or trade.get("entry_price") or trade.get("entry_premium")),
        _fmt_px("entry_ask", trade.get("entry_ask") or oc.get("ask")),
        _fmt_px("entry_price", trade.get("entry_price") or trade.get("entry_premium") or trade.get("entry_mid")),
        _fmt_px("entry_spot", trade.get("entry_spot") or trade.get("stop_entry_spot") or trade.get("spot") or oc.get("spot")),
        _fmt_num("entry_score", trade.get("entry_score")),
        _fmt_px("entry_pivot", trade.get("entry_pivot")),
        _fmt_num("entry_dte", trade.get("entry_dte") if trade.get("entry_dte") is not None else trade.get("entry_calendar_dte") or oc.get("days_to_expiration"), digits=1),
        _fmt_px("sl", trade.get("stop_loss")),
        _fmt_px("tp", trade.get("take_profit") or trade.get("target_price")),
        f"trailing {_s(trailing) if trailing is not None else _NA}",
        _fmt_num("peak_pnl", trade.get("peak_pnl_pct"), digits=2, signed=True),
        _fmt_num("trough_pnl", trade.get("trough_pnl_pct"), digits=2, signed=True),
        f"opened {_s(opened)}",
        _fmt_px("stop_spot", trade.get("stop_spot")),
        _fmt_px("target_spot", trade.get("target_spot")),
        _fmt_px("stop_entry_spot", trade.get("stop_entry_spot") or trade.get("spot") or oc.get("spot")),
        _fmt_px("delta", delta),
        f"delta_est {delta_est_s}",
        _fmt_px("last_mark", trade.get("last_mark")),
        _fmt_px("last_bid", trade.get("last_bid") or trade.get("bid") or oc.get("bid")),
        _fmt_px("last_ask", trade.get("last_ask") or trade.get("ask") or oc.get("ask")),
        f"last_mark_at {_s(trade.get('last_mark_at'))}",
        _fmt_num("last_live_score", trade.get("last_live_score")),
        _fmt_px("last_spot", trade.get("last_spot")),
        f"fill_est {fill_est_s}",
        _fmt_num("thesis_below", trade.get("thesis_below_streak") or 0),
        _fmt_num("mark_fail_streak", trade.get("mark_fail_streak") or 0),
        f"unmarked_since {_s(trade.get('unmarked_since'))}",
    ]
    return "~".join(parts)


def format_book_state_line(
    *,
    trades: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    increment_session: bool = False,
) -> str:
    """
    One pipe-delimited BOOK_STATE line (plus POS~ segments).

    Header:
      BOOK_STATE|vXXXXXXX|YYYY-MM-DD|HH:MM|equity_fill N|bp N|
      realized_cum +N|realized_mid_cum +N|sessions_elapsed N|positions N|
      [session counters]|POS~...
    """
    if trades is None:
        try:
            from tracker_agent import load_active_trades
            trades = load_active_trades() or []
        except Exception:
            trades = []
    trades = [t for t in trades if isinstance(t, dict) and t.get("ticker")]
    day, hhmm = _header_now(now)
    elapsed = int(_sessions_elapsed)
    if increment_session and day not in _eod_counted_dates:
        elapsed += 1
    port = virtual_broker.get_portfolio()
    bp = port.get("buying_power")
    realized_fill = port.get("total_realized_pnl_fill")
    realized_mid = port.get("total_realized_pnl")
    try:
        equity = virtual_broker.fill_equity()
    except Exception:
        equity = None
    snap = fill_accounting.session_snapshot()
    try:
        peak = float(virtual_broker._book.get("peak_deployed") or 0.0)
        peak = max(peak, virtual_broker._deployed_from_open_trades())
    except Exception:
        peak = 0.0
    fields = [
        "BOOK_STATE",
        fill_accounting.code_version(),
        day,
        hhmm,
        _fmt_num("equity_fill", equity),
        _fmt_num("bp", bp),
        _fmt_num("realized_cum", realized_fill, signed=True),
        _fmt_num("realized_mid_cum", realized_mid, signed=True),
        _fmt_num("sessions_elapsed", elapsed),
        _fmt_num("positions", len(trades)),
        _fmt_num("peak_deployed", peak),
        _fmt_num("scans", snap.get("scans")),
        _fmt_num("entries", snap.get("entries")),
        _fmt_num("closes", snap.get("closes")),
        _fmt_num("criticals", snap.get("criticals")),
        _fmt_num("day_realized_fill", snap.get("realized_fill"), signed=True),
        _fmt_num("day_realized_mid", snap.get("realized_mid"), signed=True),
        _fmt_num("planned_risk_closed", snap.get("planned_risk_closed")),
        _fmt_px("spy_open", snap.get("spy_open")),
        _fmt_px("spy_high", snap.get("spy_high")),
        _fmt_px("spy_low", snap.get("spy_low")),
        _fmt_px("spy_close", snap.get("spy_close")),
        f"session_date {_s(snap.get('session_date') or day)}",
        _fmt_gate_exits(),
        _day_flag("eod_done", now=now),
        _day_flag("carry_done", now=now),
        _day_flag("eod_book_done", now=now),
    ]
    line = "|".join(fields)
    for t in trades:
        line += "|" + format_pos_segment(t)
    return line


def _line_fingerprint(line: str) -> str:
    """Identity of the book, ignoring the clock fields so scan emits can skip."""
    parts = str(line or "").split("|")
    if len(parts) > 3:
        parts[2] = "DATE"
        parts[3] = "HH:MM"
    return "|".join(parts)


def split_book_state_messages(line: str, *, limit: int = DISCORD_CONTENT_LIMIT) -> list[str]:
    """
    Unsplit if it fits. Otherwise BOOK_STATE_NofM|w=<unix>|<chunk>.

    `w` ties parts of one write together so a failed 1of2 cannot splice
    onto an older 2of2.
    """
    text = str(line or "")
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    w = str(int(time.time()))
    prefix_len = len(f"BOOK_STATE_99of99|w={w}|")
    chunk = max(200, limit - prefix_len)
    pieces = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    n = len(pieces)
    return [f"BOOK_STATE_{i}of{n}|w={w}|{piece}" for i, piece in enumerate(pieces, 1)]


def _parse_labeled(token: str) -> tuple[str, str] | None:
    token = token.strip()
    if not token:
        return None
    if " " not in token:
        return None
    key, val = token.split(" ", 1)
    return key.strip(), val.strip()


def _parse_pos_segment(seg: str) -> dict[str, Any] | None:
    raw = seg.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if not raw.startswith("POS"):
        return None
    bits = raw.split("~")
    if len(bits) < 6:
        return None
    # POS ticker C/P strike exp qtyN
    ticker = bits[1].strip().upper() if len(bits) > 1 else "?"
    cp = bits[2].strip().upper() if len(bits) > 2 else "C"
    strike = _f(bits[3]) if len(bits) > 3 else None
    exp = bits[4].strip() if len(bits) > 4 else None
    qty_tok = bits[5].strip() if len(bits) > 5 else "qty1"
    qty = 1
    m = re.match(r"qty(\d+)", qty_tok, re.I)
    if m:
        qty = int(m.group(1))
    labeled: dict[str, str] = {}
    start = 6
    # qty might be labeled if an older line used qty 1
    if not m and bits[5:]:
        parsed = _parse_labeled(bits[5])
        if parsed:
            labeled[parsed[0]] = parsed[1]
            start = 6
        else:
            start = 6
    for bit in bits[start:]:
        parsed = _parse_labeled(bit)
        if parsed:
            labeled[parsed[0]] = parsed[1]
    opened = labeled.get("opened")
    if opened == _NA:
        opened = None
    trailing_raw = labeled.get("trailing")
    trailing = None if not trailing_raw or trailing_raw == _NA else _f(trailing_raw)
    unmarked = labeled.get("unmarked_since")
    if unmarked == _NA:
        unmarked = None
    last_mark_at = labeled.get("last_mark_at")
    if last_mark_at == _NA:
        last_mark_at = None
    trade_id = labeled.get("trade_id")
    if not trade_id or trade_id == _NA:
        trade_id = str(uuid.uuid4())
    direction = _direction_from_cp(cp)
    entry_mid = _f(labeled.get("entry_mid"))
    entry_price = _f(labeled.get("entry_price")) or entry_mid
    entry_ask = _f(labeled.get("entry_ask"))
    entry_spot = _f(labeled.get("entry_spot"))
    stop_entry = _f(labeled.get("stop_entry_spot")) or entry_spot
    delta = _f(labeled.get("delta"))
    last_bid = _f(labeled.get("last_bid"))
    last_ask = _f(labeled.get("last_ask"))
    fill_est = str(labeled.get("fill_est") or "0").strip() in ("1", "true", "True")
    delta_est = str(labeled.get("delta_est") or "0").strip() in ("1", "true", "True")
    entry_dte = _f(labeled.get("entry_dte"))
    sl = _f(labeled.get("sl"))
    tp = _f(labeled.get("tp"))
    option_contract = {
        "direction": direction,
        "strike": strike,
        "expiration": exp if exp and exp != _NA else None,
        "quantity": qty,
        "days_to_expiration": entry_dte,
        "delta": delta,
        "spot": stop_entry,
        "bid": last_bid,
        "ask": last_ask or entry_ask,
    }
    trade: dict[str, Any] = {
        "trade_id": trade_id,
        "ticker": ticker,
        "direction": direction,
        "strike": strike,
        "expiration": exp if exp and exp != _NA else None,
        "quantity": qty,
        "entry_price": entry_price,
        "entry_premium": entry_price,
        "entry_mid": entry_mid if entry_mid is not None else entry_price,
        "entry_ask": entry_ask if entry_ask is not None else entry_mid,
        "bid": last_bid,
        "ask": last_ask if last_ask is not None else entry_ask,
        "entry_timestamp": opened,
        "entry_time": opened,
        "stop_loss": sl,
        "take_profit": tp,
        "target_price": tp,
        "trailing_stop": trailing,
        "entry_score": _f(labeled.get("entry_score")),
        "entry_pivot": _f(labeled.get("entry_pivot")),
        "entry_spot": entry_spot,
        "entry_dte": entry_dte,
        "entry_calendar_dte": entry_dte,
        "peak_pnl_pct": _f(labeled.get("peak_pnl")),
        "trough_pnl_pct": _f(labeled.get("trough_pnl")),
        "stop_spot": _f(labeled.get("stop_spot")),
        "target_spot": _f(labeled.get("target_spot")),
        "stop_entry_spot": stop_entry,
        "delta": delta,
        "underlying_delta": delta,
        "underlying_delta_est": delta_est,
        "spot": stop_entry,
        "last_mark": _f(labeled.get("last_mark")),
        "last_bid": last_bid,
        "last_ask": last_ask,
        "last_mark_at": last_mark_at,
        "last_live_score": _f(labeled.get("last_live_score")),
        "last_spot": _f(labeled.get("last_spot")),
        "fill_est": fill_est,
        "thesis_below_streak": _i(labeled.get("thesis_below")) or 0,
        "mark_fail_streak": _i(labeled.get("mark_fail_streak")) or 0,
        "unmarked_since": unmarked,
        "option_contract": option_contract,
    }
    return trade


def parse_book_state_line(line: str) -> dict[str, Any] | None:
    """Parse a complete BOOK_STATE|... line (already assembled if split)."""
    text = str(line or "").strip()
    text = text.replace("```", "").strip()
    if not text:
        return None
    # Numbered leftover — caller should assemble first.
    if re.match(r"^BOOK_STATE_\d+of\d+\|", text):
        return None
    if not text.startswith("BOOK_STATE"):
        # tolerate a leading mention / junk before the token
        idx = text.find("BOOK_STATE|")
        if idx < 0:
            return None
        text = text[idx:]
    parts = text.split("|")
    if len(parts) < 5:
        return None
    version = parts[1].strip() if len(parts) > 1 else ""
    day = parts[2].strip() if len(parts) > 2 else ""
    hhmm = parts[3].strip() if len(parts) > 3 else ""
    header: dict[str, Any] = {}
    trades: list[dict[str, Any]] = []
    for tok in parts[4:]:
        t = tok.strip()
        if not t:
            continue
        if t.startswith("POS"):
            pos = _parse_pos_segment(t)
            if pos:
                trades.append(pos)
            continue
        labeled = _parse_labeled(t)
        if not labeled:
            continue
        key, val = labeled
        header[key] = val
    return {
        "kind": "BOOK_STATE",
        "version": version,
        "date": day,
        "time": hhmm,
        "equity_fill": _f(header.get("equity_fill")),
        "bp": _f(header.get("bp")),
        "realized_cum": _f(header.get("realized_cum")),
        "realized_mid_cum": _f(header.get("realized_mid_cum")),
        "sessions_elapsed": _i(header.get("sessions_elapsed")) or 0,
        "positions": _i(header.get("positions")) if header.get("positions") is not None else len(trades),
        "peak_deployed": _f(header.get("peak_deployed")),
        "scans": _i(header.get("scans")),
        "entries": _i(header.get("entries")),
        "closes": _i(header.get("closes")),
        "criticals": _i(header.get("criticals")),
        "day_realized_fill": _f(header.get("day_realized_fill")),
        "day_realized_mid": _f(header.get("day_realized_mid")),
        "planned_risk_closed": _f(header.get("planned_risk_closed")),
        "spy_open": _f(header.get("spy_open")),
        "spy_high": _f(header.get("spy_high")),
        "spy_low": _f(header.get("spy_low")),
        "spy_close": _f(header.get("spy_close")),
        "session_date": header.get("session_date") if header.get("session_date") != _NA else day,
        "gate_exits": _parse_gate_exits(header.get("gate_exits")),
        "eod_done": header.get("eod_done") if header.get("eod_done") != _NA else None,
        "carry_done": header.get("carry_done") if header.get("carry_done") != _NA else None,
        "eod_book_done": header.get("eod_book_done") if header.get("eod_book_done") != _NA else None,
        "trades": trades,
        "raw": text,
    }


_PART_RE = re.compile(
    r"^BOOK_STATE_(\d+)of(\d+)\|w=([^|]+)\|(.*)$",
    re.DOTALL,
)
_PART_RE_NO_W = re.compile(
    r"^BOOK_STATE_(\d+)of(\d+)\|(.*)$",
    re.DOTALL,
)


def select_latest_book_state(contents: list[str]) -> str | None:
    """
    Pick the newest complete BOOK_STATE from message contents (newest first).

    Assembles BOOK_STATE_NofM|w=<id>| parts that share `w`. Incomplete
    newest sets are skipped in favor of the next complete snapshot.
    """
    groups: dict[str, dict[int, tuple[int, str]]] = {}
    order: list[str] = []
    for raw in contents:
        text = str(raw or "").strip().replace("```", "").strip()
        if not text:
            continue
        # Unsplit wins immediately (newest complete).
        if text.startswith("BOOK_STATE|") or text.startswith("BOOK_STATE "):
            parsed = parse_book_state_line(text)
            if parsed:
                return parsed["raw"]
            idx = text.find("BOOK_STATE|")
            if idx >= 0:
                parsed = parse_book_state_line(text[idx:])
                if parsed:
                    return parsed["raw"]
            continue
        m = _PART_RE.match(text)
        if m:
            n, total, wid, payload = (
                int(m.group(1)),
                int(m.group(2)),
                m.group(3),
                m.group(4),
            )
            key = f"w:{wid}:{total}"
        else:
            m2 = _PART_RE_NO_W.match(text)
            if not m2:
                continue
            n, total, payload = int(m2.group(1)), int(m2.group(2)), m2.group(3)
            key = f"now:{total}"
        if key not in groups:
            groups[key] = {}
            order.append(key)
        groups[key][n] = (total, payload)
        got = groups[key]
        if len(got) == total and all(i in got for i in range(1, total + 1)):
            assembled = "".join(got[i][1] for i in range(1, total + 1))
            parsed = parse_book_state_line(assembled)
            if parsed:
                return parsed["raw"]
    return None


def positions_until_split(
    sample_pos: str | None = None,
    *,
    limit: int = DISCORD_CONTENT_LIMIT,
) -> int:
    """Smallest position count at which a typical BOOK_STATE exceeds `limit`."""
    header = (
        "BOOK_STATE|v741584e|2026-09-15|15:00|equity_fill 10203|bp 7004|"
        "realized_cum +213|realized_mid_cum +250|sessions_elapsed 3|positions 0|"
        "peak_deployed 1400|scans 8|entries 2|closes 1|criticals 0|"
        "day_realized_fill +12|day_realized_mid +15|planned_risk_closed 150|"
        "spy_open 580.10|spy_high 582.00|spy_low 578.00|spy_close 581.00|"
        "session_date 2026-09-15"
    )
    pos = sample_pos or _TYPICAL_POS_FOR_SPLIT
    n = 1
    while n <= 20:
        line = header + "".join("|" + pos for _ in range(n))
        # rewrite positions N
        line = re.sub(r"positions \d+", f"positions {n}", line, count=1)
        if len(line) > limit:
            return n
        n += 1
    return n


# ---------------------------------------------------------------------------
# Discord I/O
# ---------------------------------------------------------------------------

def _webhook_url() -> str:
    return (
        getattr(config, "DISCORD_WEBHOOK", "")
        or os.environ.get("DISCORD_WEBHOOK", "")
        or ""
    ).strip()


def _bot_token() -> str:
    return (
        getattr(config, "DISCORD_BOT_TOKEN", "")
        or os.environ.get("DISCORD_BOT_TOKEN", "")
        or ""
    ).strip()


def _channel_id_env() -> str:
    return (
        getattr(config, "DISCORD_CHANNEL_ID", "")
        or os.environ.get("DISCORD_CHANNEL_ID", "")
        or ""
    ).strip()


def resolve_channel_id(session: requests.Session | None = None) -> str | None:
    env_id = _channel_id_env()
    if env_id:
        return env_id
    url = _webhook_url()
    if not url:
        return None
    try:
        sess = session or requests
        resp = sess.get(url, timeout=HTTP_TIMEOUT_S)
        if resp.status_code not in (200, 201):
            print(
                f"[restore] webhook lookup HTTP {resp.status_code}: "
                f"{resp.text[:180]}"
            )
            return None
        data = resp.json()
        cid = str(data.get("channel_id") or "").strip()
        return cid or None
    except Exception as e:
        print(f"[restore] webhook channel lookup failed: {e}")
        return None


def fetch_channel_message_contents(
    *,
    token: str | None = None,
    channel_id: str | None = None,
    max_pages: int = FETCH_MAX_PAGES,
    budget_s: float = FETCH_BUDGET_S,
    session: requests.Session | None = None,
) -> list[str]:
    """Newest-first message contents. Stops at budget or max_pages."""
    tok = (token or _bot_token()).strip()
    cid = (channel_id or resolve_channel_id(session) or "").strip()
    if not tok:
        print("[restore] DISCORD_BOT_TOKEN missing — cannot read channel history")
        return []
    if not cid:
        print("[restore] DISCORD_CHANNEL_ID missing and webhook lookup failed")
        return []
    headers = {
        "Authorization": f"Bot {tok}",
        "User-Agent": "hedgefund-book-state/1.0",
    }
    sess = session or requests
    out: list[str] = []
    before = None
    t0 = time.monotonic()
    for page in range(max_pages):
        if time.monotonic() - t0 > budget_s:
            print(f"[restore] history fetch budget {budget_s:.0f}s hit at page {page}")
            break
        params: dict[str, Any] = {"limit": FETCH_PAGE_SIZE}
        if before:
            params["before"] = before
        url = f"{DISCORD_API}/channels/{cid}/messages"
        try:
            resp = sess.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT_S)
        except Exception as e:
            print(f"[restore] GET messages failed page {page}: {e}")
            break
        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 1.0))
            except Exception:
                wait = 1.0
            time.sleep(min(wait, 5.0) + 0.2)
            continue
        if resp.status_code != 200:
            print(
                f"[restore] GET messages HTTP {resp.status_code}: "
                f"{resp.text[:180]}"
            )
            break
        try:
            batch = resp.json()
        except Exception as e:
            print(f"[restore] messages JSON failed: {e}")
            break
        if not isinstance(batch, list) or not batch:
            break
        for msg in batch:
            if isinstance(msg, dict):
                content = msg.get("content") or ""
                if content:
                    out.append(str(content))
        before = batch[-1].get("id") if isinstance(batch[-1], dict) else None
        if not before:
            break
        if len(batch) < FETCH_PAGE_SIZE:
            break
        time.sleep(FETCH_PAGE_SLEEP_S)
    return out


def fetch_latest_book_state(
    *,
    contents: list[str] | None = None,
    session: requests.Session | None = None,
) -> str | None:
    bootstrap = (
        os.environ.get("BOOK_STATE_BOOTSTRAP", "")
        or getattr(config, "BOOK_STATE_BOOTSTRAP", "")
        or ""
    ).strip()
    if contents is None:
        contents = fetch_channel_message_contents(session=session)
    line = select_latest_book_state(contents or [])
    if line:
        return line
    if bootstrap:
        print("[restore] no Discord BOOK_STATE; using BOOK_STATE_BOOTSTRAP")
        return bootstrap
    return None


def _post_book_state_messages(messages: list[str]) -> bool:
    if not messages:
        return False
    try:
        import broadcaster
    except Exception as e:
        print(f"[book_state] broadcaster import failed: {e}")
        return False
    ok = True
    for i, msg in enumerate(messages):
        try:
            if len(msg) <= getattr(broadcaster, "MAX_CHUNK", 1900):
                delivered = broadcaster.send_discord_alert(msg)
            else:
                delivered = broadcaster._post_chunk(msg)
        except Exception as e:
            print(f"[book_state] post failed chunk {i + 1}/{len(messages)}: {e}")
            delivered = False
        if not delivered:
            ok = False
        if i < len(messages) - 1:
            time.sleep(0.6)
    return ok


def emit_book_state(
    reason: str = "manual",
    *,
    now: datetime | None = None,
    trades: list[dict[str, Any]] | None = None,
    force: bool = False,
) -> str | None:
    """
    Post BOOK_STATE to Discord. Skip if identical to last emit unless
    reason is eod/shutdown or force=True.
    """
    global _last_emitted_line, _shutdown_emitted, _sessions_elapsed
    if _restoring:
        return None
    increment = reason == "eod"
    try:
        line = format_book_state_line(
            trades=trades, now=now, increment_session=increment
        )
    except Exception as e:
        print(f"[book_state] format failed ({reason}): {e}")
        return None
    try:
        with _lock:
            if (
                not force
                and reason not in ("eod", "shutdown")
                and _last_emitted_line is not None
                and _line_fingerprint(line) == _line_fingerprint(_last_emitted_line)
            ):
                return line
            if reason == "shutdown" and _shutdown_emitted:
                return _last_emitted_line
            chunks = split_book_state_messages(line)
        ok = _post_book_state_messages(chunks)
        with _lock:
            _last_emitted_line = line
            if reason == "shutdown":
                _shutdown_emitted = True
            if increment:
                day, _hh = _header_now(now)
                if day not in _eod_counted_dates:
                    _eod_counted_dates.add(day)
                    _sessions_elapsed = int(_sessions_elapsed) + 1
        npos = line.count("|POS~")
        print(
            f"[book_state] emit reason={reason} ok={ok} chars={len(line)} "
            f"parts={len(chunks)} positions={npos}"
        )
        if len(chunks) > 1:
            print(
                f"[book_state] split {len(chunks)} Discord messages "
                f"(limit {DISCORD_CONTENT_LIMIT})"
            )
        return line
    except Exception as e:
        print(f"[book_state] emit failed ({reason}): {e}")
        return line


def emit_shutdown_book_state() -> None:
    try:
        emit_book_state(reason="shutdown", force=True)
    except Exception as e:
        print(f"[book_state] shutdown emit failed: {e}")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _confirm_stale_value() -> str:
    return (
        os.environ.get("CONFIRM_STALE_BOOK", "")
        or getattr(config, "CONFIRM_STALE_BOOK", "")
        or ""
    ).strip()


def _is_stale(snap: dict[str, Any], *, today: datetime | None = None) -> bool:
    day_s = str(snap.get("date") or "").strip()
    if not day_s:
        return True
    try:
        snap_day = datetime.strptime(day_s[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    now = _chicago_now(today).date()
    return (now - snap_day) > timedelta(days=STALE_CALENDAR_DAYS)


def _critical(msg: str) -> None:
    print(msg)
    try:
        import broadcaster
        broadcaster.send_discord_alert(msg)
    except Exception as e:
        print(f"[restore] CRITICAL Discord send failed: {e}")


def _apply_snapshot(snap: dict[str, Any], *, now: datetime | None = None) -> None:
    global _sessions_elapsed, _last_snapshot
    trades = list(snap.get("trades") or [])
    try:
        from tracker_agent import replace_active_trades
        ok = replace_active_trades(trades)
        if not ok:
            print("[restore] WARNING: replace_active_trades returned False")
    except Exception as e:
        print(f"[restore] replace_active_trades failed: {e}")
    bp = snap.get("bp")
    if bp is None:
        bp = virtual_broker.starting_buying_power()
    realized_fill = snap.get("realized_cum")
    if realized_fill is None:
        realized_fill = 0.0
    realized_mid = snap.get("realized_mid_cum")
    if realized_mid is None:
        # Reconstruct mid realized from bp + open cost - start when missing.
        start = virtual_broker.starting_buying_power()
        open_cost = 0.0
        for t in trades:
            try:
                entry = float(t.get("entry_price") or t.get("entry_mid") or 0.0)
            except (TypeError, ValueError):
                entry = 0.0
            qty = virtual_broker.resolve_quantity(t)
            open_cost += entry * 100.0 * qty
        realized_mid = float(bp) - start + open_cost
    virtual_broker.restore_ledger_snapshot(
        buying_power=float(bp),
        realized_mid=float(realized_mid),
        realized_fill=float(realized_fill),
    )
    if snap.get("peak_deployed") is not None or snap.get("session_date"):
        virtual_broker.restore_session_book(
            session_date=snap.get("session_date") or snap.get("date"),
            start_realized_fill=(
                float(realized_fill) - float(snap.get("day_realized_fill") or 0.0)
            ),
            start_realized=(
                float(realized_mid) - float(snap.get("day_realized_mid") or 0.0)
            ),
            peak_deployed=float(snap.get("peak_deployed") or 0.0),
        )
    fill_accounting.restore_session_snapshot(
        {
            "session_date": snap.get("session_date") or snap.get("date"),
            "scans": snap.get("scans") or 0,
            "entries": snap.get("entries") or 0,
            "closes": snap.get("closes") or 0,
            "criticals": snap.get("criticals") or 0,
            "realized_mid": snap.get("day_realized_mid") or 0.0,
            "realized_fill": snap.get("day_realized_fill") or 0.0,
            "planned_risk_closed": snap.get("planned_risk_closed") or 0.0,
            "spy_open": snap.get("spy_open"),
            "spy_high": snap.get("spy_high"),
            "spy_low": snap.get("spy_low"),
            "spy_close": snap.get("spy_close"),
        }
    )
    try:
        import signal_gate
        gate = signal_gate.get_gate()
        open_tickers = [t.get("ticker") for t in trades if t.get("ticker")]
        gate.sync_open_from_book(open_tickers)
        for t in trades:
            ticker = str(t.get("ticker") or "").upper().strip()
            if not ticker:
                continue
            opened = _parse_iso_dt(t.get("entry_timestamp") or t.get("entry_time"))
            if opened is not None:
                gate._st(ticker).last_entry_at = opened
        exits = snap.get("gate_exits") or {}
        if isinstance(exits, dict):
            with _lock:
                _gate_exits.clear()
                _gate_exits.update(exits)
            for ticker, ts in exits.items():
                dt = _parse_iso_dt(ts)
                if dt is None:
                    continue
                gate._st(str(ticker).upper()).last_exit_at = dt
        print(
            f"[restore] gate sync_open_from_book: {len(open_tickers)} open "
            f"({', '.join(str(t) for t in open_tickers) or 'none'}) "
            f"exits={len(exits) if isinstance(exits, dict) else 0}"
        )
    except Exception as e:
        print(f"[restore] gate sync_open_from_book warn: {e}")
    try:
        import position_exits as pex
        sess = _chicago_now(now).date()
        today = sess.isoformat()
        if snap.get("eod_done") == today:
            pex.mark_eod_done(sess)
            print(f"[restore] eod_done {today} — will not flatten again today")
        if snap.get("carry_done") == today:
            pex.mark_carry_review_done(sess)
            print(f"[restore] carry_done {today} — will not carry-review again today")
        if snap.get("eod_book_done") == today:
            pex.mark_eod_book_done(sess)
            print(f"[restore] eod_book_done {today} — will not emit SESSION again today")
    except Exception as e:
        print(f"[restore] day-flag restore warn: {e}")
    set_sessions_elapsed(int(snap.get("sessions_elapsed") or 0))
    _last_snapshot = snap


def _start_flat_unrecovered(why: str) -> None:
    global _unrecovered, _trading_blocked, _block_reason
    _unrecovered = True
    _trading_blocked = False
    _block_reason = None
    try:
        from tracker_agent import replace_active_trades
        replace_active_trades([])
    except Exception as e:
        print(f"[restore] clear open book failed: {e}")
    try:
        seed = virtual_broker.starting_buying_power()
        virtual_broker.restore_ledger_snapshot(
            buying_power=seed, realized_mid=0.0, realized_fill=0.0
        )
    except Exception as e:
        print(f"[restore] seed ledger failed: {e}")
    set_sessions_elapsed(0)
    _critical(
        "🚨 **CRITICAL: BOOK_STATE NOT RECOVERED**\n"
        f"{why}\n"
        "Starting FLAT at $10,000 / 0 positions.\n"
        "This is NOT a valid measurement result — state was not recovered.\n"
        "If this is the first boot of BOOK_STATE, set BOOK_STATE_BOOTSTRAP "
        "to a reconstructed line or continue knowing the book is empty."
    )


def restore_at_boot(
    *,
    contents: list[str] | None = None,
    now: datetime | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Before the first scan: pull the newest BOOK_STATE and rebuild the book.

    Once per process. Returns a status dict.
    """
    global _restore_done, _restoring, _trading_blocked, _block_reason
    global _unrecovered, _version_mismatch, _last_snapshot
    with _lock:
        if _restore_done:
            return {
                "ok": True,
                "already": True,
                "blocked": _trading_blocked,
                "unrecovered": _unrecovered,
                "snapshot": _last_snapshot,
            }
        _restore_done = True
        _restoring = True
    result: dict[str, Any] = {
        "ok": False,
        "already": False,
        "blocked": False,
        "unrecovered": False,
        "stale": False,
        "version_mismatch": False,
        "snapshot": None,
    }
    try:
        if bool(getattr(config, "RESET_LEDGER_ON_BOOT", False)):
            print(
                "[restore] RESET_LEDGER_ON_BOOT is true — skipping Discord "
                "BOOK_STATE restore (ledger was just wiped)."
            )
            result["ok"] = True
            result["skipped_reset"] = True
            return result
        line = fetch_latest_book_state(contents=contents, session=session)
        if not line:
            why = (
                "No BOOK_STATE line found in Discord history "
                f"(searched up to {FETCH_MAX_PAGES} pages / {FETCH_BUDGET_S:.0f}s)"
                + (
                    ""
                    if _bot_token()
                    else " — DISCORD_BOT_TOKEN is not set, so history could not be read"
                )
            )
            _start_flat_unrecovered(why)
            result["unrecovered"] = True
            result["ok"] = True
            return result
        snap = parse_book_state_line(line)
        if not snap:
            _start_flat_unrecovered(
                "Most recent BOOK_STATE line failed to parse."
            )
            result["unrecovered"] = True
            result["ok"] = True
            return result
        running_ver = fill_accounting.code_version()
        snap_ver = str(snap.get("version") or "")
        if snap_ver and running_ver and snap_ver != running_ver and snap_ver != "vn/a":
            _version_mismatch = True
            result["version_mismatch"] = True
            _critical(
                "🚨 **CRITICAL: BOOK_STATE VERSION MISMATCH**\n"
                f"Snapshot written by `{snap_ver}`, this process is `{running_ver}`.\n"
                "Restoring anyway so carry lots are not dropped. Confirm the "
                "field layout still matches before trusting exits."
            )
        stale = _is_stale(snap, today=now)
        result["stale"] = stale
        _apply_snapshot(snap, now=now)
        npos = len(snap.get("trades") or [])
        equity = snap.get("equity_fill")
        equity_s = f"{int(round(equity))}" if equity is not None else "n/a"
        sess_n = current_session_number()
        print(
            f"[restore] BOOK_STATE {snap.get('date')} {snap.get('time')} "
            f"-> {npos} positions, equity {equity_s}, session {sess_n}"
        )
        result["snapshot"] = snap
        result["ok"] = True
        if stale:
            confirm = _confirm_stale_value()
            want = str(snap.get("date") or "")
            if confirm and confirm in (want, "1", "true", "yes", "on"):
                print(
                    f"[restore] stale BOOK_STATE {want} confirmed via "
                    f"CONFIRM_STALE_BOOK={confirm} — trading allowed"
                )
            else:
                _trading_blocked = True
                _block_reason = (
                    f"BOOK_STATE {snap.get('date')} {snap.get('time')} is older "
                    f"than {STALE_CALENDAR_DAYS} calendar days"
                )
                result["blocked"] = True
                _critical(
                    "🚨 **CRITICAL: BOOK_STATE STALE**\n"
                    f"Most recent BOOK_STATE is {snap.get('date')} {snap.get('time')} "
                    f"(older than {STALE_CALENDAR_DAYS} calendar days).\n"
                    f"Restored {npos} positions / equity {equity_s} for inspection.\n"
                    "Trading is HALTED until you set "
                    f"`CONFIRM_STALE_BOOK={want}` (or `1`) and restart."
                )
        return result
    except Exception as e:
        print(f"[restore] restore_at_boot crashed: {e}")
        _start_flat_unrecovered(f"restore_at_boot raised: {e}")
        result["unrecovered"] = True
        result["ok"] = False
        result["error"] = str(e)
        return result
    finally:
        _restoring = False
        _unrecovered = bool(result.get("unrecovered"))
        result["unrecovered"] = _unrecovered
        result["blocked"] = _trading_blocked
