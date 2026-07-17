"""
strike_selector.py — Advanced Volatility & Strike Selection (Task 3a).

When the scoring engine flags EXECUTE, this module picks the specific
contract algorithmically instead of leaving it to LLM prose:

  1. Direction from technical posture (close vs pivot + day momentum).
  2. Expected move = ATR(14) * sqrt(days_to_expiration) — the strike target
     sits at 50% of the expected move (achievable, still leveraged).
  3. Candidate contracts near the target strike are ranked on:
       tight spread (45%), open interest (25%), volume (15%),
       and IV vs. the chain median (15%) — cheaper-than-median IV is
       preferred when BUYING premium (IV-crush protection).
"""

import math
from datetime import datetime


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


def _days_to_expiration(exp_str):
    try:
        exp = datetime.strptime(exp_str, "%Y-%m-%d")
        return max((exp - datetime.now()).days, 1)
    except Exception:
        return 7  # sane default for weeklies


def infer_direction(pivot_data):
    """CALL if price holds above pivot with non-negative momentum, PUT if
    decisively below pivot with negative momentum, else side with posture."""
    close = pivot_data.get("close", 0.0)
    pivot = pivot_data.get("pivot", close)
    pct = pivot_data.get("pct_change", 0.0)
    if close >= pivot and pct >= 0:
        return "CALL"
    if close < pivot and pct < 0:
        return "PUT"
    return "CALL" if close >= pivot else "PUT"


def select_optimal_contract(options_dict, pivot_data, atr_abs=None):
    """
    Returns a dict describing the chosen contract + rationale, or
    {"error": ...} when nothing tradeable exists (the caller should then
    downgrade the broadcast to PASS-with-reason rather than crash).
    """
    spot = options_dict.get("current_price")
    chains = options_dict.get("chains", {})
    if not isinstance(spot, (int, float)) or not chains:
        return {"error": "No usable chain or spot price."}

    direction = infer_direction(pivot_data)
    side_key = "calls" if direction == "CALL" else "puts"

    # Default expected daily move of 1.5% if ATR unavailable
    atr = atr_abs if atr_abs else spot * 0.015

    best, best_rank, best_meta = None, -1.0, {}
    for exp, sides in chains.items():
        dte = _days_to_expiration(exp)
        expected_move = atr * math.sqrt(dte)
        target = spot + 0.5 * expected_move if direction == "CALL" else spot - 0.5 * expected_move

        contracts = sides.get(side_key, [])
        ivs = sorted([c.get("impliedVolatility") or 0 for c in contracts if c.get("impliedVolatility")])
        median_iv = ivs[len(ivs) // 2] if ivs else 0.0

        for c in contracts:
            strike = c.get("strike") or 0
            if not strike or abs(strike - target) / spot > 0.04:  # within 4% of target
                continue
            bid, ask = c.get("bid") or 0.0, c.get("ask") or 0.0
            mid = (bid + ask) / 2.0
            if mid <= 0.05:  # untradeable teenies
                continue
            spread_pct = (ask - bid) / mid if mid else 1.0
            oi = int(c.get("openInterest") or 0)
            vol = int(c.get("volume") or 0)
            iv = c.get("impliedVolatility") or 0.0

            spread_sub = max(0.0, min(1.0, (0.15 - spread_pct) / 0.13))
            oi_sub = min(1.0, math.log10(max(oi, 1)) / 4.0)
            vol_sub = min(1.0, math.log10(max(vol, 1)) / 3.5)
            iv_sub = 1.0 if (median_iv and iv <= median_iv) else 0.4  # prefer <= median IV

            rank = 0.45 * spread_sub + 0.25 * oi_sub + 0.15 * vol_sub + 0.15 * iv_sub
            if rank > best_rank:
                best_rank = rank
                best = c
                best_meta = {
                    "expiration": exp, "dte": dte, "target_strike": round(target, 2),
                    "expected_move": round(expected_move, 2), "median_iv": round(median_iv, 4),
                    "spread_pct": round(spread_pct * 100, 2), "mid": round(mid, 2),
                }

    if best is None:
        return {"error": f"No liquid {direction} contract within 4% of the ATR-derived target strike."}

    entry = best_meta["mid"]
    return {
        "direction": direction,
        "strike": best["strike"],
        "expiration": best_meta["expiration"],
        "days_to_expiration": best_meta["dte"],
        "entry_premium": entry,
        "stop_loss": round(entry * 0.80, 2),      # 20% stop
        "take_profit": round(entry * 1.50, 2),    # 50% target
        "implied_volatility": round(best.get("impliedVolatility") or 0.0, 4),
        "chain_median_iv": best_meta["median_iv"],
        "bid_ask_spread_pct": best_meta["spread_pct"],
        "open_interest": int(best.get("openInterest") or 0),
        "volume": int(best.get("volume") or 0),
        "atr_expected_move": best_meta["expected_move"],
        "selection_rank": round(best_rank, 3),
        "rationale": (
            f"{direction} selected from {'above' if direction == 'CALL' else 'below'}-pivot posture. "
            f"ATR expected move ${best_meta['expected_move']} over {best_meta['dte']}d places the target near "
            f"${best_meta['target_strike']}; strike {best['strike']} chosen for "
            f"{best_meta['spread_pct']}% spread, OI {int(best.get('openInterest') or 0):,}, "
            f"IV {round(best.get('impliedVolatility') or 0, 3)} vs chain median {best_meta['median_iv']}."
        ),
    }
