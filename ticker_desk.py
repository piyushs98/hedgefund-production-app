import os
import time
import sqlite3
from datetime import datetime

import yfinance as yf
from news_memory import get_historical_context
from yf_client import SESSION, TICKER_PACING_SECONDS

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover — py<3.9
    _ET = None

# CENTRALIZED GROQ CONFIGURATION
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Verify Groq import
try:
    from groq import Groq
    HAS_GROQ = True
except ImportError:
    HAS_GROQ = False

# ---------------------------------------------------------------------------
# Daily pivot cache: first computation per (ticker, session date) wins.
# Levels are frozen for the rest of the trading day. Live close / pct_change
# are refreshed each call so scoring still sees current spot vs a static pivot.
# Key: (TICKER, "YYYY-MM-DD") -> {pivot, r1, s1, r2, s2, basis_date, H, L, C}
# ---------------------------------------------------------------------------
_PIVOT_CACHE = {}


def _session_date_et(now=None):
    """US equity session calendar date (America/New_York)."""
    if now is None:
        now = datetime.now(_ET) if _ET is not None else datetime.now()
    elif _ET is not None and getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=_ET)
    elif _ET is not None and getattr(now, "tzinfo", None) is not None:
        now = now.astimezone(_ET)
    return now.date() if hasattr(now, "date") else now


def _bar_date(ts):
    """Calendar date of a yfinance bar index, in America/New_York when tz-aware."""
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if getattr(ts, "tzinfo", None) is not None and _ET is not None:
        return ts.astimezone(_ET).date()
    if hasattr(ts, "date"):
        return ts.date()
    return ts


def _levels_from_ohlc(high, low, close):
    """Standard floor pivots from a single completed session's OHLC."""
    if (high + low + close) > 0:
        pivot = (high + low + close) / 3.0
    else:
        pivot = 100.0
    r1 = (2 * pivot) - low if pivot > 0 else 101.0
    s1 = (2 * pivot) - high if pivot > 0 else 99.0
    r2 = pivot + (high - low) if pivot > 0 else 102.0
    s2 = pivot - (high - low) if pivot > 0 else 98.0
    return {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2),
        "s1": round(s1, 2),
        "r2": round(r2, 2),
        "s2": round(s2, 2),
    }


def _select_completed_session(hist, today):
    """
    Return (basis_dict, live_close, live_from_today_bar) from daily history.

    Basis = last bar whose date is strictly before `today` (completed session).
    live_close = today's developing close if present, else basis close.
    live_from_today_bar = True only when today's bar was present (pct_change is
    then measurable). When False, pct_change is UNKNOWN — not a flat day.
    Raises ValueError if no completed session is available.
    """
    if hist is None or hist.empty:
        raise ValueError("empty history")

    completed = []
    today_close = None
    for ts, row in hist.iterrows():
        d = _bar_date(ts)
        if d < today:
            completed.append((d, row))
        elif d == today:
            today_close = float(row["Close"])

    if not completed:
        raise ValueError(f"no completed session before {today.isoformat()}")

    basis_date, basis_row = completed[-1]  # most recent strictly-prior bar
    high = float(basis_row["High"])
    low = float(basis_row["Low"])
    close = float(basis_row["Close"])
    if today_close is not None:
        return {
            "high": high,
            "low": low,
            "close": close,
            "basis_date": basis_date,
        }, float(today_close), True
    return {
        "high": high,
        "low": low,
        "close": close,
        "basis_date": basis_date,
    }, float(close), False


