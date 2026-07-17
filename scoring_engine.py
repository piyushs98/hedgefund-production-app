"""
scoring_engine.py — Deterministic Multi-Factor Weighted Scoring (Task 2).

WHY THIS EXISTS
---------------
The previous engine graded tickers by substring-matching LLM prose:

    if "Low Risk" in risk_report: ...
    if ticker in ticker_manager_report and "warning" not in ticker_manager_report.lower(): ...

That second check was the root cause of the "hyper-conservative" behavior:
the Ticker Team Manager's own report template contains a section literally
titled "Technical Warning Flags", so the word "warning" appears in almost
every report and the 20-point technical bonus was almost never awarded.
Max realistic scores hovered around 50-65 -> systematic PASS.

This module scores from RAW NUMBERS instead of prose. Each pillar produces
a normalized ratio in [0, 1] plus a metrics dict, then the ratio is scaled
by the dynamic pillar weight (loaded from config / saturday_audit feedback).
Every sub-factor is GRADED (partial credit), never binary — a single wide
metric shaves points instead of killing the trade.

Pillars (default weights, dynamically adjustable):
    Liquidity & Order Book Depth ... 30
    Technical Momentum & Volatility  40
    Macro & Sector Sentiment ....... 30
Threshold: total >= 70 -> EXECUTE else PASS.
"""

import math
from dataclasses import dataclass, field

import config

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
    liquidity_ratio: float = 0.0      # 0..1
    technical_ratio: float = 0.0      # 0..1
    sentiment_ratio: float = 0.0      # 0..1
    liquidity_score: float = 0.0      # ratio * weight
    technical_score: float = 0.0
    sentiment_score: float = 0.0
    adversarial_penalty: float = 0.0
    total_score: float = 0.0
    action_flag: str = "PASS"
    weights: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)   # raw numbers for CEO prompt + telemetry
    reasons: list = field(default_factory=list)


