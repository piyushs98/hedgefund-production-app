"""
scoring_engine.py — Floor-fixed conviction scoring.

Score = clamp( Technical(0..TECH_CEIL) + Sentiment(-SENT_MAX..+SENT_MAX), 0, 100 )

Technical alone can clear EXECUTE_THRESHOLD (70). Sentiment is a signed
modifier that confirms or vetoes; it cannot manufacture a signal from a
weak technical setup.

Liquidity is NOT in the score. The chain-wide ATM median is a breadth
statistic (138–740 contracts); it is logged as atm_n/med_spr/usable for
Discord forensics only. Tradability is a Part C reject on the chosen
contract (MAX_CONTRACT_SPREAD_PCT).

Dead zone: if |spot − pivot| / ATR < DEAD_ZONE_ATR, direction is undefined
and the ticker hard-PASSes regardless of other pillars.

Calibrated defaults (2026-08-10 two-sided Mon/Fri test, config/env-tunable):
  tech_ceil=85, sent_max=15
  pivot_scale=0.40 ATR, pivot_power=1.0, mom_scale=0.45 %, mix 70/30
  dead_zone_atr=0.30
  vol_mult in [0.70, 1.0]

KNOWN LIMITATION (do not fix without real telemetry):
  mom_scale saturates near ~1.1–1.2% day-move under 0.45; revisit with multi-day
  ground truth. vol_mult deliberately haircuts ATR% > 4% (e.g. TSLA) — do not
  soften without ATR-based stop geometry.

The additive 30/40/30 pillar weights are RETIRED. score_ticker ignores
config.load_weights(); saturday_audit weight writes no longer affect live scoring.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import config


def _cfg_float(name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except (TypeError, ValueError):
        return float(default)


# Lightweight sentiment lexicon for headline scoring (deterministic, no LLM).
_BULLISH_WORDS = {
    "beats", "beat", "surge", "surges", "rally", "rallies", "record", "upgrade",
    "upgraded", "outperform", "growth", "soars", "soar", "jump", "jumps", "gain",
    "gains", "bullish", "buy", "strong", "tops", "expands", "partnership",
    "breakthrough", "approval", "rate cut", "subsidize", "subsidy", "tailwind",
}
_BEARISH_WORDS = {
    "miss", "misses", "plunge", "plunges", "selloff", "sell-off", "downgrade",
    "downgraded", "lawsuit", "probe", "investigation", "recall", "layoff",
    "layoffs", "bearish", "weak", "falls", "fall", "drop", "drops", "slump",
    "cuts guidance", "tariff", "hawkish", "inflation", "bottleneck", "delay",
    "delays", "warning", "warns", "fraud", "decline",
}


@dataclass
class ScoreCard:
    ticker: str
    # Legacy field names retained for telemetry/CEO payloads:
    #   liquidity_score  -> liq_mult (0..1), NOT additive points
    #   technical_score  -> T (0..TECH_CEIL)
    #   sentiment_score  -> S (-SENT_MAX..+SENT_MAX), signed modifier
    liquidity_ratio: float = 0.0      # same as liq_mult (0..1)
    technical_ratio: float = 0.0      # tech_raw before ceil (0..1)
    sentiment_ratio: float = 0.0      # signed alignment (-1..+1)
    liquidity_score: float = 0.0      # liq_mult
    technical_score: float = 0.0      # T
    sentiment_score: float = 0.0      # S
    adversarial_penalty: float = 0.0
    total_score: float = 0.0
    action_flag: str = "PASS"
    # Deprecated: no longer 30/40/30. Stored for schema compat only.
    weights: dict = field(default_factory=lambda: {
        "liquidity": 0,
        "technical": _cfg_float("TECH_CEIL", 85.0),
        "sentiment": _cfg_float("SENT_MAX", 15.0),
    })
    metrics: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)
    # Scorer-level hard block (e.g. dead_zone) for GATE compact reasons
    block_reason: str | None = None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _ramp(x: float, scale: float, power: float = 1.0) -> float:
    """0 at x<=0; approaches 1 as x→+∞. power>1 slows the early curve."""
    if x <= 0:
        return 0.0
    return max(0.0, math.tanh(x / max(scale, 1e-9))) ** power


def _vol_mult(atr_pct) -> float:
    """ATR regime as confidence multiplier on technical, not additive points.

    Unchanged by the 2026-08 scoring rewrite: names with ATR% > 4 are haircut
    deliberately (pairing needs ATR-based stops, not a softer score).
    """
    if atr_pct is None:
        return 0.70
    try:
        a = float(atr_pct)
    except (TypeError, ValueError):
        return 0.70
    if 1.0 <= a <= 4.0:
        return 1.0
    if a < 1.0:
        return 0.70 + 0.30 * _clamp(a / 1.0, 0.0, 1.0)
    return max(0.70, 1.0 - (a - 4.0) / 8.0)


def _liq_mult_from_spread(med_spread) -> tuple[float | None, str | None]:
    """
    Liquidity is a GATE, not points.

    Returns (liq_mult, status) where status is:
      None                 — ok, mult in (0, 1]
      "no_liq_data"        — unknown (no usable quotes); mult is None
      "spread_untradeable" — measured median >10%; mult is 0.0

    Missing/empty ATM quotes are UNKNOWN, never defaulted to 100% wide.
    """
    if med_spread is None:
        return None, "no_liq_data"
    if med_spread > 0.10:
        return 0.0, "spread_untradeable"
    if med_spread > 0.06:
        return 0.60, None
    if med_spread > 0.03:
        return 0.85, None
    return 1.0, None


def _infer_direction_sign(pivot_data) -> tuple[float, str]:
    """CALL → +1, PUT → -1. Matches strike_selector.infer_direction."""
    try:
        from strike_selector import infer_direction
        d = infer_direction(pivot_data)
        if str(d).upper().startswith("P"):
            return -1.0, "PUT"
        return 1.0, "CALL"
    except Exception:
        close = float(pivot_data.get("close") or 0.0)
        pivot = float(pivot_data.get("pivot") or close)
        if close >= pivot:
            return 1.0, "CALL"
        return -1.0, "PUT"


# ------------------------------------------------------------------
# Liquidity: multiplier only
# ------------------------------------------------------------------
def score_liquidity(options_dict):
    """
    Returns (liq_mult | None, metrics dict, reasons list, status).

    status: None | "no_liq_data" | "spread_untradeable"
    Does NOT contribute additive points.
    Empty ATM quote list is UNKNOWN (no_liq_data), never med_spread=1.0.
    """
    reasons, metrics = [], {}
    spot = options_dict.get("current_price")
    chains = options_dict.get("chains", {})
    if not isinstance(spot, (int, float)) or not chains:
        mult, status = _liq_mult_from_spread(None)
        metrics.update({
            "error": "no usable chain/spot",
            "liq_mult": mult,
            "liq_status": status,
            "veto": status,
        })
        reasons.append("No usable options chain data; liquidity UNKNOWN (no_liq_data).")
        return mult, metrics, reasons, status

    atm = []
    for exp, sides in chains.items():
        for side in ("calls", "puts"):
            for opt in sides.get(side, []):
                strike = opt.get("strike") or 0
                if strike and abs(strike - spot) / spot <= 0.05:
                    atm.append(opt)
    if not atm:
        mult, status = _liq_mult_from_spread(None)
        metrics.update({
            "error": "no ATM contracts",
            "liq_mult": mult,
            "liq_status": status,
            "veto": status,
        })
        reasons.append("No contracts within 5% of spot; liquidity UNKNOWN (no_liq_data).")
        return mult, metrics, reasons, status

    spreads = []
    total_volume, total_oi = 0, 0
    for opt in atm:
        bid, ask = opt.get("bid") or 0.0, opt.get("ask") or 0.0
        mid = (bid + ask) / 2.0
        # Require real two-sided quotes; zero/stale bid from fillna(0) is not a spread
        if mid > 0 and ask >= bid > 0:
            spreads.append((ask - bid) / mid)
        total_volume += int(opt.get("volume") or 0)
        total_oi += int(opt.get("openInterest") or 0)

    if not spreads:
        # Data outage / all zero bids — NOT untradeable 100% spread
        mult, status = _liq_mult_from_spread(None)
        metrics.update({
            "atm_contracts": len(atm),
            "median_atm_spread_pct": None,
            "usable_spread_quotes": 0,
            "total_atm_volume": total_volume,
            "total_atm_open_interest": total_oi,
            "spot": spot,
            "liq_mult": mult,
            "liq_status": status,
            "veto": status,
        })
        reasons.append(
            f"ATM band has {len(atm)} contracts but 0 usable bid/ask pairs; "
            "liquidity UNKNOWN (no_liq_data)."
        )
        return mult, metrics, reasons, status

    med_spread = sorted(spreads)[len(spreads) // 2]
    mult, status = _liq_mult_from_spread(med_spread)
    metrics.update({
        "atm_contracts": len(atm),
        "median_atm_spread_pct": round(med_spread * 100, 2),
        "usable_spread_quotes": len(spreads),
        "total_atm_volume": total_volume,
        "total_atm_open_interest": total_oi,
        "spot": spot,
        "liq_mult": mult,
        "liq_status": status,
        "veto": status,
    })
    if status == "spread_untradeable":
        reasons.append(
            f"Median ATM spread {med_spread * 100:.1f}% → liq_mult 0 "
            f"(spread_untradeable); ATM vol {total_volume:,}, OI {total_oi:,}."
        )
    else:
        reasons.append(
            f"Median ATM spread {med_spread * 100:.1f}% → liq_mult {mult:.2f}"
            f"; ATM vol {total_volume:,}, OI {total_oi:,}."
        )
    return mult, metrics, reasons, status


def _parse_pct_change(pivot_data) -> tuple[float | None, str | None]:
    """
    Returns (pct_change | None, mom_status).

    mom_status:
      None              — usable day-change for mom_sub
      "no_momentum_data"— missing / unusable; do NOT score mom_sub as 0

    Exactly 0.0 with live spot+pivot is treated as UNKNOWN (partial failure
    costume), not a measured flat day — see Tuesday 2026-08-11 forensics.
    """
    raw = pivot_data.get("pct_change", None)
    if raw is None:
        return None, "no_momentum_data"
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return None, "no_momentum_data"
    if math.isnan(pct) or math.isinf(pct):
        return None, "no_momentum_data"

    try:
        close = float(pivot_data.get("close") or 0.0)
        pivot = float(pivot_data.get("pivot") or 0.0)
    except (TypeError, ValueError):
        close, pivot = 0.0, 0.0

    # Placeholder failure (close=pivot=100) is not "live"
    looks_live = (
        close > 0
        and pivot > 0
        and not (abs(close - 100.0) < 1e-9 and abs(pivot - 100.0) < 1e-9)
    )
    # Exactly 0.0 while spot/pivot live → data failure, not a flat day
    if looks_live and pct == 0.0:
        return None, "no_momentum_data"
    return pct, None


# ------------------------------------------------------------------
# Technical: directional, ATR-normalised, 0..TECH_CEIL
# ------------------------------------------------------------------
def score_technical(
    pivot_data,
    atr_pct=None,
    atr_abs=None,
    direction_sign=1.0,
    drift_pct=None,
):
    """
    Returns (T points, metrics, reasons, mom_status).

    pivot_sub is 0 with no evidence; mom_sub is only scored when pct_change
    is known. drift_sub is 30-minute direction-of-travel (None at the open).
    When drift is missing, mom weight falls back to (1 - W_PIVOT) so the
    mix stays 70/30. vol_mult multiplies technical only.

    Queued: whether W_PIVOT=0.70 should stay this high — Aug 21 AAPL 09:16
    survived almost entirely on pivot distance while 30m drift was against.
    """
    reasons, metrics = [], {}
    close = float(pivot_data.get("close") or 0.0)
    pivot = float(pivot_data.get("pivot") or close)
    r1 = float(pivot_data.get("r1") or close)
    s1 = float(pivot_data.get("s1") or close)
    pct_change, mom_status = _parse_pct_change(pivot_data)
    sign = 1.0 if direction_sign >= 0 else -1.0

    pivot_scale = _cfg_float("PIVOT_SCALE", 0.40)
    pivot_power = _cfg_float("PIVOT_POWER", 1.0)
    mom_scale = _cfg_float("MOM_SCALE", 0.45)
    w_pivot = _cfg_float("W_PIVOT", 0.70)
    w_mom = _cfg_float("W_MOM", 0.18)
    w_drift = _cfg_float("W_DRIFT", 0.12)
    drift_scale = _cfg_float("DRIFT_SCALE", 0.25)
    tech_ceil = _cfg_float("TECH_CEIL", 85.0)

    atr = None
    if atr_abs is not None:
        try:
            atr = float(atr_abs)
        except (TypeError, ValueError):
            atr = None
    if atr is None or atr <= 0:
        if atr_pct is not None and close:
            try:
                atr = abs(close) * float(atr_pct) / 100.0
            except (TypeError, ValueError):
                atr = None
        if atr is None or atr <= 0:
            # Do not invent 1.2% of spot. Caller must hard-PASS no_atr_data.
            metrics.update({
                "atr": None,
                "atr_missing": True,
                "pivot_sub": 0.0,
                "mom_sub": None,
                "drift_sub": None,
                "vol_mult": None,
                "tech_raw": 0.0,
                "atr_distance": None,
                "atr_distance_signed": None,
            })
            reasons.append("no_atr_data — refusing fabricated ATR")
            return 0.0, metrics, reasons, None

    dist_signed = sign * (close - pivot) / atr if atr else 0.0
    dist_abs = abs(close - pivot) / atr if atr else 0.0

    pivot_sub = _ramp(dist_signed, pivot_scale, pivot_power)
    drift_sub = None
    drift_used = False
    try:
        if drift_pct is not None and drift_pct != "":
            dlt = float(drift_pct)
            if dlt == dlt and abs(dlt) < 50:
                drift_sub = _ramp(sign * dlt, drift_scale, 1.0)
                drift_used = True
    except (TypeError, ValueError):
        drift_sub = None
        drift_used = False

    if mom_status is None and pct_change is not None:
        mom_sub = _ramp(sign * pct_change, mom_scale, 1.0)
        if drift_used:
            tech_raw = (
                w_pivot * pivot_sub + w_mom * mom_sub + w_drift * float(drift_sub)
            )
        else:
            # Open / missing 30m bars: do not haircut the open drive.
            tech_raw = w_pivot * pivot_sub + (1.0 - w_pivot) * mom_sub
    else:
        # Do not silently score mom_sub=0 — momentum excluded (forensic only)
        mom_sub = None
        tech_raw = w_pivot * pivot_sub  # incomplete; block_reason set by score_ticker
    vol_m = _vol_mult(atr_pct)
    T = tech_raw * vol_m * tech_ceil

    metrics.update({
        "close": round(close, 2),
        "pivot": round(pivot, 2),
        "r1": round(r1, 2),
        "s1": round(s1, 2),
        "pct_change": None if pct_change is None else round(pct_change, 2),
        "mom_status": mom_status,
        "atr_pct": atr_pct,
        "atr_abs": round(atr, 4) if atr else None,
        "direction_sign": sign,
        "atr_distance": round(dist_abs, 4),
        "atr_distance_signed": round(dist_signed, 4),
        "pivot_sub": round(pivot_sub, 4),
        "mom_sub": None if mom_sub is None else round(mom_sub, 4),
        "drift_sub": None if drift_sub is None else round(drift_sub, 4),
        "drift_pct": None if not drift_used else round(float(drift_pct), 3),
        "drift_used": drift_used,
        "vol_mult": round(vol_m, 4),
        "tech_raw": round(tech_raw, 4),
        "T": round(T, 2),
        "pivot_scale": pivot_scale,
        "pivot_power": pivot_power,
        "mom_scale": mom_scale,
        "w_pivot": w_pivot,
        "w_mom": w_mom,
        "w_drift": w_drift,
        "drift_scale": drift_scale,
        "tech_ceil": tech_ceil,
    })
    if not close or not pivot:
        reasons.append("Missing price/pivot data.")
        return 0.0, metrics, reasons, mom_status

    if mom_status == "no_momentum_data":
        reasons.append(
            f"dir={'C' if sign > 0 else 'P'}; ATR-dist {dist_signed:+.2f}; "
            f"pivot_sub {pivot_sub:.3f}; mom_sub UNKNOWN (no_momentum_data); "
            f"vol_mult {vol_m:.2f} → T_partial={T:.1f}/{tech_ceil:g}."
        )
    else:
        reasons.append(
            f"dir={'C' if sign > 0 else 'P'}; ATR-dist {dist_signed:+.2f}; "
            f"pivot_sub {pivot_sub:.3f}; mom_sub {mom_sub:.3f} "
            f"(pct {pct_change:+.2f}%); "
            + (
                f"drift_sub {drift_sub:.3f} (30m {float(drift_pct):+.2f}%); "
                if drift_used
                else "drift n/a; "
            )
            + f"vol_mult {vol_m:.2f} → T={T:.1f}/{tech_ceil:g}."
        )
    return T, metrics, reasons, mom_status


# ------------------------------------------------------------------
# Sentiment: signed modifier -SENT_MAX..+SENT_MAX; 0 when no evidence
# ------------------------------------------------------------------
def score_sentiment(headlines_text, macro_vector="", futures_pct=None, direction_sign=1.0):
    """
    Returns (S points, metrics, reasons).
    Neutral / missing components contribute 0 and are excluded from the average.
    Signed so adverse news subtracts from a call (and vice versa).
    """
    reasons, metrics = [], {}
    sign = 1.0 if direction_sign >= 0 else -1.0
    sent_max = _cfg_float("SENT_MAX", 15.0)
    text = (headlines_text or "").lower()
    lines = [l for l in text.splitlines() if l.strip()]
    bull = sum(1 for l in lines for w in _BULLISH_WORDS if w in l)
    bear = sum(1 for l in lines for w in _BEARISH_WORDS if w in l)

    parts: list[float] = []
    weights: list[float] = []

    if lines or (bull + bear) > 0:
        n = max(bull + bear, 1)
        raw_news = _clamp((bull - bear) / n, -1.0, 1.0)  # + = bullish
        parts.append(sign * raw_news)
        weights.append(0.55)
        news_aligned = sign * raw_news
    else:
        news_aligned = 0.0

    if futures_pct is not None:
        try:
            raw_f = _clamp(float(futures_pct) / 0.75, -1.0, 1.0)
        except (TypeError, ValueError):
            raw_f = 0.0
        parts.append(sign * raw_f)
        weights.append(0.25)
        fut_aligned = sign * raw_f
    else:
        fut_aligned = None  # excluded

    mv = (macro_vector or "").upper()
    if "EXPANSIONARY_TAILWIND" in mv:
        raw_m, macro_note = 1.0, "expansionary tailwind"
    elif "SUPPLY_CHAIN_BOTTLENECK" in mv:
        raw_m, macro_note = -0.7, "supply-chain bottleneck"
    elif "EARNINGS_IMMINENT" in mv:
        raw_m, macro_note = -0.5, "earnings imminent (IV-crush risk)"
    else:
        raw_m, macro_note = 0.0, "neutral macro"

    if abs(raw_m) > 1e-9:
        parts.append(sign * raw_m)
        weights.append(0.20)
    macro_aligned = sign * raw_m if abs(raw_m) > 1e-9 else 0.0

    if parts:
        wsum = sum(weights)
        aligned = sum(p * (w / wsum) for p, w in zip(parts, weights))
    else:
        aligned = 0.0

    S = aligned * sent_max
    metrics.update({
        "headline_count": len(lines),
        "bullish_hits": bull,
        "bearish_hits": bear,
        "news_aligned": round(news_aligned, 4),
        "futures_pct": futures_pct,
        "futures_aligned": None if fut_aligned is None else round(fut_aligned, 4),
        "macro_note": macro_note,
        "macro_aligned": round(macro_aligned, 4),
        "sentiment_aligned": round(aligned, 4),
        "S": round(S, 2),
        "sent_max": sent_max,
    })
    reasons.append(
        f"{len(lines)} headlines ({bull} bull/{bear} bear); "
        f"futures {'n/a' if fut_aligned is None else f'{futures_pct}%'}; "
        f"{macro_note}; aligned {aligned:+.2f} → S={S:+.1f}/{sent_max:g}."
    )
    return S, metrics, reasons


# Scorer-level hard blocks (gate compact tags). Priority high → low.
# Liquidity is no longer a score block — Part C rejects the chosen contract.
# no_pivot_data / no_atr_data: missing market data — never invent a score.
_BLOCK_PRIORITY = (
    "no_pivot_data",
    "no_atr_data",
    "no_momentum_data",
    "dead_zone",
)


def _pick_block_reason(*candidates: str | None) -> str | None:
    for key in _BLOCK_PRIORITY:
        if key in candidates:
            return key
    for c in candidates:
        if c:
            return c
    return None


def _pivot_unusable(pivot_data) -> str | None:
    """None if close/pivot are real positives. Else no_pivot_data."""
    if not isinstance(pivot_data, dict):
        return "no_pivot_data"
    err = pivot_data.get("error")
    if err:
        return "no_pivot_data"
    try:
        close = float(pivot_data.get("close"))
        pivot = float(pivot_data.get("pivot"))
    except (TypeError, ValueError):
        return "no_pivot_data"
    if close != close or pivot != pivot:  # NaN
        return "no_pivot_data"
    if close <= 0 or pivot <= 0:
        return "no_pivot_data"
    return None


def _atr_unusable(atr_abs, atr_pct) -> str | None:
    """None if atr_abs or atr_pct is a positive number. Else no_atr_data."""
    for raw in (atr_abs, atr_pct):
        if raw is None or raw == "":
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0 and v == v:
            return None
    return "no_atr_data"


def data_fail_card(ticker, block_reason, options_dict=None, pivot_data=None):
    """PASS card for missing market data. total_score=0 must not be used as live_score."""
    tech_ceil = _cfg_float("TECH_CEIL", 85.0)
    sent_max = _cfg_float("SENT_MAX", 15.0)
    card = ScoreCard(
        ticker=str(ticker),
        weights={"liquidity": 0, "technical": tech_ceil, "sentiment": sent_max},
    )
    card.action_flag = "PASS"
    card.block_reason = str(block_reason or "no_pivot_data")
    card.total_score = 0.0
    card.technical_score = 0.0
    card.sentiment_score = 0.0
    card.reasons = [f"PASS: {card.block_reason} — refusing fabricated market data"]
    card.metrics = {
        "subscores": {
            "block_reason": card.block_reason,
            "final": 0.0,
            "T": 0.0,
            "S": 0.0,
        },
        "liquidity": {},
        "technical": {},
        "sentiment": {},
    }
    return card


# ------------------------------------------------------------------
# Composite
# ------------------------------------------------------------------
def score_ticker(ticker, options_dict, pivot_data, headlines_text,
                 macro_vector="", futures_pct=None, atr_pct=None, atr_abs=None,
                 weights=None, drift_pct=None):
    """
    Score = clamp(T + S, 0, 100). Liquidity is not a term.

    Chain-level ATM aggregates (atm_n / med_spr / usable) are computed for
    Discord forensics only. A wide chain median does not zero the score and
    does not hard-PASS. Contract tradability is Part C.

    Data failure still hard-PASS: no_momentum_data.
    Dead zone: |spot−pivot|/ATR < DEAD_ZONE_ATR → PASS + block_reason=dead_zone.

    ``weights`` is accepted for call-site compatibility but IGNORED — the
    30/40/30 additive scheme is retired.
    """
    if weights is not None:
        pass  # explicit ignore

    pivot_fail = _pivot_unusable(pivot_data)
    if pivot_fail:
        print(f"REJECT {ticker}:{pivot_fail}")
        return data_fail_card(ticker, pivot_fail, options_dict, pivot_data)
    atr_fail = _atr_unusable(atr_abs, atr_pct)
    if atr_fail:
        print(f"REJECT {ticker}:{atr_fail}")
        return data_fail_card(ticker, atr_fail, options_dict, pivot_data)

    tech_ceil = _cfg_float("TECH_CEIL", 85.0)
    sent_max = _cfg_float("SENT_MAX", 15.0)
    dead_zone_atr = _cfg_float("DEAD_ZONE_ATR", 0.30)
    threshold = float(getattr(config, "EXECUTE_THRESHOLD", 70))

    card = ScoreCard(
        ticker=ticker,
        weights={"liquidity": 0, "technical": tech_ceil, "sentiment": sent_max},
    )
    direction_sign, direction_label = _infer_direction_sign(pivot_data)

    liq_mult, lm, lreasons, liq_status = score_liquidity(options_dict)
    T, tm, treasons, mom_status = score_technical(
        pivot_data,
        atr_pct=atr_pct,
        atr_abs=atr_abs,
        direction_sign=direction_sign,
        drift_pct=drift_pct,
    )
    S, sm, sreasons = score_sentiment(
        headlines_text, macro_vector, futures_pct, direction_sign=direction_sign,
    )

    raw = T + S
    total = _clamp(raw, 0.0, 100.0)
    # Chain-median liq is forensic only — never a multiplier or 0.0 costume.
    if liq_status == "no_liq_data" or liq_mult is None:
        liq_display = None
    else:
        liq_display = float(liq_mult)

    atr_dist = tm.get("atr_distance")
    in_dead_zone = (
        atr_dist is not None
        and dead_zone_atr > 0
        and float(atr_dist) < float(dead_zone_atr)
    )

    card.liquidity_ratio = 0.0 if liq_display is None else liq_display
    card.technical_ratio = round(tm.get("tech_raw", 0.0), 4)
    card.sentiment_ratio = round(sm.get("sentiment_aligned", 0.0), 4)
    card.liquidity_score = (
        None if liq_display is None else round(liq_display, 4)
    )
    # ScoreCard fields are floats in dataclass — store 0 for unknown liq display
    # but keep None in metrics/subscores for forensics
    card.liquidity_score = 0.0 if liq_display is None else round(liq_display, 4)
    card.technical_score = round(T, 1)
    card.sentiment_score = round(S, 1)
    card.total_score = round(float(total), 1)

    # Liquidity is not a scorer block (Part C / CRITICAL use liq_status).
    dz_block = "dead_zone" if in_dead_zone else None
    card.block_reason = _pick_block_reason(mom_status, dz_block)

    if card.block_reason:
        card.action_flag = "PASS"
    else:
        card.action_flag = (
            "EXECUTE" if card.total_score >= threshold else "PASS"
        )

    subscores = {
        "pivot_sub": tm.get("pivot_sub"),
        "mom_sub": tm.get("mom_sub"),
        "drift_sub": tm.get("drift_sub"),
        "drift_pct": tm.get("drift_pct"),
        "vol_mult": tm.get("vol_mult"),
        "T": round(T, 2),
        "S": round(S, 2),
        "liq_mult": liq_display,
        "liq_status": liq_status,
        "atm_n": lm.get("atm_contracts"),
        "med_spr": lm.get("median_atm_spread_pct"),
        "usable": lm.get("usable_spread_quotes"),
        "mom_status": mom_status,
        "atr_distance": tm.get("atr_distance"),
        "atr_distance_signed": tm.get("atr_distance_signed"),
        "direction": direction_label,
        "raw_T_plus_S": round(raw, 2),
        "final": card.total_score,
        "dead_zone": in_dead_zone,
        "dead_zone_atr": dead_zone_atr,
        "block_reason": card.block_reason,
    }
    card.metrics = {
        "liquidity": lm,
        "technical": tm,
        "sentiment": sm,
        "subscores": subscores,
    }
    card.reasons = lreasons + treasons + sreasons
    if mom_status == "no_momentum_data":
        card.reasons.append(
            "PASS: no_momentum_data — pct_change missing/0 with live spot; "
            "mom_sub not scored as flat."
        )
    if in_dead_zone:
        card.reasons.append(
            f"Dead zone: ATR-dist {atr_dist:.4f} < {dead_zone_atr:g} "
            f"— direction undefined; hard PASS."
        )

    mom_log = "n/a" if tm.get("mom_sub") is None else tm.get("mom_sub")
    liq_log = "n/a" if liq_display is None else liq_display
    atm_n = lm.get("atm_contracts")
    med_spr = lm.get("median_atm_spread_pct")
    usable = lm.get("usable_spread_quotes")
    med_s = "n/a" if med_spr is None else f"{med_spr}%"
    br = f" {card.block_reason}" if card.block_reason else ""
    # Mandatory per-scan sub-score log (forensic reconstruction depends on this)
    print(
        f"[{ticker}] score subs "
        f"piv={subscores['pivot_sub']} mom={mom_log} "
        f"dft={tm.get('drift_sub') if tm.get('drift_sub') is not None else 'n/a'} "
        f"d30={tm.get('drift_pct') if tm.get('drift_pct') is not None else 'n/a'} "
        f"vol={subscores['vol_mult']} T={subscores['T']} S={subscores['S']} "
        f"liq={liq_log} dATR={subscores['atr_distance']} "
        f"atm_n={atm_n if atm_n is not None else 'n/a'} "
        f"med_spr={med_s} usable={usable if usable is not None else 'n/a'} "
        f"dir={direction_label} final={subscores['final']}"
        f"{br} → {card.action_flag}"
    )
    return card


def apply_adversarial_penalty(card, penalty=15.0, reason=""):
    """Devil's Advocate veto: subtract penalty and re-evaluate the flag."""
    threshold = float(getattr(config, "EXECUTE_THRESHOLD", 70))
    card.adversarial_penalty = penalty
    card.total_score = round(max(0.0, card.total_score - penalty), 1)
    # Data / dead-zone hard PASS stays PASS; otherwise re-check threshold
    if card.block_reason in _BLOCK_PRIORITY:
        card.action_flag = "PASS"
    else:
        card.action_flag = (
            "EXECUTE" if card.total_score >= threshold else "PASS"
        )
    if reason:
        card.reasons.append(f"Adversarial veto (-{penalty:g} pts): {reason}")
    if isinstance(card.metrics.get("subscores"), dict):
        card.metrics["subscores"]["final"] = card.total_score
        card.metrics["subscores"]["adversarial_penalty"] = penalty
    return card