def fetch_pivot_data(ticker):
    """
    Employee Tier - Specialist Tech Assist:
    Computes standard floor pivots from the last COMPLETED trading session
    (bar date strictly before today's ET date). Pivot / R1 / S1 / R2 / S2 are
    cached per ticker per session day — first computation wins; never mutated.

    The returned ``close`` and ``pct_change`` still reflect the live spot so
    downstream scoring can compare current price to the frozen pivot.
    """
    ticker_key = str(ticker).upper().strip()
    today = _session_date_et()
    cache_key = (ticker_key, today.isoformat())

    try:
        stock = yf.Ticker(ticker, session=SESSION)
        # Extra days so weekends/holidays still leave a completed bar
        hist = stock.history(period="10d")

        live_from_today = False
        if hist.empty:
            info = stock.info or {}
            live_close = (
                info.get("regularMarketPrice")
                or info.get("previousClose")
                or 100.0
            )
            prev = info.get("previousClose") or live_close
            # No daily bars: weak fallback for levels only (not ideal, rare)
            high = float(prev) * 1.01
            low = float(prev) * 0.99
            basis_close = float(prev)
            basis_date = None
            basis = {"high": high, "low": low, "close": basis_close, "basis_date": basis_date}
            live_close = float(live_close)
            # info.regularMarketPrice may be live, but without a today bar we
            # cannot trust pct vs basis as a measured day-change for scoring.
            live_from_today = info.get("regularMarketPrice") is not None
        else:
            basis, live_close, live_from_today = _select_completed_session(hist, today)
            high, low, basis_close = basis["high"], basis["low"], basis["close"]
            basis_date = basis["basis_date"]

        computed = _levels_from_ohlc(high, low, basis_close)

        if cache_key in _PIVOT_CACHE:
            cached = _PIVOT_CACHE[cache_key]
            drift = (
                cached["pivot"] != computed["pivot"]
                or cached["r1"] != computed["r1"]
                or cached["s1"] != computed["s1"]
                or cached["r2"] != computed["r2"]
                or cached["s2"] != computed["s2"]
            )
            if drift:
                print(
                    f"CRITICAL: pivot for {ticker_key} attempted to change on "
                    f"{today.isoformat()} "
                    f"(cached P={cached['pivot']} R1={cached['r1']} S1={cached['s1']} "
                    f"→ recomputed P={computed['pivot']} R1={computed['r1']} "
                    f"S1={computed['s1']}); keeping original cached values."
                )
            levels = {
                "pivot": cached["pivot"],
                "r1": cached["r1"],
                "s1": cached["s1"],
                "r2": cached["r2"],
                "s2": cached["s2"],
            }
            basis_close = cached.get("basis_close", basis_close)
        else:
            levels = computed
            _PIVOT_CACHE[cache_key] = {
                **levels,
                "basis_close": round(basis_close, 2),
                "basis_high": round(high, 2),
                "basis_low": round(low, 2),
                "basis_date": basis_date.isoformat() if basis_date else None,
            }
            basis_label = basis_date.isoformat() if basis_date else "unknown"
            print(
                f"pivot basis {ticker_key} {basis_label}: "
                f"H={high:.2f} L={low:.2f} C={basis_close:.2f} -> "
                f"P={levels['pivot']:.2f} R1={levels['r1']:.2f} S1={levels['s1']:.2f}"
            )

        # pct_change is UNKNOWN (None) when we cannot measure day move — never
        # costume a missing today bar as a flat day (pct=0.0).
        if live_from_today and basis_close and basis_close > 0:
            pct_change = round(((live_close - basis_close) / basis_close) * 100.0, 2)
        else:
            pct_change = None
            if not live_from_today:
                print(
                    f"[{ticker_key}] pct_change UNKNOWN: no today bar in history "
                    f"(live_close fell back to basis); not scoring as flat day."
                )

        return {
            "close": round(live_close, 2),
            "pivot": levels["pivot"],
            "r1": levels["r1"],
            "s1": levels["s1"],
            "r2": levels["r2"],
            "s2": levels["s2"],
            "pct_change": pct_change,
            "basis_close": round(float(basis_close), 2) if basis_close else None,
            "live_from_today": bool(live_from_today),
        }
    except Exception as e:
        print(f"❌ [Specialist Desk] Error fetching pivot data for {ticker}: {e}")
        # Default placeholder safe return (not cached — avoid freezing garbage)
        # pct_change=None so scorer flags no_momentum_data, not silent mom=0.
        return {
            "close": 100.0,
            "pivot": 100.0,
            "r1": 101.0,
            "s1": 99.0,
            "r2": 102.0,
            "s2": 98.0,
            "pct_change": None,
            "basis_close": None,
            "live_from_today": False,
        }

