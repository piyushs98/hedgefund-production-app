"""
strike_selector.py — Advanced Volatility & Strike Selection (Task 3a + Stage 4 C).

When the scoring engine flags EXECUTE, this module picks the specific
contract algorithmically instead of leaving it to LLM prose:

  1. Direction from technical posture (close vs pivot + day momentum).
  2. Expected move = ATR(14) * sqrt(effective_dte) — honest same-day
     fraction using ET RTH (09:30–16:00); no floor-at-1 (C-B).
  3. Candidate contracts near the target strike are ranked on:
       tight spread (45%), open interest (25%), volume (15%),
       and IV vs. the chain median (15%).
  4. Entry filters (reject → try next rank; else error so gate.rollback_admit):
       C-A  calendar DTE >= MIN_DTE (default 1)
       C-C  required_move_atr <= REQUIRED_MOVE_ATR_K (default 0.5)
       C-F  decay_density <= EXIT_MAX_DECAY_DENSITY (default 8 %/hr)
       spread  (ask-bid)/mid <= MAX_CONTRACT_SPREAD_PCT (default 8 %)
               on the candidate only; no two-sided quote → no_liq_data
     Error payloads include reject_tag for GATE line logging, e.g. decay(9.3>8.0)
     or spread(12.4>8.0).
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import config

_ET = ZoneInfo("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_RTH_HOURS = 6.5


def compute_atr(hist_df, period=14):
    """True ATR from a yfinance history DataFrame (needs High/Low/Close).
    Returns (atr_absolute, atr_pct_of_close) or (None, None)."""
    try:
        if hist_df is None or hist_df.empty or len(hist_df) < 2:
            return None, None
        high, low, close = hist_df["High"], hist_df["Low"], hist_df["Close"]
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = tr1.combine(tr2, max).combine(tr3, max)
        atr = tr.rolling(window=min(period, len(tr))).mean().iloc[-1]
        last_close = float(close.iloc[-1])
        if not last_close or math.isnan(float(atr)):
            return None, None
        return float(atr), round(float(atr) / last_close * 100, 2)
    except Exception as e:
        print(f"[Strike Selector] ATR computation failed: {e}")
        return None, None


def _now_et(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(_ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=_ET)
    return now.astimezone(_ET)


def calendar_dte(exp_str: str, now: datetime | None = None) -> int:
    """Whole calendar days from ET session date to expiration date."""
    et = _now_et(now)
    exp = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
    return (exp - et.date()).days


def rth_fraction_remaining(now: datetime | None = None) -> float:
    """
    Remaining fraction of a full RTH day (09:30–16:00 ET).
    1.0 before the open, 0.0 at/after the close.
    """
    et = _now_et(now)
    open_dt = et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_dt = et.replace(hour=16, minute=0, second=0, microsecond=0)
    full = (close_dt - open_dt).total_seconds()
    if full <= 0:
        return 0.0
    if et >= close_dt:
        return 0.0
    if et <= open_dt:
        return 1.0
    return max(0.0, (close_dt - et).total_seconds() / full)


def rth_hours_remaining_to_expiry(exp_str: str, now: datetime | None = None) -> float:
    """
    Approximate RTH hours from now until expiration close (16:00 ET on exp date).

    Counts remaining time today (if any) plus 6.5h for each full session day
    after today through the expiration date.
    """
    et = _now_et(now)
    try:
        exp_d = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
    except ValueError:
        return _RTH_HOURS * 7

    today = et.date()
    cal = (exp_d - today).days
    if cal < 0:
        return 0.0

    # Hours left in today's RTH (0 if after close)
    open_dt = et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_dt = et.replace(hour=16, minute=0, second=0, microsecond=0)
    if et >= close_dt:
        today_left = 0.0
    elif et <= open_dt:
        today_left = _RTH_HOURS
    else:
        today_left = max(0.0, (close_dt - et).total_seconds() / 3600.0)

    if cal == 0:
        return today_left

    # Remaining today + full RTH days for each day from tomorrow through exp
    # (calendar_days full sessions after today, including exp day)
    return today_left + _RTH_HOURS * float(cal)


def effective_dte(exp_str: str, now: datetime | None = None) -> float:
    """
    C-B: trading-day units for expected_move — no floor at 1.

    0DTE  → remaining RTH fraction of today (e.g. ~0.84 at 09:32 CDT / 10:32 ET)
    N DTE → remaining_today_frac + N  (time until exp-day close in session units)
    """
    cal = calendar_dte(exp_str, now)
    if cal < 0:
        return 0.0
    frac = rth_fraction_remaining(now)
    if cal == 0:
        return max(frac, 1e-6)
    return float(cal) + frac


# Back-compat name used in older call sites / tests
def _days_to_expiration(exp_str: str, now: datetime | None = None) -> float:
    return effective_dte(exp_str, now)


def infer_direction(pivot_data):
    """CALL if price holds above pivot with non-negative momentum, PUT if
    decisively below pivot with negative momentum, else side with posture.

    pct_change may be None (unknown day-change) — treat as neutral for
    direction so we do not force PUT/CALL from a data hole.
    """
    close = pivot_data.get("close", 0.0)
    pivot = pivot_data.get("pivot", close)
    raw_pct = pivot_data.get("pct_change", None)
    try:
        pct = float(raw_pct) if raw_pct is not None else None
    except (TypeError, ValueError):
        pct = None
    if pct is None:
        return "CALL" if close >= pivot else "PUT"
    if close >= pivot and pct >= 0:
        return "CALL"
    if close < pivot and pct < 0:
        return "PUT"
    return "CALL" if close >= pivot else "PUT"


def _moneyness(spot: float, strike: float, entry: float, direction: str):
    if direction == "CALL":
        intrinsic = max(0.0, spot - strike)
    else:
        intrinsic = max(0.0, strike - spot)
    extrinsic = float(entry) - intrinsic
    extrinsic_pct = (extrinsic / float(entry) * 100.0) if float(entry) > 0 else None
    return intrinsic, extrinsic, extrinsic_pct


def required_move_atr(
    spot: float,
    strike: float,
    premium: float,
    direction: str,
    atr: float,
    dte_eff: float,
) -> float | None:
    """
    C-C: underlying move (in ATR·√dte units) needed to reach breakeven by expiry.
    CALL BE = strike + premium; PUT BE = strike − premium.
    Already-ITM enough for BE → 0.0.
    """
    if atr is None or atr <= 0 or dte_eff is None or dte_eff <= 0:
        return None
    if direction == "CALL":
        be = strike + premium
        need = be - spot
    else:
        be = strike - premium
        need = spot - be
    if need <= 0:
        return 0.0
    denom = atr * math.sqrt(dte_eff)
    if denom <= 0:
        return None
    return need / denom


def decay_density(extrinsic_pct: float | None, hours_rth_to_expiry: float) -> float | None:
    """C-F: ext_pct / RTH hours remaining to expiry (% per hour)."""
    if extrinsic_pct is None:
        return None
    if hours_rth_to_expiry is None or hours_rth_to_expiry <= 0:
        # No time left → infinite density; treat as huge so filters reject
        return 1e9
    return float(extrinsic_pct) / float(hours_rth_to_expiry)


def _passes_entry_filters(
    *,
    cal_dte: int,
    rm_atr: float | None,
    dens: float | None,
    extrinsic: float | None = None,
    extrinsic_pct: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    spread_pct: float | None = None,
) -> tuple[bool, str | None]:
    """
    Return (ok, reject_tag). reject_tag is compact for GATE lines:
      min_dte(0) | rm_atr(0.61>0.50) | decay(9.3>8.0) | bad_quote(ext=…)
      | min_ext(1.5<10) | spread(12.4>8.0) | no_liq_data
    """
    min_dte = int(getattr(config, "MIN_DTE", 1))
    k = float(getattr(config, "REQUIRED_MOVE_ATR_K", 0.5))
    max_dens = float(getattr(config, "EXIT_MAX_DECAY_DENSITY", 8.0))
    min_ext_pct = float(getattr(config, "MIN_EXTRINSIC_PCT", 10.0))
    max_spread = float(getattr(config, "MAX_CONTRACT_SPREAD_PCT", 8.0))

    if cal_dte < min_dte:
        return False, f"min_dte({cal_dte})"
    # Two-sided quote required; missing bid is UNKNOWN, not a 200% spread.
    if bid is not None or ask is not None:
        try:
            b = float(bid) if bid is not None else 0.0
            a = float(ask) if ask is not None else 0.0
        except (TypeError, ValueError):
            return False, "no_liq_data"
        if not (a >= b > 0):
            return False, "no_liq_data"
    if spread_pct is not None:
        try:
            spr = float(spread_pct)
        except (TypeError, ValueError):
            spr = None
        if spr is not None and spr > max_spread:
            return False, f"spread({spr:.1f}>{max_spread:.1f})"
    # Stale/crossed mid: extrinsic cannot be non-positive in a real market
    if extrinsic is not None and extrinsic <= 0:
        return False, f"bad_quote(ext={extrinsic:.2f})"
    if extrinsic_pct is not None and extrinsic_pct <= 0:
        return False, f"bad_quote(ext%={extrinsic_pct:.1f})"
    # Deep ITM / synthetic stock — low theta density but pure delta risk
    if extrinsic_pct is not None and extrinsic_pct < min_ext_pct:
        return False, f"min_ext({extrinsic_pct:.1f}<{min_ext_pct:.0f})"
    if rm_atr is not None and rm_atr > k:
        return False, f"rm_atr({rm_atr:.2f}>{k:.2f})"
    if dens is not None and dens > max_dens:
        return False, f"decay({dens:.1f}>{max_dens:.1f})"
    return True, None


def select_optimal_contract(options_dict, pivot_data, atr_abs=None, now=None):
    """
    Returns a dict describing the chosen contract + rationale, or
    {"error": ..., "reject_tag": "min_dte(0)"} when nothing tradeable
    exists (caller must gate.rollback_admit, not on_close).
    """
    spot = options_dict.get("current_price")
    chains = options_dict.get("chains", {})
    if not isinstance(spot, (int, float)) or not chains:
        return {"error": "No usable chain or spot price."}

    direction = infer_direction(pivot_data)
    side_key = "calls" if direction == "CALL" else "puts"
    atr = atr_abs if atr_abs else spot * 0.015
    min_dte = int(getattr(config, "MIN_DTE", 1))
    now = now  # optional inject for tests

    # Rank all liquid near-target contracts across loaded expiries
    ranked: list[dict[str, Any]] = []
    in_band = 0
    for exp, sides in chains.items():
        try:
            cal = calendar_dte(exp, now)
        except Exception:
            continue
        # Soft prefilter: skip clearly expired; MIN_DTE applied hard below
        if cal < 0:
            continue

        dte_eff = effective_dte(exp, now)
        hours_left = rth_hours_remaining_to_expiry(exp, now)
        expected_move = atr * math.sqrt(max(dte_eff, 1e-6))
        target = (
            spot + 0.5 * expected_move
            if direction == "CALL"
            else spot - 0.5 * expected_move
        )

        contracts = sides.get(side_key, []) if isinstance(sides, dict) else []
        ivs = sorted(
            [c.get("impliedVolatility") or 0 for c in contracts if c.get("impliedVolatility")]
        )
        median_iv = ivs[len(ivs) // 2] if ivs else 0.0

        for c in contracts:
            strike = c.get("strike") or 0
            if not strike or abs(strike - target) / spot > 0.04:
                continue
            in_band += 1
            bid, ask = c.get("bid") or 0.0, c.get("ask") or 0.0
            mid = (bid + ask) / 2.0
            if mid <= 0.05:
                continue
            spread_pct = (ask - bid) / mid if mid else 1.0
            oi = int(c.get("openInterest") or 0)
            vol = int(c.get("volume") or 0)
            iv = c.get("impliedVolatility") or 0.0

            spread_sub = max(0.0, min(1.0, (0.15 - spread_pct) / 0.13))
            oi_sub = min(1.0, math.log10(max(oi, 1)) / 4.0)
            vol_sub = min(1.0, math.log10(max(vol, 1)) / 3.5)
            iv_sub = 1.0 if (median_iv and iv <= median_iv) else 0.4

            rank = 0.45 * spread_sub + 0.25 * oi_sub + 0.15 * vol_sub + 0.15 * iv_sub
            try:
                intrinsic, extrinsic, extrinsic_pct = _moneyness(
                    float(spot), float(strike), float(mid), direction
                )
            except (TypeError, ValueError):
                continue

            rm = required_move_atr(
                float(spot), float(strike), float(mid), direction, float(atr), dte_eff
            )
            dens = decay_density(extrinsic_pct, hours_left)

            ranked.append(
                {
                    "contract": c,
                    "rank": rank,
                    "expiration": exp,
                    "cal_dte": cal,
                    "dte_eff": dte_eff,
                    "hours_left": hours_left,
                    "target_strike": round(target, 2),
                    "expected_move": round(expected_move, 2),
                    "median_iv": round(median_iv, 4),
                    "spread_pct": round(spread_pct * 100, 2),
                    "bid": bid,
                    "ask": ask,
                    "mid": round(mid, 2),
                    "intrinsic": intrinsic,
                    "extrinsic": extrinsic,
                    "extrinsic_pct": extrinsic_pct,
                    "required_move_atr": rm,
                    "decay_density": dens,
                }
            )

    if not ranked:
        if in_band > 0:
            return {
                "error": (
                    f"No two-sided quotes on {in_band} {direction} contract(s) "
                    f"within 4% of the ATR-derived target strike."
                ),
                "reject_tag": "no_liq_data",
            }
        return {
            "error": (
                f"No liquid {direction} contract within 4% of the ATR-derived "
                f"target strike (MIN_DTE={min_dte})."
            ),
            "reject_tag": "no_liquid",
        }

    ranked.sort(key=lambda r: r["rank"], reverse=True)

    rejects: list[str] = []
    reject_tags: list[str] = []
    chosen = None
    for cand in ranked:
        ok, why = _passes_entry_filters(
            cal_dte=cand["cal_dte"],
            rm_atr=cand["required_move_atr"],
            dens=cand["decay_density"],
            extrinsic=cand.get("extrinsic"),
            extrinsic_pct=cand.get("extrinsic_pct"),
            bid=cand.get("bid"),
            ask=cand.get("ask"),
            spread_pct=cand.get("spread_pct"),
        )
        if not ok:
            tag = why or "filter"
            reject_tags.append(tag)
            rejects.append(
                f"{cand['expiration']} {cand['contract'].get('strike')}: {tag}"
            )
            continue
        chosen = cand
        break

    if chosen is None:
        # Primary tag = top-ranked candidate's failure (most liquid / would-have-picked)
        primary = reject_tags[0] if reject_tags else "filter"
        sample = "; ".join(rejects[:5])
        return {
            "error": (
                f"All {direction} candidates failed entry filters "
                f"(MIN_DTE/required_move/decay_density/spread). "
                f"Tried {len(ranked)}; e.g. {sample}"
            ),
            "reject_tag": primary,
            "filter_rejects": rejects,
        }

    best = chosen["contract"]
    entry = chosen["mid"]
    strike = best["strike"]
    intrinsic = chosen["intrinsic"]
    extrinsic = chosen["extrinsic"]
    extrinsic_pct = chosen["extrinsic_pct"]
    rm_atr = chosen["required_move_atr"]
    dens = chosen["decay_density"]

    raw_delta = best.get("delta")
    try:
        delta = float(raw_delta) if raw_delta is not None and raw_delta != "" else None
        if delta is not None and (delta != delta):
            delta = None
    except (TypeError, ValueError):
        delta = None

    out = {
        "direction": direction,
        "strike": strike,
        "expiration": chosen["expiration"],
        "days_to_expiration": round(chosen["dte_eff"], 3),
        "calendar_dte": chosen["cal_dte"],
        "entry_premium": entry,
        "stop_loss": round(entry * 0.80, 2),
        "take_profit": round(entry * 1.50, 2),
        "implied_volatility": round(best.get("impliedVolatility") or 0.0, 4),
        "chain_median_iv": chosen["median_iv"],
        "bid_ask_spread_pct": chosen["spread_pct"],
        "open_interest": int(best.get("openInterest") or 0),
        "volume": int(best.get("volume") or 0),
        "atr_expected_move": chosen["expected_move"],
        "selection_rank": round(chosen["rank"], 3),
        "spot": round(float(spot), 2) if isinstance(spot, (int, float)) else spot,
        "intrinsic": round(intrinsic, 2) if intrinsic is not None else None,
        "extrinsic": round(extrinsic, 2) if extrinsic is not None else None,
        "extrinsic_pct": round(extrinsic_pct, 1) if extrinsic_pct is not None else None,
        "required_move_atr": round(rm_atr, 3) if rm_atr is not None else None,
        "decay_density": round(dens, 2) if dens is not None else None,
        "hours_to_expiry_rth": round(chosen["hours_left"], 2),
        "rationale": (
            f"{direction} selected from "
            f"{'above' if direction == 'CALL' else 'below'}-pivot posture. "
            f"ATR expected move ${chosen['expected_move']} over "
            f"{chosen['dte_eff']:.2f}d (cal {chosen['cal_dte']}) places the target near "
            f"${chosen['target_strike']}; strike {best['strike']} chosen for "
            f"{chosen['spread_pct']}% spread, OI {int(best.get('openInterest') or 0):,}, "
            f"IV {round(best.get('impliedVolatility') or 0, 3)} vs chain median "
            f"{chosen['median_iv']}; rm_atr={rm_atr if rm_atr is not None else 'n/a'} "
            f"decay={dens if dens is not None else 'n/a'}%/hr."
        ),
    }
    if delta is not None:
        out["delta"] = round(delta, 3)
    if rejects:
        out["rejected_better_ranks"] = len(rejects)
    return out