def format_subscore_bits(card) -> str:
    """Compact piv=/mom=/vol=/T=/S=/liq=/dATR=/atm_n=/med_spr=/usable=."""
    sub = (card.metrics or {}).get("subscores") or {}
    tm = (card.metrics or {}).get("technical") or {}
    lm = (card.metrics or {}).get("liquidity") or {}

    def _g(key, alt=None):
        v = sub.get(key)
        if v is None and alt is not None:
            v = tm.get(alt)
        return v

    piv = _g("pivot_sub")
    mom = _g("mom_sub")
    vol = _g("vol_mult")
    T = _g("T")
    if T is None:
        T = getattr(card, "technical_score", None)
    S = _g("S")
    if S is None:
        S = getattr(card, "sentiment_score", None)
    liq = _g("liq_mult")
    # Unknown liq is None in subscores — do not fall back to 0.0 card field
    d_atr = _g("atr_distance")
    atm_n = sub.get("atm_n")
    if atm_n is None:
        atm_n = lm.get("atm_contracts")
    med_spr = sub.get("med_spr")
    if med_spr is None:
        med_spr = lm.get("median_atm_spread_pct")
    usable = sub.get("usable")
    if usable is None:
        usable = lm.get("usable_spread_quotes")

    def _f(v, nd=2):
        if v is None:
            return "n/a"
        try:
            return f"{float(v):.{nd}f}"
        except (TypeError, ValueError):
            return "n/a"

    def _i(v):
        if v is None:
            return "n/a"
        try:
            return str(int(v))
        except (TypeError, ValueError):
            return "n/a"

    dft = _g("drift_sub")
    d30 = sub.get("drift_pct")
    if d30 is None:
        d30 = tm.get("drift_pct")
    med_s = "n/a" if med_spr is None else f"{_f(med_spr, 1)}%"
    bits = (
        f"piv={_f(piv, 3)} mom={_f(mom, 3)} dft={_f(dft, 3)} "
        f"d30={_f(d30, 2)} vol={_f(vol, 2)} "
        f"T={_f(T, 1)} S={_f(S, 1)} liq={_f(liq, 2)} dATR={_f(d_atr, 3)} "
        f"atm_n={_i(atm_n)} med_spr={med_s} usable={_i(usable)}"
    )
    br = sub.get("block_reason") or getattr(card, "block_reason", None)
    if br:
        bits += f" block={br}"
    return bits