def get_specialist_briefing(ticker, pivot_data, news_headlines):
    """
    Individual specialist micro-agent:
    Uses Groq's llama-3.1-8b-instant to extract a concise technical and news briefing
    for the ticker. Falls back to a clean mock summary if the Groq key is missing.
    """
    # 1. Fallback if Groq API key is missing or library not loaded
    if not GROQ_API_KEY or not HAS_GROQ:
        direction = "ABOVE" if pivot_data["close"] >= pivot_data["pivot"] else "BELOW"
        mock_brief = (
            f"Trading {direction} daily pivot (${pivot_data['pivot']}). "
            f"Immediate technical ranges show support at ${pivot_data['s1']} and resistance at ${pivot_data['r1']}. "
            f"Overnight headlines are neutral to slightly bullish for {ticker}."
        )
        return mock_brief

    # 2. Execute call via Groq
    try:
        client = Groq(api_key=GROQ_API_KEY)
        
        prompt = f"""
You are the Specialist Ticker Agent for {ticker}. Analyze the technical levels and headlines:
Ticker: {ticker}
Current Price: ${pivot_data['close']:.2f}
Daily Pivot Point: ${pivot_data['pivot']:.2f}
Support levels (S1, S2): ${pivot_data['s1']:.2f}, ${pivot_data['s2']:.2f}
Resistance levels (R1, R2): ${pivot_data['r1']:.2f}, ${pivot_data['r2']:.2f}

Overnight News Headlines:
{news_headlines}

Your Task:
Output a strict 2-sentence technical briefing. Mention:
1. Whether the price is trading above or below the daily pivot, and the immediate support/resistance bands.
2. The core sentiment trend from the overnight headlines. Do not explain the news details; summarize the sentiment context.
"""
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            model="llama-3.1-8b-instant",
            max_tokens=100,
        )
        return chat_completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ [Specialist Desk] Groq API call error for {ticker}: {e}. Using fallback.")
        direction = "ABOVE" if pivot_data["close"] >= pivot_data["pivot"] else "BELOW"
        return f"Trading {direction} daily pivot (${pivot_data['pivot']}). Ranges: ${pivot_data['s1']} - ${pivot_data['r1']}. Overnight news shows stable sentiment."


def get_aggregated_briefings(tickers):
    """
    Ticker Specialist Desk:
    Executes the micro-agents for the portfolio tickers list.
    Returns:
        dict: mapping of ticker -> specialist_briefing string.
    """
    print("\n--- OPERATING TICKER SPECIALIST DESK ---")
    payload = {}
    tickers_list = list(tickers)
    
    for i, ticker in enumerate(tickers_list):
        print(f"[Specialist Desk] Running micro-agent for {ticker}...")
        
        # 1. Fetch pricing/pivot metrics
        pivot_data = fetch_pivot_data(ticker)
        
        # 2. Fetch database overnight headlines
        news_headlines = get_historical_context(ticker, days=3)
        if not news_headlines.strip():
            news_headlines = "No recent headlines in database memory."
            
        # 3. Formulate specialist briefing
        briefing = get_specialist_briefing(ticker, pivot_data, news_headlines)
        payload[ticker] = briefing

        if i < len(tickers_list) - 1:
            time.sleep(TICKER_PACING_SECONDS)
        
    return payload


# ==========================================
# 🧪 TEST THE DESK
# ==========================================
if __name__ == "__main__":
    print("[Specialist Desk] Running Standalone Tests...")
    test_tickers = ["AAPL", "TSLA"]
    
    briefings = get_aggregated_briefings(test_tickers)
    for ticker, brief in briefings.items():
        print(f"\n[{ticker}] Specialist Briefing:")
        print(brief)
    print("\n[Specialist Desk] Standalone Tests Completed.")
