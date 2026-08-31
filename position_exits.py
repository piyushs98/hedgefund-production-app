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
  Thesis void: live score < THESIS_EXIT_SCORE (default 55) AND entry_score
      >= EXECUTE_THRESHOLD (70) — carry review and every exit pass, P&L ignored
  Earnings flatten: open lot whose expiry spans a print, once the session
      is inside the blackout window (EARNINGS_FLATTEN_SPANNING, default on)
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
import fill_accounting
import virtual_broker

try:
    import pytz
except ImportError:  # pragma: no cover
    pytz = None

# Once-per-process-day flags (Chicago session date).
_eod_flatten_dates: set[str] = set()
_carry_review_dates: set[str] = set()
_eod_book_dates: set[str] = set()


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
    """Test helper — clear in-process EOD / carry-review / BOOK day sets."""
    _eod_flatten_dates.clear()
    _carry_review_dates.clear()
    _eod_book_dates.clear()
    try:
        virtual_broker.reset_book_for_tests()
    except Exception:
        pass


def eod_book_already_done(session_day: date | None = None) -> bool:
    day = session_day or _chicago_now().date()
    return day.isoformat() in _eod_book_dates


def mark_eod_book_done(session_day: date | None = None) -> None:
    day = session_day or _chicago_now().date()
    _eod_book_dates.add(day.isoformat())