def metrics_snapshot_text(card, *, include_futures=True):
    """Compact snapshot for CEO prompt — includes calibrated sub-scores."""
    lm = card.metrics.get("liquidity", {})
    tm = card.metrics.get("technical", {})
    sm = card.metrics.get("sentiment", {})
    sub = card.metrics.get("subscores", {})
    tech_ceil = _cfg_float("TECH_CEIL", 85.0)
    sent_max = _cfg_float("SENT_MAX", 15.0)
    if include_futures:
        sentiment_line = (
            f"- Headlines scanned: {sm.get('headline_count')} ({sm.get('bullish_hits')} bullish / "
            f"{sm.get('bearish_hits')} bearish) | Futures: {sm.get('futures_pct')}% | "
            f"Macro: {sm.get('macro_note')}\n"
        )
    else:
        sentiment_line = (
            f"- Headlines scanned: {sm.get('headline_count')} ({sm.get('bullish_hits')} bullish / "
            f"{sm.get('bearish_hits')} bearish) | Macro: {sm.get('macro_note')}\n"
            f"- Session-open ES/NQ futures already briefed earlier; do not restate them.\n"
        )
    dz = " yes" if sub.get("dead_zone") else " no"
    return (
        f"RAW METRICS SNAPSHOT for {card.ticker} (cite these numbers verbatim):\n"
        f"- Spot: {tm.get('close')} | Pivot: {tm.get('pivot')} | R1: {tm.get('r1')} | "
        f"S1: {tm.get('s1')} | Day change: {tm.get('pct_change')}% | ATR%: {tm.get('atr_pct')}\n"
        f"- ATR-distance: {sub.get('atr_distance')} | dir: {sub.get('direction')} | "
        f"pivot_sub: {sub.get('pivot_sub')} | mom_sub: {sub.get('mom_sub')} | "
        f"vol_mult: {sub.get('vol_mult')} | dead_zone:{dz}\n"
        f"- Median ATM spread: {lm.get('median_atm_spread_pct')}% | ATM n: "
        f"{lm.get('atm_contracts')} | usable quotes: {lm.get('usable_spread_quotes')} | "
        f"ATM volume: {lm.get('total_atm_volume')} | "
        f"ATM open interest: {lm.get('total_atm_open_interest')} "
        f"(chain-median forensic; not in score)\n"
        f"{sentiment_line}"
        f"- Score: T={card.technical_score}/{tech_ceil:g} + S={card.sentiment_score:+g}/{sent_max:g} "
        f"(adversarial -{card.adversarial_penalty:g}) → TOTAL {card.total_score}/100 → {card.action_flag}"
    )