# ------------------------------------------------------------------
# Pillar 1: Liquidity & Order Book Depth  (graded, max ratio 1.0)
# ------------------------------------------------------------------
def score_liquidity(options_dict):
    """
    Grades the near-the-money option chain:
      * median bid/ask spread as % of mid  (60% of pillar)
      * total ATM volume                    (25% of pillar)
      * total ATM open interest             (15% of pillar)
    Returns (ratio 0..1, metrics dict, reasons list).
    """
    reasons, metrics = [], {}
    spot = options_dict.get("current_price")
    chains = options_dict.get("chains", {})
    if not isinstance(spot, (int, float)) or not chains:
        return 0.0, {"error": "no usable chain/spot"}, ["No usable options chain data."]

    # Collect contracts within ±5% of spot across the nearest expirations
    atm = []
    for exp, sides in chains.items():
        for side in ("calls", "puts"):
            for opt in sides.get(side, []):
                strike = opt.get("strike") or 0
                if strike and abs(strike - spot) / spot <= 0.05:
                    atm.append(opt)
    if not atm:
        return 0.0, {"error": "no ATM contracts"}, ["No contracts within 5% of spot."]

    spreads = []
    total_volume, total_oi = 0, 0
    for opt in atm:
        bid, ask = opt.get("bid") or 0.0, opt.get("ask") or 0.0
        mid = (bid + ask) / 2.0
        if mid > 0 and ask >= bid > 0:
            spreads.append((ask - bid) / mid)
        total_volume += int(opt.get("volume") or 0)
        total_oi += int(opt.get("openInterest") or 0)

    med_spread = sorted(spreads)[len(spreads) // 2] if spreads else 1.0
    metrics.update({
        "atm_contracts": len(atm),
        "median_atm_spread_pct": round(med_spread * 100, 2),
        "total_atm_volume": total_volume,
        "total_atm_open_interest": total_oi,
        "spot": spot,
    })

    # Graded sub-scores (no cliffs):
    # spread: 2% or tighter -> full credit, 15%+ -> zero, linear between
    spread_sub = max(0.0, min(1.0, (0.15 - med_spread) / 0.13))
    # volume: log-scaled, 10k ATM contracts -> full credit
    vol_sub = max(0.0, min(1.0, math.log10(max(total_volume, 1)) / 4.0))
    # open interest: 50k -> full credit
    oi_sub = max(0.0, min(1.0, math.log10(max(total_oi, 1)) / 4.7))

    ratio = 0.60 * spread_sub + 0.25 * vol_sub + 0.15 * oi_sub
    reasons.append(
        f"Median ATM spread {med_spread*100:.1f}% (sub {spread_sub:.2f}), "
        f"ATM volume {total_volume:,} (sub {vol_sub:.2f}), OI {total_oi:,} (sub {oi_sub:.2f})."
    )
    return ratio, metrics, reasons


# ------------------------------------------------------------------
# Pillar 2: Technical Momentum & Volatility  (graded, max ratio 1.0)
# ------------------------------------------------------------------
def score_technical(pivot_data, atr_pct=None):
    """
    Grades price structure from ticker_desk pivot data:
      * position vs daily pivot, distance-scaled     (45%)
      * day-over-day momentum (pct_change)           (35%)
      * volatility regime via ATR% (if provided)     (20%)
    """
    reasons, metrics = [], {}
    close = pivot_data.get("close", 0.0)
    pivot = pivot_data.get("pivot", close)
    r1, s1 = pivot_data.get("r1", close), pivot_data.get("s1", close)
    pct_change = pivot_data.get("pct_change", 0.0)

    metrics.update({
        "close": close, "pivot": pivot, "r1": r1, "s1": s1,
        "pct_change": pct_change, "atr_pct": atr_pct,
    })
    if not close or not pivot:
        return 0.0, metrics, ["Missing price/pivot data."]

    # Position vs pivot: at/above pivot earns credit scaling toward R1;
    # below pivot decays toward S1 (partial credit, not zero at first tick below).
    if close >= pivot:
        span = max(r1 - pivot, 1e-9)
        pivot_sub = 0.6 + 0.4 * min(1.0, (close - pivot) / span)
    else:
        span = max(pivot - s1, 1e-9)
        pivot_sub = max(0.0, 0.6 * (1 - (pivot - close) / span))

    # Momentum: +2% day move -> full credit; -2% -> zero; linear
    mom_sub = max(0.0, min(1.0, (pct_change + 2.0) / 4.0))

    # Volatility regime: sweet spot ATR% in [1%, 4%] — enough movement to pay
    # the premium, not so much that stops get shredded.
    if atr_pct is None:
        vol_sub = 0.5  # unknown -> neutral, never punitive
    elif 1.0 <= atr_pct <= 4.0:
        vol_sub = 1.0
    elif atr_pct < 1.0:
        vol_sub = max(0.0, atr_pct / 1.0)
    else:
        vol_sub = max(0.0, 1.0 - (atr_pct - 4.0) / 4.0)

    ratio = 0.45 * pivot_sub + 0.35 * mom_sub + 0.20 * vol_sub
    reasons.append(
        f"Close {close} vs pivot {pivot} (sub {pivot_sub:.2f}); day move {pct_change:+.2f}% "
        f"(sub {mom_sub:.2f}); ATR% {atr_pct if atr_pct is not None else 'n/a'} (sub {vol_sub:.2f})."
    )
    return ratio, metrics, reasons


# ------------------------------------------------------------------
# Pillar 3: Macro & Sector Sentiment  (graded, max ratio 1.0)
# ------------------------------------------------------------------
def score_sentiment(headlines_text, macro_vector="", futures_pct=None):
    """
    Grades news flow deterministically:
      * lexicon-scored headline balance over DB window   (55%)
      * overnight futures direction                       (25%)
      * Innovation Hub macro vector modifiers             (20%, graded — not
        the old hard zero-override)
    """
    reasons, metrics = [], {}
    text = (headlines_text or "").lower()
    lines = [l for l in text.splitlines() if l.strip()]
    bull = sum(1 for l in lines for w in _BULLISH_WORDS if w in l)
    bear = sum(1 for l in lines for w in _BEARISH_WORDS if w in l)
    n = max(bull + bear, 1)
    news_sub = 0.5 + 0.5 * ((bull - bear) / n)          # 0..1, 0.5 = balanced
    news_sub = max(0.0, min(1.0, news_sub))

    if futures_pct is None:
        fut_sub = 0.5
    else:
        fut_sub = max(0.0, min(1.0, (futures_pct + 0.75) / 1.5))  # ±0.75% band

    mv = (macro_vector or "").upper()
    if "EXPANSIONARY_TAILWIND" in mv:
        macro_sub, macro_note = 1.0, "expansionary tailwind"
    elif "SUPPLY_CHAIN_BOTTLENECK" in mv:
        macro_sub, macro_note = 0.15, "supply-chain bottleneck"
    elif "EARNINGS_IMMINENT" in mv:
        macro_sub, macro_note = 0.25, "earnings imminent (IV-crush risk)"
    else:
        macro_sub, macro_note = 0.5, "neutral macro"

    ratio = 0.55 * news_sub + 0.25 * fut_sub + 0.20 * macro_sub
    metrics.update({
        "headline_count": len(lines), "bullish_hits": bull, "bearish_hits": bear,
        "news_sub": round(news_sub, 3), "futures_pct": futures_pct,
        "macro_note": macro_note,
    })
    reasons.append(
        f"{len(lines)} headlines: {bull} bullish / {bear} bearish hits (sub {news_sub:.2f}); "
        f"futures {futures_pct if futures_pct is not None else 'n/a'} (sub {fut_sub:.2f}); {macro_note} (sub {macro_sub:.2f})."
    )
    return ratio, metrics, reasons


# ------------------------------------------------------------------
# Composite
# ------------------------------------------------------------------
def score_ticker(ticker, options_dict, pivot_data, headlines_text,
                 macro_vector="", futures_pct=None, atr_pct=None, weights=None):
    """Runs all three pillars and assembles the final ScoreCard."""
    w = weights or config.load_weights()
    card = ScoreCard(ticker=ticker, weights=dict(w))

    lr, lm, lreasons = score_liquidity(options_dict)
    tr, tm, treasons = score_technical(pivot_data, atr_pct=atr_pct)
    sr, sm, sreasons = score_sentiment(headlines_text, macro_vector, futures_pct)

    card.liquidity_ratio, card.technical_ratio, card.sentiment_ratio = lr, tr, sr
    card.liquidity_score = round(lr * w["liquidity"], 1)
    card.technical_score = round(tr * w["technical"], 1)
    card.sentiment_score = round(sr * w["sentiment"], 1)
    card.total_score = round(card.liquidity_score + card.technical_score + card.sentiment_score, 1)
    card.action_flag = "EXECUTE" if card.total_score >= config.EXECUTE_THRESHOLD else "PASS"
    card.metrics = {"liquidity": lm, "technical": tm, "sentiment": sm}
    card.reasons = lreasons + treasons + sreasons
    return card


def apply_adversarial_penalty(card, penalty=15.0, reason=""):
    """Devil's Advocate veto: subtract penalty and re-evaluate the flag."""
    card.adversarial_penalty = penalty
    card.total_score = round(max(0.0, card.total_score - penalty), 1)
    card.action_flag = "EXECUTE" if card.total_score >= config.EXECUTE_THRESHOLD else "PASS"
    if reason:
        card.reasons.append(f"Adversarial veto (-{penalty:g} pts): {reason}")
    return card


def metrics_snapshot_text(card):
    """Compact, exact-numbers snapshot injected into the CEO prompt so the
    final broadcast can cite real metrics instead of generic prose (Task 1)."""
    lm = card.metrics.get("liquidity", {})
    tm = card.metrics.get("technical", {})
    sm = card.metrics.get("sentiment", {})
    return (
        f"RAW METRICS SNAPSHOT for {card.ticker} (cite these numbers verbatim):\n"
        f"- Spot: {tm.get('close')} | Pivot: {tm.get('pivot')} | R1: {tm.get('r1')} | "
        f"S1: {tm.get('s1')} | Day change: {tm.get('pct_change')}% | ATR%: {tm.get('atr_pct')}\n"
        f"- Median ATM spread: {lm.get('median_atm_spread_pct')}% | ATM volume: "
        f"{lm.get('total_atm_volume')} | ATM open interest: {lm.get('total_atm_open_interest')}\n"
        f"- Headlines scanned: {sm.get('headline_count')} ({sm.get('bullish_hits')} bullish / "
        f"{sm.get('bearish_hits')} bearish) | Futures: {sm.get('futures_pct')}% | Macro: {sm.get('macro_note')}\n"
        f"- Pillar scores: Liquidity {card.liquidity_score}/{card.weights.get('liquidity')} | "
        f"Technical {card.technical_score}/{card.weights.get('technical')} | "
        f"Sentiment {card.sentiment_score}/{card.weights.get('sentiment')} | "
        f"Adversarial penalty: -{card.adversarial_penalty:g} | TOTAL: {card.total_score}/100"
    )