def maybe_emit_eod_book(now_cdt: datetime | None = None) -> str | None:
    """
    Once per Chicago session, at/after 14:45 flatten: Discord BOOK + SESSION.

    BOOK equity and SESSION realized/equity are the FILL series.
    Open value is BID. SESSION supplements BOOK; both fire.
    """
    now = _chicago_now(now_cdt)
    if not is_eod_flatten_window(now):
        return None
    if eod_book_already_done(now.date()):
        return None
    try:
        line = virtual_broker.format_book_line()
    except Exception as e:
        print(f"[Exits] BOOK line failed: {e}")
        return None
    try:
        peak = float(virtual_broker._book.get("peak_deployed") or 0.0)
        peak = max(peak, virtual_broker._deployed_from_open_trades())
        port = virtual_broker.get_portfolio()
        session_line = fill_accounting.format_session_line(
            equity_fill=virtual_broker.fill_equity(),
            peak_deployed=peak,
            open_value=virtual_broker.open_mark_value(),
            bp=port.get("buying_power"),
            now=now,
        )
    except Exception as e:
        print(f"[Exits] SESSION line failed: {e}")
        session_line = None
    mark_eod_book_done(now.date())
    print(f"[Exits] {line}")
    if session_line:
        print(f"[Exits] {session_line}")
    try:
        import broadcaster
        payload = line if not session_line else f"{line}\n{session_line}"
        broadcaster.send_discord_alert(payload)
    except Exception as e:
        print(f"[Exits] BOOK Discord warn: {e}")
    return line


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
    live_score: float | None = None,
    card: Any = None,
    options_dict: dict[str, Any] | None = None,
) -> tuple[str | None, float | None]:
    """
    Shared exit decision for scan exits and morning carry review.

    EOD is selective: only cal_dte < CARRY_MIN_DTE is forced flat.
    Morning carry review typically sets include_time_stop=False.
    THESIS_VOID fires after SL/TP (so a hit target still banks) and
    before B5 path rules. It requires a clean score and two consecutive
    clean prints below THESIS_EXIT_SCORE. SL/TP/expiry always run.
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

    # Earnings: flatten lots whose expiry spans the print, once inside
    # the blackout window. Off when EARNINGS_FLATTEN_SPANNING is false.
    try:
        import earnings_blackout
        if earnings_blackout.should_flatten_trade(trade, sess):
            if exit_px is None and entry is not None:
                exit_px = entry
            return "EARNINGS_FLATTEN", exit_px
    except Exception:
        pass

    if mark is None:
        return None, None

    sl = _f(trade.get("stop_loss"))
    tp = _f(trade.get("take_profit") or trade.get("target_price"))
    if _sl_breached(mark, sl):
        return "STOP_LOSS", mark
    if _tp_breached(mark, tp):
        return "TAKE_PROFIT", mark

    thesis = _thesis_void_reason(
        trade, live_score, card=card, options_dict=options_dict
    )
    if thesis:
        return thesis, mark

    pnl = _pnl_pct(entry, mark)
    # Path rules (optional time stop with score exempt)
    if include_time_stop:
        b5, skip_log = _b5_exit_reason(
            trade, mark, entry, pnl, datetime.now(timezone.utc), live_score=live_score
        )
        if skip_log:
            # Stash for run_scan_exits summary (caller may also log immediately)
            trade["_time_stop_skip_log"] = skip_log
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

        live_score = _live_score_from_card(card)
        if live_score is not None:
            trade["last_live_score"] = live_score
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
                prev_trough = _f(trade.get("trough_pnl_pct"))
                if prev_trough is None or pnl < prev_trough:
                    trade["trough_pnl_pct"] = pnl
                trade["last_mark"] = mark
                trade["last_bid"] = mark_info.get("bid")
                trade["last_ask"] = mark_info.get("ask")
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
            live_score=live_score,
            card=card,
            options_dict=options_dict,
        )
        if reason is None and mark is None:
            u_reason, u_px = underlying_exit_reason(trade, mark_info.get("spot"))
            if u_reason and u_px is not None:
                reason, exit_px = u_reason, u_px

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

        thesis_skip = trade.pop("_thesis_skip_log", None)
        if thesis_skip:
            print(f"[CarryReview] {thesis_skip}")
            summary.setdefault("thesis_skipped", []).append(thesis_skip)
            try:
                from tracker_agent import save_active_trade
                save_active_trade(trade)
            except Exception:
                pass

        if reason and exit_px is not None:
            closed = close_open_position(trade, float(exit_px), f"CARRY_{reason}")
            summary["closed"].append(closed)
            action = f"CLOSE {reason}"
        else:
            summary["held"].append(ticker)
            action = "HOLD"
            if mark is None:
                action = "HOLD (no mark)"
            if thesis_skip:
                action = thesis_skip.split(" — ")[0] if "THESIS_" in thesis_skip else action

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


def _trade_delta(trade: dict[str, Any]) -> tuple[float | None, bool]:
    """Return (|delta|, estimated). Missing → 0.50 estimated."""
    raw = trade.get("delta")
    if raw is None and isinstance(trade.get("option_contract"), dict):
        raw = trade["option_contract"].get("delta")
    if raw is None:
        raw = trade.get("underlying_delta")
    d = _f(raw)
    if d is None or abs(d) < 1e-6:
        return 0.50, True
    return abs(d), bool(trade.get("underlying_delta_est"))


def _stop_entry_spot(trade: dict[str, Any]) -> float | None:
    for key in ("stop_entry_spot", "spot"):
        v = _f(trade.get(key))
        if v is not None and v > 0:
            return v
    oc = trade.get("option_contract")
    if isinstance(oc, dict):
        v = _f(oc.get("spot"))
        if v is not None and v > 0:
            return v
    v = _f(trade.get("entry_spot"))
    if v is not None and v > 0:
        return v
    return None


def ensure_underlying_levels(trade: dict[str, Any]) -> dict[str, Any]:
    """
    stop_spot / target_spot from entry underlying and |delta|.

    calls: stop_spot = entry_spot - (entry_mid - SL) / |delta|
    puts:  stop_spot = entry_spot + (entry_mid - SL) / |delta|
    TP is the symmetric target_spot. Missing delta → 0.50, tagged estimated.
    Does not change SL/TP premium levels used when a mark exists.
    """
    if not isinstance(trade, dict):
        return {}
    if _f(trade.get("stop_spot")) is not None and _f(trade.get("target_spot")) is not None:
        return {
            "stop_spot": _f(trade.get("stop_spot")),
            "target_spot": _f(trade.get("target_spot")),
            "stop_entry_spot": _stop_entry_spot(trade),
            "underlying_delta": _trade_delta(trade)[0],
            "underlying_delta_est": bool(trade.get("underlying_delta_est")),
        }
    entry = _entry_premium(trade)
    sl = _f(trade.get("stop_loss"))
    tp = _f(trade.get("take_profit") or trade.get("target_price"))
    spot0 = _stop_entry_spot(trade)
    abs_d, est = _trade_delta(trade)
    is_put = "PUT" in _trade_direction(trade)
    stop_spot = None
    target_spot = None
    if spot0 is not None and entry is not None and abs_d > 1e-6:
        if sl is not None:
            risk = entry - sl
            if is_put:
                stop_spot = spot0 + risk / abs_d
            else:
                stop_spot = spot0 - risk / abs_d
        if tp is not None:
            reward = tp - entry
            if is_put:
                target_spot = spot0 - reward / abs_d
            else:
                target_spot = spot0 + reward / abs_d
    if stop_spot is not None:
        trade["stop_spot"] = round(stop_spot, 4)
    if target_spot is not None:
        trade["target_spot"] = round(target_spot, 4)
    if spot0 is not None:
        trade["stop_entry_spot"] = spot0
    trade["underlying_delta"] = abs_d
    trade["underlying_delta_est"] = est
    return {
        "stop_spot": stop_spot,
        "target_spot": target_spot,
        "stop_entry_spot": spot0,
        "underlying_delta": abs_d,
        "underlying_delta_est": est,
    }


def estimate_premium_from_spot(trade: dict[str, Any], spot: float) -> float | None:
    """Linear delta estimate. At exact stop_spot this equals SL."""
    entry = _entry_premium(trade)
    spot0 = _stop_entry_spot(trade)
    if entry is None or spot0 is None:
        return None
    abs_d, _est = _trade_delta(trade)
    signed = -abs_d if "PUT" in _trade_direction(trade) else abs_d
    est = entry + (float(spot) - spot0) * signed
    return max(0.01, round(est, 4))


def underlying_exit_reason(
    trade: dict[str, Any],
    spot: float | None,
) -> tuple[str | None, float | None]:
    """
    When the option mark is missing, fire SL/TP off the underlying.

    Returns (UNDERLYING_STOP|UNDERLYING_TAKE_PROFIT, estimated_premium) or (None, None).
    """
    px = _f(spot)
    if px is None or px <= 0:
        return None, None
    levels = ensure_underlying_levels(trade)
    stop_spot = _f(levels.get("stop_spot"))
    target_spot = _f(levels.get("target_spot"))
    is_put = "PUT" in _trade_direction(trade)
    prem = estimate_premium_from_spot(trade, px)
    if prem is None:
        prem = _f(trade.get("stop_loss")) or _entry_premium(trade)
    if stop_spot is not None:
        if is_put and px >= stop_spot:
            return "UNDERLYING_STOP", prem
        if (not is_put) and px <= stop_spot:
            return "UNDERLYING_STOP", prem
    if target_spot is not None:
        if is_put and px <= target_spot:
            return "UNDERLYING_TAKE_PROFIT", prem
        if (not is_put) and px >= target_spot:
            return "UNDERLYING_TAKE_PROFIT", prem
    return None, None


def _should_escalate(prev_alerted: int, streak: int) -> bool:
    """First alert, then only when streak doubles or hits a multiple of 10."""
    if streak <= 0:
        return False
    if prev_alerted <= 0:
        return True
    if streak >= prev_alerted * 2:
        return True
    if streak % 10 == 0 and streak > prev_alerted:
        return True
    return False


_DATA_FAIL_BLOCKS = frozenset({"no_pivot_data", "no_atr_data"})
_THESIS_DIRTY = frozenset({
    "no_liq_data",
    "no_momentum_data",
    "no_pivot_data",
    "no_atr_data",
})


def _live_score_from_card(card: Any) -> float | None:
    """Skip 0.0 costume scores from missing pivot/ATR — do not thesis-void."""
    if card is None:
        return None
    br = getattr(card, "block_reason", None)
    if isinstance(br, str) and br in _DATA_FAIL_BLOCKS:
        return None
    return _f(getattr(card, "total_score", None))


def score_is_clean_for_thesis(
    card: Any,
    options_dict: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """
    True only when pivot, ATR, pct_change, and a usable chain are present.

    Dirty: no_liq_data, no_momentum_data, no_pivot_data, no_atr_data, usable=0.
    dead_zone is a real measurement and is not dirty.
    """
    if card is None:
        return False, "no_score"
    br = getattr(card, "block_reason", None)
    if isinstance(br, str) and br in _THESIS_DIRTY:
        return False, br
    if isinstance(options_dict, dict) and "error" in options_dict:
        return False, "no_liq_data"
    metrics = getattr(card, "metrics", None)
    sub: dict[str, Any] = {}
    liq: dict[str, Any] = {}
    tech: dict[str, Any] = {}
    if isinstance(metrics, dict):
        sub = metrics.get("subscores") or {}
        liq = metrics.get("liquidity") or {}
        tech = metrics.get("technical") or {}
        if not isinstance(sub, dict):
            sub = {}
        if not isinstance(liq, dict):
            liq = {}
        if not isinstance(tech, dict):
            tech = {}
    liq_status = sub.get("liq_status") or liq.get("liq_status")
    if liq_status == "no_liq_data":
        return False, "no_liq_data"
    usable = sub.get("usable")
    if usable is None:
        usable = liq.get("usable_spread_quotes")
    if usable is not None:
        try:
            if int(usable) <= 0:
                return False, "usable=0"
        except (TypeError, ValueError):
            pass
    mom_status = sub.get("mom_status") or tech.get("mom_status")
    if mom_status == "no_momentum_data":
        return False, "no_momentum_data"
    if tech.get("atr_missing") is True or (
        isinstance(metrics, dict) and tech.get("atr") is None and tech.get("atr_abs") is None
        and "atr_distance" not in tech and "atr_distance" not in sub
    ):
        # Only dirty when technical metrics exist and ATR is explicitly missing.
        if isinstance(metrics, dict) and tech.get("atr_missing") is True:
            return False, "no_atr_data"
    if isinstance(metrics, dict) and tech:
        if tech.get("pivot") is None and tech.get("close") is None:
            return False, "no_pivot_data"
        if tech.get("pct_change") is None and mom_status == "no_momentum_data":
            return False, "no_momentum_data"
    return True, None


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
    if not isinstance(options_dict, dict):
        return out
    spot = _f(options_dict.get("current_price"))
    out["spot"] = spot
    if "error" in options_dict:
        return out

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


def stop_loss_slippage(
    trade: dict[str, Any],
    exit_price: float,
    pnl: float | None,
) -> dict[str, Any] | None:
    """
    Planned stop risk vs realized loss on a STOP_LOSS fill.

    planned_risk = (entry - SL) * 100 * qty   (dollars, positive)
    actual_loss  = -pnl                       (dollars, positive when losing)
    slippage_pct = (actual_loss - planned) / planned * 100
    """
    entry = _entry_premium(trade)
    sl = _f(trade.get("stop_loss"))
    qty = virtual_broker.resolve_quantity(trade)
    if entry is None or sl is None or entry <= 0:
        return None
    planned = (entry - sl) * 100.0 * qty
    if pnl is not None:
        try:
            actual = -float(pnl)
        except (TypeError, ValueError):
            actual = (entry - float(exit_price)) * 100.0 * qty
    else:
        try:
            actual = (entry - float(exit_price)) * 100.0 * qty
        except (TypeError, ValueError):
            return None
    slip = None
    if planned > 0:
        slip = (actual - planned) / planned * 100.0
    stop_pct = (entry - sl) / entry * 100.0
    atr5_pct = None
    delta_used = None
    coin_flip = None
    try:
        import ticker_desk as _td
        delta = trade.get("delta")
        if delta is None and isinstance(trade.get("option_contract"), dict):
            delta = trade["option_contract"].get("delta")
        ticker = trade.get("ticker")
        if ticker:
            atr5_pct, delta_used = _td.option_5m_atr_pct(entry, delta, ticker)
    except Exception:
        atr5_pct, delta_used = None, None
    if atr5_pct is not None and stop_pct is not None:
        coin_flip = stop_pct < (2.0 * float(atr5_pct))
    return {
        "planned_risk": round(planned, 2),
        "actual_loss": round(actual, 2),
        "slippage_pct": None if slip is None else round(slip, 1),
        "stop_pct": round(stop_pct, 1),
        "atr5_pct": None if atr5_pct is None else round(float(atr5_pct), 1),
        "atr5_delta": delta_used,
        "coin_flip": coin_flip,
        "quantity": qty,
    }


def format_closed_discord_line(closed: dict[str, Any]) -> str:
    """SCAN EXITS / EXIT PASS row, with stop-leak stats when present."""
    ticker = closed.get("ticker") or "?"
    reason = closed.get("reason") or "?"
    px = closed.get("exit_price")
    try:
        px_s = f"${float(px):g}" if px is not None else "?"
    except (TypeError, ValueError):
        px_s = str(px)
    if str(reason).startswith("UNDERLYING_"):
        try:
            px_s = f"~{float(px):.2f}" if px is not None else "?"
        except (TypeError, ValueError):
            px_s = str(px)
        spot = closed.get("spot")
        lvl_name = "stop_spot" if "STOP" in str(reason) else "target_spot"
        lvl = closed.get(lvl_name)
        extra = f"est, mark unavailable, spot {spot} vs {lvl_name} {lvl}"
        return f"**{ticker}** {reason} @ {px_s} ({extra})"
    line = f"**{ticker}** {reason} @ {px_s}"
    pnl = closed.get("pnl")
    if pnl is not None:
        try:
            line += f" PnL ${float(pnl):.0f}"
        except (TypeError, ValueError):
            pass
    if "STOP_LOSS" in str(reason) and closed.get("planned_risk") is not None:
        planned = float(closed["planned_risk"])
        slip = closed.get("slippage_pct")
        slip_s = f"{slip:.0f}%" if isinstance(slip, (int, float)) else "n/a"
        extra = f"planned ${-abs(planned):.0f}, slip {slip_s}"
        stop_pct = closed.get("stop_pct")
        atr5 = closed.get("atr5_pct")
        if stop_pct is not None:
            extra += f", stop={stop_pct:.0f}% of entry"
            if atr5 is not None:
                extra += f" vs 5m ATR {atr5:.0f}% (δ×und)"
                if closed.get("coin_flip"):
                    extra += ", coin_flip"
        line += f" ({extra})"
    return line


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
    qty = virtual_broker.resolve_quantity(trade)
    # Trigger price is mid (exit_price). Fill uses last bid / entry ask.
    # A stop still fires when mark <= SL; only the recorded fill changes.
    entry_ask = _f(trade.get("entry_ask"))
    if entry_ask is None:
        entry_ask = fill_accounting.extract_ask(trade)
    exit_bid = _f(trade.get("last_bid"))
    if exit_bid is None:
        exit_bid = fill_accounting.extract_bid(trade)
    # Underlying fallback is an estimated premium, not a bid.
    if "UNDERLYING_" in str(reason):
        exit_bid = None
    # TRADE records unrounded (bid+ask)/2 when present; debit/SL stay on
    # rounded entry_premium so sizing and stops do not change.
    record_mid = _f(trade.get("entry_mid"))
    if record_mid is None:
        record_mid = float(entry) if entry is not None else float(exit_price)
    quotes = fill_accounting.resolve_fill_prices(
        entry_mid=record_mid,
        exit_mid=float(exit_price),
        entry_ask=entry_ask,
        exit_bid=exit_bid,
    )
    result: dict[str, Any] = {
        "ticker": ticker,
        "reason": reason,
        "exit_price": exit_price,
        "entry_price": entry,
        "entry_mid": record_mid,
        "entry_ask": quotes["entry_ask"],
        "exit_mid": quotes["exit_mid"],
        "exit_bid": quotes["exit_bid"],
        "fill_est": bool(quotes.get("fill_est")),
        "quantity": qty,
        "ok": False,
    }
    try:
        sell = virtual_broker.paper_sell(
            trade,
            exit_price,
            direction,
            entry,
            notes=f"EXIT:{reason}",
            quantity=qty,
            exit_bid=quotes["exit_bid"],
            entry_ask=quotes["entry_ask"],
            fill_est=bool(quotes.get("fill_est")),
        )
        result["sell"] = sell
        result["ok"] = bool(sell.get("ok"))
        if sell.get("pnl") is not None:
            result["pnl"] = sell["pnl"]
        result["pnl_mid"] = sell.get("pnl_mid", sell.get("pnl"))
        result["pnl_fill"] = sell.get("pnl_fill")
        if "STOP_LOSS" in str(reason):
            stats = stop_loss_slippage(trade, exit_price, sell.get("pnl"))
            if stats:
                result.update(stats)
    except Exception as e:
        print(f"[Exits] paper_sell failed for {ticker}: {e}")
        result["sell_error"] = str(e)
        if "STOP_LOSS" in str(reason):
            stats = stop_loss_slippage(trade, exit_price, None)
            if stats:
                result.update(stats)

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

    if "STOP_LOSS" in str(reason) and result.get("planned_risk") is not None:
        slip = result.get("slippage_pct")
        slip_s = f"{slip:.0f}%" if isinstance(slip, (int, float)) else "n/a"
        pnl_s = result.get("pnl")
        try:
            pnl_s = f"{float(pnl_s):.0f}"
        except (TypeError, ValueError):
            pnl_s = str(pnl_s)
        noise = ""
        if result.get("stop_pct") is not None:
            noise = f", stop={result['stop_pct']:.0f}% of entry"
            if result.get("atr5_pct") is not None:
                noise += f" vs 5m ATR {result['atr5_pct']:.0f}% (δ×und)"
                if result.get("coin_flip"):
                    noise += ", coin_flip"
        print(
            f"[Exits] {ticker} STOP_LOSS @ {exit_price:g} "
            f"PnL {pnl_s} (planned {-abs(float(result['planned_risk'])):.0f}, "
            f"slip {slip_s}{noise})"
        )
    print(
        f"[Exits] CLOSED {ticker} reason={reason} "
        f"exit=${exit_price:.4f} entry={entry} qty={qty} ok={result.get('ok')}"
        f" pnl_mid={result.get('pnl_mid')} pnl_fill={result.get('pnl_fill')}"
        f"{' fill=est' if result.get('fill_est') else ''}"
    )
    try:
        planned = result.get("planned_risk")
        if planned is None:
            planned = fill_accounting.planned_risk_dollars(
                result.get("entry_mid") or entry,
                trade.get("stop_loss"),
                qty,
            )
            if planned is not None:
                result["planned_risk"] = planned
        trade_line = fill_accounting.format_trade_line(
            trade,
            reason=reason,
            entry_mid=result.get("entry_mid") or entry,
            entry_ask=result.get("entry_ask"),
            exit_mid=result.get("exit_mid") or exit_price,
            exit_bid=result.get("exit_bid"),
            qty=qty,
            pnl_mid=result.get("pnl_mid") if result.get("pnl_mid") is not None else result.get("pnl"),
            pnl_fill=result.get("pnl_fill"),
            planned_risk=planned,
            fill_est=bool(result.get("fill_est")),
        )
        result["trade_line"] = trade_line
        print(f"[Exits] {trade_line}")
        try:
            import broadcaster
            broadcaster.send_discord_alert(trade_line)
        except Exception as disc_err:
            print(f"[Exits] TRADE Discord warn: {disc_err}")
    except Exception as trade_err:
        print(f"[Exits] TRADE line failed for {ticker}: {trade_err}")
    return result


def _thesis_void_reason(
    trade: dict[str, Any],
    live_score: float | None,
    card: Any = None,
    options_dict: dict[str, Any] | None = None,
) -> str | None:
    """
    Score-based invalidation: close when the live thesis is gone, P&L ignored.

    Requires a high-conviction entry (entry_score >= EXECUTE_THRESHOLD).
    Requires a CLEAN score this pass (pivot, ATR, pct_change, usable chain).
    Requires TWO consecutive clean prints below THESIS_EXIT_SCORE.
    Exit-only passes have no card — do not void on a stale last_live_score
    and do not break the streak.
    """
    ticker = str(trade.get("ticker") or "?").upper()
    if card is None and live_score is None:
        return None
    if card is None:
        # Score was supplied without a card (unit tests / legacy). Treat as
        # one reading but still require the two-print counter.
        clean, dirty_why = True, None
    else:
        clean, dirty_why = score_is_clean_for_thesis(card, options_dict)
    if not clean:
        trade["thesis_below_streak"] = 0
        trade["_thesis_skip_log"] = (
            f"THESIS_SKIP {ticker} ({dirty_why or 'dirty'}) — not evaluated"
        )
        return None
    score = live_score
    if score is None:
        score = _live_score_from_card(card)
    if score is None:
        return None
    entry_score = _f(trade.get("entry_score"))
    if entry_score is None:
        return None
    min_entry = float(getattr(config, "EXECUTE_THRESHOLD", 70.0))
    exit_thr = float(getattr(config, "THESIS_EXIT_SCORE", 55.0))
    if entry_score < min_entry:
        return None
    if score < exit_thr:
        try:
            streak = int(trade.get("thesis_below_streak") or 0) + 1
        except (TypeError, ValueError):
            streak = 1
        trade["thesis_below_streak"] = streak
        if streak < 2:
            trade["_thesis_skip_log"] = (
                f"THESIS_ARM {ticker} score={score:.0f} (1/2) — waiting for confirmation"
            )
            return None
        return "THESIS_VOID"
    trade["thesis_below_streak"] = 0
    return None


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
    live_score: float | None = None,
) -> tuple[str | None, str | None]:
    """
    B5 rules (checked after hard SL/TP).

    Returns (exit_reason | None, skip_log_line | None).
    skip_log_line is set when TIME_STOP would have fired but score exempted:
      TIME_STOP SKIPPED MSFT score=84 >= 80
    """
    if entry is None or pnl is None:
        return None, None

    peak = _f(trade.get("peak_pnl_pct"))
    if peak is None:
        peak = pnl

    be_peak = float(getattr(config, "EXIT_BREAKEVEN_PEAK_PCT", 25.0))
    trail_peak = float(getattr(config, "EXIT_TRAIL_PEAK_PCT", 40.0))
    giveback = float(getattr(config, "EXIT_TRAIL_GIVEBACK_FRAC", 0.30))
    t_min = int(getattr(config, "EXIT_TIME_STOP_MINUTES", 90))
    t_band = float(getattr(config, "EXIT_TIME_STOP_PNL_ABS_PCT", 10.0))
    score_exempt = float(getattr(config, "TIME_STOP_SCORE_EXEMPT", 80.0))

    # Breakeven lock: was up enough, mark back to entry (or worse)
    if peak >= be_peak and mark <= entry:
        return "BREAKEVEN_LOCK", None

    # Trailing giveback of peak gain
    if peak >= trail_peak and peak > 0:
        floor_pnl = peak * (1.0 - giveback)
        if pnl <= floor_pnl:
            return "TRAILING_GIVEBACK", None

    # Time stop: dead money — not when thesis still strong
    score = live_score
    if score is None:
        score = _f(trade.get("last_live_score"))

    ent = _parse_entry_time(trade)
    if ent is not None:
        age_min = (now_utc - ent.astimezone(timezone.utc)).total_seconds() / 60.0
        if age_min >= t_min and abs(pnl) < t_band:
            if score is not None and score >= score_exempt:
                ticker = str(trade.get("ticker") or "?").upper()
                skip = (
                    f"TIME_STOP SKIPPED {ticker} "
                    f"score={score:.0f} >= {score_exempt:.0f}"
                )
                return None, skip
            return "TIME_STOP", None

    return None, None


def _mark_fail_threshold() -> int:
    return max(1, int(getattr(config, "MARK_FAIL_ALERT_STREAK", 2)))


def _alert_mark_failure(trade: dict[str, Any], streak: int, *, all_failed: bool = False) -> None:
    """Discord CRITICAL when marks fail. First per position, then doubles / every 10."""
    prev = 0
    try:
        prev = int(trade.get("mark_fail_alert_at") or 0)
    except (TypeError, ValueError):
        prev = 0
    if not _should_escalate(prev, streak):
        return
    trade["mark_fail_alert_at"] = streak
    label = _fmt_contract_label(trade)
    if all_failed:
        msg = (
            f"🚨 **CRITICAL: MARK FAILED (all open)**\n"
            f"`{label}` and other open positions: **0 marks obtained** this pass.\n"
            f"Underlying fallback stop applies if spot is live. "
            f"Consecutive mark failures: {streak}."
        )
    else:
        msg = (
            f"🚨 **CRITICAL: MARK FAILED {label} x{streak}**\n"
            f"Option mark missing. Underlying stop used if spot is available."
        )
    print(f"[Exits] {msg.replace(chr(10), ' | ')}")
    try:
        import broadcaster
        broadcaster.send_discord_alert(msg)
    except Exception as e:
        print(f"[Exits] mark-fail Discord warn: {e}")


def _alert_unprotected(trade: dict[str, Any], minutes: float) -> None:
    if trade.get("unprotected_alerted"):
        return
    trade["unprotected_alerted"] = True
    label = _fmt_contract_label(trade)
    msg = (
        f"🚨 **CRITICAL: UNPROTECTED {label}**\n"
        f"No option mark and no underlying spot for {minutes:.0f} minutes "
        f"(MARK_BLACKOUT_MINUTES="
        f"{int(getattr(config, 'MARK_BLACKOUT_MINUTES', 45))}). "
        f"Stop is not being checked. Manual action may be required."
    )
    print(f"[Exits] {msg.replace(chr(10), ' | ')}")
    try:
        import broadcaster
        broadcaster.send_discord_alert(msg)
    except Exception as e:
        print(f"[Exits] UNPROTECTED Discord warn: {e}")


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

    scored_by_ticker: map ticker -> {options_dict, card, ...} from phase-1
    or exit-only chain pulls.

    Mark health: counts positions checked / marks ok / marks failed; persists
    mark_fail_streak on each trade; Discord CRITICAL after
    MARK_FAIL_ALERT_STREAK consecutive failures (default 2), and if zero
    marks while any positions are open.
    """
    summary: dict[str, Any] = {
        "closed": [],
        "marks_recorded": 0,
        "positions_checked": 0,
        "marks_ok": 0,
        "marks_failed": 0,
        "skipped_no_mark": [],
        "mark_fail_alerts": [],
        "time_stop_skipped": [],
        "thesis_skipped": [],
        "eod_triggered": False,
        "open_before": len(open_trades),
        "open_after": None,
    }
    if not open_trades:
        summary["open_after"] = 0
        now_empty = _chicago_now(now_cdt)
        if is_eod_flatten_window(now_empty) and not eod_already_done(now_empty.date()):
            mark_eod_done(now_empty.date())
            summary["eod_triggered"] = True
        book_line = maybe_emit_eod_book(now_cdt)
        if book_line:
            summary["book_line"] = book_line
        return summary

    now = _chicago_now(now_cdt)
    sess = session_date or _et_session_date()
    do_eod = force_eod if force_eod is not None else (
        is_eod_flatten_window(now) and not eod_already_done(now.date())
    )
    fail_threshold = _mark_fail_threshold()

    # Work on a shallow copy of the list; closes remove from durable store.
    for trade in list(open_trades):
        if not isinstance(trade, dict) or not trade.get("ticker"):
            continue
        ticker = str(trade["ticker"])
        summary["positions_checked"] += 1
        ctx = scored_by_ticker.get(ticker) or scored_by_ticker.get(ticker.upper()) or {}
        options_dict = ctx.get("options_dict")
        card = ctx.get("card")
        live_score = _live_score_from_card(card)

        mark_info = lookup_option_mark(trade, options_dict)
        entry = _entry_premium(trade)
        mark = _f(mark_info.get("mark"))

        # Always try to record a mark when we have one (B4), including
        # the pre-close observation so the path is complete.
        if mark is not None:
            summary["marks_ok"] += 1
            trade["mark_fail_streak"] = 0
            trade["mark_fail_alert_at"] = 0
            trade.pop("unmarked_since", None)
            trade.pop("spot_dark_since", None)
            trade.pop("unprotected_alerted", None)
            if live_score is not None:
                trade["last_live_score"] = live_score
            if record_position_mark(
                trade, mark_info, live_score=live_score, scan_id=scan_id
            ):
                summary["marks_recorded"] += 1
            # Track peak/trough for B5 + TRADE mfe/mae. last_bid is BOOK mark.
            pnl = _pnl_pct(entry, mark)
            if pnl is not None:
                prev_peak = _f(trade.get("peak_pnl_pct"))
                if prev_peak is None or pnl > prev_peak:
                    trade["peak_pnl_pct"] = pnl
                prev_trough = _f(trade.get("trough_pnl_pct"))
                if prev_trough is None or pnl < prev_trough:
                    trade["trough_pnl_pct"] = pnl
                trade["last_mark"] = mark
                trade["last_bid"] = mark_info.get("bid")
                trade["last_ask"] = mark_info.get("ask")
                trade["last_mark_at"] = datetime.now(timezone.utc).isoformat()
            try:
                from tracker_agent import save_active_trade
                save_active_trade(trade)
            except Exception as pe:
                print(f"[Exits] peak/mark persist warn {ticker}: {pe}")
        else:
            summary["marks_failed"] += 1
            summary["skipped_no_mark"].append(ticker)
            try:
                streak = int(trade.get("mark_fail_streak") or 0) + 1
            except (TypeError, ValueError):
                streak = 1
            trade["mark_fail_streak"] = streak
            trade["last_mark_fail_at"] = datetime.now(timezone.utc).isoformat()
            if not trade.get("unmarked_since"):
                trade["unmarked_since"] = datetime.now(timezone.utc).isoformat()
            spot = _f(mark_info.get("spot"))
            if spot is None:
                try:
                    from data_engineer import fetch_spot as _fetch_spot
                    spot = _fetch_spot(ticker)
                    if spot is not None:
                        mark_info["spot"] = spot
                except Exception:
                    pass
            if spot is not None:
                trade["last_spot"] = spot
                trade["last_spot_at"] = datetime.now(timezone.utc).isoformat()
                trade.pop("spot_dark_since", None)
            else:
                if not trade.get("spot_dark_since"):
                    trade["spot_dark_since"] = datetime.now(timezone.utc).isoformat()
            try:
                from tracker_agent import save_active_trade
                save_active_trade(trade)
            except Exception as pe:
                print(f"[Exits] mark_fail_streak persist warn {ticker}: {pe}")
            if streak >= fail_threshold:
                label = _fmt_contract_label(trade)
                summary["mark_fail_alerts"].append(f"{label} x{streak}")
                _alert_mark_failure(trade, streak, all_failed=False)
            if spot is None:
                try:
                    since = _parse_entry_time(
                        {"entry_timestamp": trade.get("spot_dark_since")
                         or trade.get("unmarked_since")}
                    )
                    if since is not None:
                        mins = (
                            datetime.now(timezone.utc) - since.astimezone(timezone.utc)
                        ).total_seconds() / 60.0
                        blackout = float(getattr(config, "MARK_BLACKOUT_MINUTES", 45))
                        if mins >= blackout:
                            _alert_unprotected(trade, mins)
                            try:
                                from tracker_agent import save_active_trade
                                save_active_trade(trade)
                            except Exception:
                                pass
                except Exception:
                    pass

        # B1 selective EOD (cal_dte < CARRY_MIN_DTE only), C-D 0DTE, B2–B5
        # TIME_STOP uses live_score or last_live_score; exempt if >= TIME_STOP_SCORE_EXEMPT
        trade.pop("_time_stop_skip_log", None)
        reason, exit_px = evaluate_exit_reason_for_mark(
            trade,
            mark,
            sess=sess,
            now=now,
            include_time_stop=True,
            do_eod=do_eod,
            live_score=live_score,
            card=card,
            options_dict=options_dict,
        )
        if reason is None and mark is None:
            u_reason, u_px = underlying_exit_reason(trade, mark_info.get("spot"))
            if u_reason and u_px is not None:
                reason, exit_px = u_reason, u_px
                trade["_underlying_spot"] = mark_info.get("spot")
                trade["_underlying_stop_spot"] = trade.get("stop_spot")
                trade["_underlying_target_spot"] = trade.get("target_spot")
        skip_log = trade.pop("_time_stop_skip_log", None)
        if skip_log:
            summary["time_stop_skipped"].append(skip_log)
            print(f"[Exits] {skip_log}")
        thesis_skip = trade.pop("_thesis_skip_log", None)
        if thesis_skip:
            summary.setdefault("thesis_skipped", []).append(thesis_skip)
            print(f"[Exits] {thesis_skip}")
        if thesis_skip or "thesis_below_streak" in trade:
            try:
                from tracker_agent import save_active_trade
                save_active_trade(trade)
            except Exception:
                pass

        if reason is None:
            continue

        if exit_px is None:
            print(f"[Exits] {ticker} would close ({reason}) but no mark/entry — skip")
            continue

        closed = close_open_position(trade, float(exit_px), reason)
        if str(reason).startswith("UNDERLYING_"):
            closed["fill_est"] = True
            closed["spot"] = trade.get("_underlying_spot") or mark_info.get("spot")
            closed["stop_spot"] = trade.get("stop_spot")
            closed["target_spot"] = trade.get("target_spot")
            spot_s = closed.get("spot")
            lvl = closed.get("stop_spot") if "STOP" in str(reason) else closed.get("target_spot")
            line = (
                f"{ticker} {reason} @ ~{float(exit_px):.2f} "
                f"(est, mark unavailable, spot {spot_s} vs "
                f"{'stop_spot' if 'STOP' in str(reason) else 'target_spot'} {lvl})"
            )
            print(f"[Exits] {line}")
            closed["underlying_line"] = line
        summary["closed"].append(closed)

    if do_eod:
        mark_eod_done(now.date())
        summary["eod_triggered"] = True

    # Zero marks while book is open → fleet-wide CRITICAL
    if (
        summary["positions_checked"] > 0
        and summary["marks_ok"] == 0
        and summary["marks_failed"] > 0
    ):
        summary["all_marks_failed"] = True
        # Alert once with first open trade as example
        sample = next(
            (t for t in open_trades if isinstance(t, dict) and t.get("ticker")),
            {"ticker": "?"},
        )
        _alert_mark_failure(sample, summary["marks_failed"], all_failed=True)

    try:
        from tracker_agent import load_active_trades
        summary["open_after"] = len(load_active_trades())
    except Exception:
        summary["open_after"] = None

    n_closed = len(summary["closed"])
    n_ts_skip = len(summary["time_stop_skipped"])
    # One-line health summary every exit pass (log always)
    mark_line = (
        f"[Exits] marks: checked={summary['positions_checked']} "
        f"ok={summary['marks_ok']} failed={summary['marks_failed']} "
        f"closed={n_closed} time_stop_skipped={n_ts_skip} "
        f"eod={summary['eod_triggered']} "
        f"carry_min_dte={_carry_min_dte()} "
        f"open {summary['open_before']}→{summary['open_after']}"
    )
    print(mark_line)
    summary["mark_summary_line"] = mark_line
    book_line = maybe_emit_eod_book(now)
    if book_line:
        summary["book_line"] = book_line
    return summary
