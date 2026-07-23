"""
master_bot.py — Macro trading loop only (no web server).

This module is pure trading logic: portfolio scans, CEO synthesis, and
`run_macro_loop()`. Flask / keep_alive / PORT binding live exclusively in
`main.py` (the Render orchestrator). Do not reintroduce app.run() here or
in modules imported at the top level of this file.

Key design notes:
  * Single run_portfolio_scan() used by the live 30-min market-hours loop
    and the optional BYPASS_MARKET_HOURS developer one-shot.
  * scoring_engine.py grades pillars from raw numbers + dynamic weights.
  * CEO output uses a strict per-ticker Markdown schema; Gemini failure
    falls back to a deterministic numeric formatter.
  * CircuitBreaker, telemetry, and strike_selector handle resilience,
    backtest logging, and contract selection on EXECUTE.
  * Secrets are env-only (config.assert_secrets crashes early).
"""

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime

import pytz
import requests
import yfinance as yf
from google import genai

import config
import broadcaster
import telemetry
import scoring_engine
import strike_selector
import virtual_broker
import llm_chain
from circuit_breaker import CircuitBreaker
from yf_client import SESSION

# ---------------------------------------------------------------------------
# Network / LLM hard limits
# ---------------------------------------------------------------------------
# Wall-clock budget per external call. Keep well under gunicorn --timeout 120
# so a hung Yahoo/Gemini request cannot pin a gthread until worker kill.
API_CALL_TIMEOUT_S = 20
# google-genai HttpOptions.timeout is in milliseconds.
LLM_HTTP_TIMEOUT_MS = 20_000
# Gemini → DeepSeek chain may use two sequential provider attempts.
LLM_CHAIN_TIMEOUT_S = API_CALL_TIMEOUT_S
LLM_CHAIN_WALL_CLOCK_S = API_CALL_TIMEOUT_S * 2


class MasterBotScanError(Exception):
    """
    Hard failure during a Master Bot scan step (timeout or unrecoverable).

    Attributes:
        step: Pipeline stage name for ops JSON (``Scan failed at: <step>``).
        is_timeout: True when the failure was a wall-clock / network timeout.
        message: Human-readable diagnostic string.
    """

    def __init__(self, message, *, step="unknown", is_timeout=False):
        super().__init__(message)
        self.message = message
        self.step = step or "unknown"
        self.is_timeout = bool(is_timeout)


# Employee tier
from data_engineer import fetch_options_data
from math_agent import calculate_swing_targets

# Memory
from news_memory import get_historical_context, save_headline, clear_expired_news
import sqlite3

# Executive briefing — scheduled 09:15–09:29 EST prep meeting.
from pre_market_meeting import generate_morning_briefing

# Scrapers
from sector_scrapers import (
    scrape_tech_sector, scrape_macro_finance,
    scrape_politics_government, fetch_overnight_futures,
    extract_article_info,
)
from gov_policy_scraper import scrape_gov_policy
from china_macro_scraper import scrape_china_macro
from earnings_calendar_scraper import scrape_earnings_calendar
from innovation_manager import generate_macro_catalyst_vector

# Manager tier
from managers import generate_risk_report, generate_sentiment_report, generate_ticker_manager_report
from ticker_desk import get_aggregated_briefings, fetch_pivot_data
from adversarial_agent import DevilsAdvocate

# Shared durable state with the micro Tracker (atomic load/append/write).
# save_active_trade dual-writes JSON + SQLite active_trades_store (news_room.db).
from tracker_agent import save_active_trade

TICKERS = config.TICKERS
GEMINI_API_KEY = config.GEMINI_API_KEY
DEEPSEEK_API_KEY = getattr(config, "DEEPSEEK_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
BYPASS_MARKET_HOURS = os.environ.get("BYPASS_MARKET_HOURS", "false").lower() == "true"


def _gemini_client():
    """
    Gemini client with a strict per-request HTTP timeout.

    The google-genai SDK accepts timeout via Client ``http_options``
    (milliseconds). Do NOT pass ``request_options=`` to generate_content —
    that kwarg is not part of the current SDK surface and is silently
    ignored or TypeError'd depending on version.

    Prefer ``_llm_generate_text`` for new call sites — it adds DeepSeek failover.
    """
    return genai.Client(
        api_key=GEMINI_API_KEY,
        http_options={"timeout": LLM_HTTP_TIMEOUT_MS},
    )


def _llm_generate_text(prompt, *, step, system=None, timeout_s=LLM_CHAIN_TIMEOUT_S):
    """
    High-availability LLM text: Gemini first, automatic DeepSeek failover.

    Integrates with existing SRE envelopes:
      * Each provider attempt is wall-clock bounded (same ``timeout_s``).
      * Raises ``MasterBotScanError`` so ticker isolation / immortal loop
        paths continue to work unchanged.
      * Does not alter scoring or execution criteria — text generation only.
    """
    try:
        return llm_chain.generate_text(
            prompt,
            step=step,
            system=system,
            timeout_s=timeout_s,
        )
    except llm_chain.LLMChainError as exc:
        raise MasterBotScanError(
            exc.message,
            step=exc.step or step,
            is_timeout=exc.is_timeout,
        ) from exc
    except MasterBotScanError:
        raise
    except Exception as exc:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        is_timeout = (
            "timeout" in name
            or "timed out" in msg
            or "timeout" in msg
            or isinstance(exc, (TimeoutError, requests.Timeout))
        )
        raise MasterBotScanError(
            str(exc) or f"LLM failed at {step}",
            step=step,
            is_timeout=is_timeout,
        ) from exc


def _call_with_timeout(fn, *, timeout_s=API_CALL_TIMEOUT_S, step="api_call"):
    """
    Run ``fn()`` under a hard wall-clock timeout.

    HTTP adapters (yf_client SESSION, Gemini HttpOptions) handle most hangs,
    but yfinance multi-request sequences and SDK edge cases can still stall.
    This wrapper guarantees the trading thread resumes within ``timeout_s``.

    Raises:
        MasterBotScanError: on wall-clock timeout (is_timeout=True) or when
            the underlying call raises a requests/timeout-class error.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError as exc:
            print(
                f"[master_bot] TIMEOUT step={step} after {timeout_s}s — "
                "aborting this call cleanly"
            )
            raise MasterBotScanError(
                f"Timed out after {timeout_s}s",
                step=step,
                is_timeout=True,
            ) from exc
        except MasterBotScanError:
            raise
        except Exception as exc:
            name = type(exc).__name__.lower()
            msg = str(exc).lower()
            is_timeout = (
                "timeout" in name
                or "timed out" in msg
                or "timeout" in msg
                or isinstance(exc, (TimeoutError, requests.Timeout))
            )
            if is_timeout:
                print(f"[master_bot] TIMEOUT step={step}: {exc}")
                raise MasterBotScanError(
                    str(exc) or f"Timeout at {step}",
                    step=step,
                    is_timeout=True,
                ) from exc
            raise

# Shared with main.py / tracker_agent.py — absolute path so cwd cannot desync.
ACTIVE_TRADES_PATH = os.environ.get(
    "ACTIVE_TRADES_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "active_trades.json")),
)

# Full-day NYSE closures (observed dates). Early-close days are NOT included —
# those still run the normal trading window with thinner liquidity.
# Source: NYSE Holidays & Trading Hours calendar (2026–2027).
NYSE_HOLIDAYS = {
    # ---- 2026 ----
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King, Jr. Day
    "2026-02-16",  # Washington's Birthday (Presidents' Day)
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth National Independence Day
    "2026-07-03",  # Independence Day (observed; July 4 is Saturday)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving Day
    "2026-12-25",  # Christmas Day
    # ---- 2027 ----
    "2027-01-01",  # New Year's Day
    "2027-01-18",  # Martin Luther King, Jr. Day
    "2027-02-15",  # Washington's Birthday (Presidents' Day)
    "2027-03-26",  # Good Friday
    "2027-05-31",  # Memorial Day
    "2027-06-18",  # Juneteenth (observed; June 19 is Saturday)
    "2027-07-05",  # Independence Day (observed; July 4 is Sunday)
    "2027-09-06",  # Labor Day
    "2027-11-25",  # Thanksgiving Day
    "2027-12-24",  # Christmas Day (observed; Dec 25 is Saturday)
}

broadcaster.WEBHOOK_URL = config.DISCORD_WEBHOOK or broadcaster.WEBHOOK_URL


# ==========================================
# 📁 ACTIVE TRADE PERSISTENCE (Tracker handoff)
# ==========================================

def record_executed_trade(ticker, contract, scan_id=None, card=None):
    """
    Append a confirmed EXECUTE position to active_trades.json via the
    Tracker agent's atomic save helper (temp file + os.replace).

    Also mirrors the position into SQLite `active_trades_store` inside
    news_room.db (via save_active_trade) so open trades survive restarts
    when the JSON file is empty.

    Does not alter scoring or execution criteria — persistence only.
    Returns True on successful write.
    """
    if not contract or "error" in contract:
        return False

    entry_time = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    entry_price = contract.get("entry_premium")
    target_price = contract.get("take_profit")
    stop_loss = contract.get("stop_loss")

    option_contract = {
        "direction": contract.get("direction"),
        "strike": contract.get("strike"),
        "expiration": contract.get("expiration"),
        "days_to_expiration": contract.get("days_to_expiration"),
        "implied_volatility": contract.get("implied_volatility"),
        "bid_ask_spread_pct": contract.get("bid_ask_spread_pct"),
    }

    notes_parts = [f"scan_id={scan_id}"] if scan_id else []
    if card is not None:
        notes_parts.append(f"score={getattr(card, 'total_score', None)}")
        notes_parts.append(f"flag={getattr(card, 'action_flag', None)}")
    if contract.get("rationale"):
        notes_parts.append(str(contract["rationale"])[:240])

    trade_payload = {
        # Tracker-compatible core fields
        "trade_id": str(uuid.uuid4()),
        "ticker": ticker,
        "direction": contract.get("direction"),
        "entry_price": entry_price,
        "entry_premium": entry_price,
        "entry_timestamp": entry_time,
        "entry_time": entry_time,
        "strike": contract.get("strike"),
        "expiration": contract.get("expiration"),
        "stop_loss": stop_loss,
        "take_profit": target_price,
        "target_price": target_price,
        "trailing_stop": None,
        "option_contract": option_contract,
        "notes": "; ".join(notes_parts),
    }

    # Dual-write: active_trades.json + news_room.db active_trades_store
    ok = save_active_trade(trade_payload, path=ACTIVE_TRADES_PATH)
    if ok:
        print(
            "[CEO] Successfully recorded new position to active_trades.json "
            "and SQLite active_trades_store"
        )
    else:
        print(f"[CEO] WARNING: failed to record {ticker} to {ACTIVE_TRADES_PATH}")
    return ok


# ==========================================
# 📈 DATA HELPERS
# ==========================================

def get_latest_futures_pct(symbol="ES=F"):
    """Latest overnight futures % change captured by the futures scraper
    (stored in headlines.sentiment_score). None if nothing recent."""
    try:
        with sqlite3.connect(config.NEWS_DB_PATH, timeout=30.0) as conn:
            row = conn.execute(
                """SELECT sentiment_score FROM headlines
                   WHERE ticker = ? AND sentiment_score IS NOT NULL
                     AND timestamp >= datetime('now', '-18 hours')
                   ORDER BY timestamp DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def fetch_atr(ticker, breaker=None):
    """ATR(14) via 1-month daily history. Returns (atr_abs, atr_pct).

    yfinance is wall-clock bounded so a hung Yahoo socket cannot stall the
    portfolio scan indefinitely.
    """
    try:
        def _fetch():
            hist = yf.Ticker(ticker, session=SESSION).history(period="1mo")
            return strike_selector.compute_atr(hist)

        atr_abs, atr_pct = _call_with_timeout(
            _fetch, timeout_s=API_CALL_TIMEOUT_S, step=f"yfinance_atr:{ticker}"
        )
        if breaker and atr_abs is not None:
            breaker.record_success(f"atr:{ticker}")
        return atr_abs, atr_pct
    except MasterBotScanError as e:
        print(f"[{ticker}] ATR fetch failed at {e.step}: {e.message}")
        if breaker:
            breaker.record_failure(f"atr:{ticker}")
        return None, None
    except Exception as e:
        print(f"[{ticker}] ATR fetch failed: {e}")
        if breaker:
            breaker.record_failure(f"atr:{ticker}")
        return None, None


def ensure_news_context(ticker, breaker=None):
    """DB-first news retrieval with the live yfinance cold-start fallback."""
    news_string = get_historical_context(ticker, days=90)
    if news_string.strip():
        return news_string
    print(f"[{ticker}] 👷 No news in database. Running live yfinance fallback...")
    try:
        def _fetch_news():
            return yf.Ticker(ticker, session=SESSION).news

        articles = _call_with_timeout(
            _fetch_news,
            timeout_s=API_CALL_TIMEOUT_S,
            step=f"yfinance_news:{ticker}",
        )
        for article in articles or []:
            title, publisher = extract_article_info(article)
            if title and title != "Unknown Title":
                save_headline(ticker, "Fallback", publisher, title)
        news_string = get_historical_context(ticker, days=90)
        if breaker:
            breaker.record_success(f"news:{ticker}")
    except MasterBotScanError as e:
        print(f"❌ [{ticker}] yfinance news fallback {e.step}: {e.message}")
        if breaker:
            breaker.record_failure(f"news:{ticker}")
    except Exception as e:
        print(f"❌ [{ticker}] yfinance news fallback error: {e}")
        if breaker:
            breaker.record_failure(f"news:{ticker}")
    return news_string if news_string.strip() else "No recent headlines or historical news found."


# ==========================================
# 💼 QUANT MANAGER (context/color for the CoS — not used for scoring)
# ==========================================

def run_quant_manager(options_json, ticker_symbol):
    print(f"[{ticker_symbol}] 💼 Quant Manager (AI): Analyzing options chain for setups...")
    prompt = f"""
You are a Quantitative Analyst. Analyze this options data for {ticker_symbol}.
Look at the Strike prices, Implied Volatility, Volume, and Open Interest.
Identify ONE high-probability options trade (either a Call or a Put) with unusual volume or a compelling setup.
Provide the strike, expiration, and a 1-sentence quantitative rationale.

Data:
{options_json}
"""
    try:
        # Gemini → DeepSeek chain; each leg wall-clock bounded.
        return _llm_generate_text(
            prompt,
            step=f"llm_quant:{ticker_symbol}",
            timeout_s=LLM_CHAIN_TIMEOUT_S,
        )
    except Exception as e:
        # Both providers failed — soft fallback; strike_selector remains authoritative.
        print(
            f"[{ticker_symbol}] 💼 Quant Manager: LLM chain failed ({e}); "
            "using fallback note."
        )
        return (f"Quant context unavailable (LLM offline). Deterministic strike selection "
                f"in strike_selector.py remains authoritative for {ticker_symbol}.")


# ==========================================
# 👔 CHIEF OF STAFF SYNTHESIS
# ==========================================

def run_cos_synthesis(ticker_symbol, ticker_manager_report, news_report, options_report, quant_report):
    print(f"[{ticker_symbol}] 👔 Chief of Staff (AI): Synthesizing Corporate Brief...")
    prompt = f"""
You are the Chief of Staff (CoS) of a quantitative hedge fund. Compile a synthesized Corporate Brief for ticker {ticker_symbol}.

--- 1. TICKER TEAM MANAGER REPORT ---
{ticker_manager_report}

--- 2. SENTIMENT/NEWS MANAGER REPORT ---
{news_report}

--- 3. RISK & LIQUIDITY REPORT ---
{options_report}

--- 4. QUANT MANAGER REPORT ---
{quant_report}

CRITICAL FIDELITY RULES:
- PRESERVE every specific number present in the manager reports (spreads, strikes, premiums, support/resistance, IV). Do NOT round them away or replace them with adjectives.
- Never write generic filler like "conditions look favorable" — every claim must carry its metric.

Structure the brief as:
- **Technical & Market Alignment**: exact levels and how they align with sentiment.
- **Execution Feasibility**: exact option pricing, spreads, risk rating, and the specific strategy (strike, expiration, premium, 20% stop, 50% target) if one exists.
- **CoS Direct Recommendation**: your advice with the deciding metrics named.
"""
    try:
        return _llm_generate_text(
            prompt,
            step=f"llm_cos:{ticker_symbol}",
            timeout_s=LLM_CHAIN_TIMEOUT_S,
        )
    except Exception as e:
        print(
            f"[{ticker_symbol}] 👔 CoS: LLM chain failed ({e}); "
            "using structural fallback."
        )
        return (f"=== CORPORATE BRIEF {ticker_symbol} (LOCAL FALLBACK) ===\n{options_report}\n"
                f"{news_report}\nQuant context: {quant_report}")


# ==========================================
# 👑 CEO AGENT — strict schema + deterministic numeric fallback (Task 1)
# ==========================================

def format_ceo_deterministic(card, contract=None):
    """Fills the exact required schema from real numbers. Used when Gemini
    is unavailable AND appended as ground truth beneath the LLM output."""
    lm = card.metrics.get("liquidity", {})
    tm = card.metrics.get("technical", {})
    sm = card.metrics.get("sentiment", {})
    strat = ""
    if contract and "error" not in contract:
        strat = (f" Selected contract: {contract['direction']} {contract['strike']} exp "
                 f"{contract['expiration']} @ ${contract['entry_premium']} "
                 f"(SL ${contract['stop_loss']} / TP ${contract['take_profit']}, "
                 f"IV {contract['implied_volatility']}, spread {contract['bid_ask_spread_pct']}%).")
    elif contract and "error" in contract:
        strat = f" Strike selection aborted: {contract['error']}"
    return (
        f"### {card.ticker} - {card.action_flag}\n"
        f"* **Market Context & Gap**: Spot {tm.get('close')} vs pivot {tm.get('pivot')} "
        f"(R1 {tm.get('r1')} / S1 {tm.get('s1')}); day change {tm.get('pct_change')}%; "
        f"ATR% {tm.get('atr_pct')}; overnight futures {sm.get('futures_pct')}%.\n"
        f"* **Quantitative Liquidity Metric**: Median ATM spread {lm.get('median_atm_spread_pct')}%, "
        f"ATM volume {lm.get('total_atm_volume')}, open interest {lm.get('total_atm_open_interest')} "
        f"across {lm.get('atm_contracts')} contracts.\n"
        f"* **Sentiment Alignment**: {sm.get('headline_count')} headlines scanned — "
        f"{sm.get('bullish_hits')} bullish vs {sm.get('bearish_hits')} bearish signals; "
        f"macro read: {sm.get('macro_note')}.\n"
        f"* **Strategic Executive Decision**: Weighted engine scored Liquidity "
        f"{card.liquidity_score}/{card.weights.get('liquidity')}, Technical "
        f"{card.technical_score}/{card.weights.get('technical')}, Sentiment "
        f"{card.sentiment_score}/{card.weights.get('sentiment')} "
        f"(adversarial penalty -{card.adversarial_penalty:g}) for a total of "
        f"{card.total_score}/100, mandating {card.action_flag}.{strat}"
    )


def run_ceo_decision(corporate_brief, morning_macro_context, card, contract=None):
    ticker = card.ticker
    print(f"[{ticker}] 👑 CEO Agent (AI): Formulating final decision...")
    snapshot = scoring_engine.metrics_snapshot_text(card)
    contract_block = json.dumps(contract, indent=2) if contract else "No contract selected (PASS)."
    prompt = f"""
You are the CEO of a quantitative hedge fund making the final call on {ticker}.
The weighted scoring engine result is AUTHORITATIVE: {card.total_score}/100 -> {card.action_flag}. You may not change the flag.

{snapshot}

--- SELECTED CONTRACT (deterministic strike selector) ---
{contract_block}

--- MORNING PRE-MARKET BRIEFING ---
{morning_macro_context}

--- CHIEF OF STAFF CORPORATE BRIEF ---
{corporate_brief}

OUTPUT RULES (strict):
1. Output EXACTLY this Markdown structure, nothing before or after it:
### {ticker} - {card.action_flag}
* **Market Context & Gap**: ...
* **Quantitative Liquidity Metric**: ...
* **Sentiment Alignment**: ...
* **Strategic Executive Decision**: ...
2. Every bullet MUST quote the exact numbers from the RAW METRICS SNAPSHOT above (spreads, volumes, pivots, headline counts). Generic phrases without numbers are a formatting failure.
3. The Strategic Executive Decision bullet must be 2-3 sentences, cite the {card.total_score}/100 total with its pillar breakdown, and — if a contract was selected — name its strike, expiration, entry premium, stop-loss, and take-profit.
4. Do not truncate, summarize away, or omit any of the four bullets.
"""
    try:
        text = _llm_generate_text(
            prompt,
            step=f"llm_ceo:{ticker}",
            timeout_s=LLM_CHAIN_TIMEOUT_S,
        )
        # Schema guard: if the model drifted, fall back to deterministic format
        required = ["### ", "* **Market Context & Gap**", "* **Quantitative Liquidity Metric**",
                    "* **Sentiment Alignment**", "* **Strategic Executive Decision**"]
        if not all(tag in text for tag in required):
            print(f"[{ticker}] 👑 CEO output failed schema check — using deterministic formatter.")
            return format_ceo_deterministic(card, contract)
        return text
    except Exception as e:
        # Both Gemini and DeepSeek failed — never break the scan; deterministic CEO text.
        print(
            f"[{ticker}] 👑 CEO Agent: LLM chain failed ({e}); "
            "using deterministic formatter."
        )
        return format_ceo_deterministic(card, contract)


# ==========================================
# 🔁 SHARED PORTFOLIO SCAN  (was duplicated twice before)
# ==========================================

def run_night_harvest():
    print("[System State] Triggering background scrapers...")
    try:
        scrape_tech_sector()
        scrape_macro_finance()
        scrape_politics_government()
        fetch_overnight_futures()
        scrape_gov_policy(TICKERS)
        scrape_china_macro(TICKERS)
        scrape_earnings_calendar(TICKERS)
        clear_expired_news()
    except Exception as err:
        print(f"❌ Scraper error in night harvest: {err}")


def is_us_equity_market_open(now=None):
    """
    True during regular NYSE cash session (Mon–Fri 09:30–16:00 ET),
    excluding configured full-day holidays. Early-close days still count open.
    """
    try:
        est_tz = pytz.timezone("America/New_York")
    except Exception:
        est_tz = None
    now = now or datetime.now(est_tz)
    if now.tzinfo is None and est_tz is not None:
        now = est_tz.localize(now)
    if now.weekday() > 4:
        return False
    if now.strftime("%Y-%m-%d") in NYSE_HOLIDAYS:
        return False
    t = now.time()
    trading_start = datetime.strptime("09:30:00", "%H:%M:%S").time()
    trading_end = datetime.strptime("15:59:59", "%H:%M:%S").time()
    return trading_start <= t <= trading_end


def run_portfolio_scan(
    morning_macro_context,
    breaker,
    inter_ticker_sleep=10,
    tickers=None,
):
    """
    One full pass across a ticker universe.

    Used by the live 30-min market-hours loop (and optional developer bypass).

    Pipeline per ticker:
      price/options retrieval -> technical/momentum scoring ->
      Adversarial Agent risk/veto -> strike select -> CEO decision ->
      Discord (+ virtual broker on EXECUTE).

    Args:
        morning_macro_context: Pre-market briefing text for the CEO.
        breaker: CircuitBreaker instance.
        inter_ticker_sleep: Seconds between tickers (rate-limit cushion).
        tickers: Universe override; defaults to config TICKERS.

    Returns:
        dict with scan_id, tickers_scanned, results, vetoes, trades,
        discord_delivered, circuit_breaker_open.
    """
    universe = list(tickers) if tickers is not None else list(TICKERS)
    result = {
        "scan_id": None,
        "tickers_scanned": universe,
        "results": [],
        "vetoes": [],
        "trades": [],
        "discord_delivered": None,  # None = no Discord attempts this scan
        "discord_attempts": 0,
        "discord_successes": 0,
        "circuit_breaker_open": False,
        "aborted": False,
        "failed_at": None,
        "error": None,
        "is_timeout": False,
    }

    if breaker.is_open():
        print("🛑 [System] Circuit breaker OPEN — portfolio scan suspended this cycle.")
        result["circuit_breaker_open"] = True
        result["aborted"] = True
        return result

    scan_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    result["scan_id"] = scan_id
    weights = config.load_weights()
    futures_pct = get_latest_futures_pct("ES=F")
    print(f"\n🚀 PORTFOLIO SCAN {scan_id} | tickers={universe} | "
          f"weights={weights} | ES=F overnight {futures_pct}%")

    # Portfolio-level technical color (LLM, for the CoS brief only).
    # Wall-clock bound: a hung yfinance desk + Gemini manager must not pin
    # the trading thread past API_CALL_TIMEOUT_S * stages.
    try:
        print("\n[System State] 👷 Running Ticker Specialist Desk...")
        specialist_briefings = _call_with_timeout(
            lambda: get_aggregated_briefings(universe),
            # Desk fans out one yfinance pull per ticker; budget scales lightly.
            timeout_s=max(API_CALL_TIMEOUT_S, API_CALL_TIMEOUT_S * max(1, len(universe))),
            step="specialist_desk_yfinance",
        )
        # Wall clock = 2x single-provider budget so Gemini→DeepSeek failover
        # can complete inside generate_ticker_manager_report without outer kill.
        ticker_manager_report = _call_with_timeout(
            lambda: generate_ticker_manager_report(
                specialist_briefings, api_key=GEMINI_API_KEY
            ),
            timeout_s=LLM_CHAIN_WALL_CLOCK_S,
            step="llm_ticker_manager",
        )
    except MasterBotScanError as desk_err:
        print(
            f"❌ Specialist Desk/Manager hard failure at {desk_err.step}: "
            f"{desk_err.message}"
        )
        result["failed_at"] = desk_err.step
        result["error"] = desk_err.message
        result["is_timeout"] = desk_err.is_timeout
        # Soft-degrade the brief — NEVER abort the whole scan for desk failure.
        # Per-ticker yfinance/LLM isolation still processes the universe.
        ticker_manager_report = (
            f"Portfolio technical report unavailable "
            f"(failed at {desk_err.step}: {desk_err.message})."
        )
    except Exception as desk_err:
        print(f"❌ Specialist Desk/Manager error: {desk_err}")
        ticker_manager_report = "Portfolio technical report unavailable this cycle."

    def _send_discord(message):
        """Send Discord alert and track delivery success for this scan.

        Delivery is hardened in broadcaster.send_discord_alert (timeout +
        retries). Never raises — a webhook outage must not kill the scan.
        """
        text = str(message) if message is not None else ""
        result["discord_attempts"] += 1
        try:
            ok = broadcaster.send_discord_alert(text)
        except Exception as discord_err:
            # Absolute last line of defense if broadcaster itself throws.
            print(
                f"[master_bot] ERROR: Discord send raised unexpectedly: "
                f"{discord_err}"
            )
            ok = False
        if ok:
            result["discord_successes"] += 1
        return ok

    def _isolate_ticker(ticker, ticker_summary, err, *, sleep_after=True):
        """
        Record a single-ticker failure and keep scanning the rest of the book.

        Mission rule: one bad yfinance/LLM call must never abort the portfolio.
        """
        step = getattr(err, "step", None) or type(err).__name__
        msg = getattr(err, "message", None) or str(err)
        print(
            f"❌ [{ticker}] Isolated failure at {step}: {msg} "
            f"— continuing to next ticker"
        )
        ticker_summary["error"] = f"{step}: {msg}"
        result["results"].append(ticker_summary)
        # Surface last isolation for ops without marking the whole scan aborted.
        result["failed_at"] = step
        result["error"] = msg
        if getattr(err, "is_timeout", False):
            result["is_timeout"] = True
        if sleep_after and idx < len(universe) - 1:
            time.sleep(inter_ticker_sleep)

    for idx, ticker in enumerate(universe):
        print(f"\n------------------------------------------")
        print(f"🔄 PROCESSING TICKER: {ticker} ({idx + 1}/{len(universe)})")
        print(f"------------------------------------------")
        ticker_summary = {
            "ticker": ticker,
            "action_flag": None,
            "total_score": None,
            "vetoed": False,
            "veto_reason": None,
            "trade_executed": False,
            "error": None,
        }
        try:
            # ---- Employee tier: raw data (circuit-breaker instrumented) ----
            print(f"[{ticker}] 👷 Data Engineer: Fetching options chain...")
            options_json = _call_with_timeout(
                lambda t=ticker: fetch_options_data(t),
                timeout_s=API_CALL_TIMEOUT_S,
                step=f"yfinance_options:{ticker}",
            )
            options_dict = json.loads(options_json)
            if "error" in options_dict:
                print(f"❌ Skipping {ticker}: {options_dict['error']}")
                ticker_summary["error"] = options_dict["error"]
                result["results"].append(ticker_summary)
                breaker.record_failure(f"options_chain:{ticker}")
                if breaker.is_open():
                    print("🛑 Circuit breaker tripped mid-scan — aborting remaining tickers.")
                    result["circuit_breaker_open"] = True
                    result["aborted"] = True
                    break
                continue
            breaker.record_success(f"options_chain:{ticker}")

            pivot_data = _call_with_timeout(
                lambda t=ticker: fetch_pivot_data(t),
                timeout_s=API_CALL_TIMEOUT_S,
                step=f"yfinance_pivot:{ticker}",
            )
            atr_abs, atr_pct = fetch_atr(ticker, breaker)
            news_string = ensure_news_context(ticker, breaker)
            math_json = calculate_swing_targets(options_json)

            # ---- Manager tier (LLM context for the brief) ----
            # Each call is wall-clock bounded (2x single-provider budget so
            # Gemini→DeepSeek failover inside managers can finish). Soft LLM
            # failures fall back inside manager modules; hard timeouts isolate
            # THIS ticker only — never the immortal macro loop.
            try:
                risk_report = _call_with_timeout(
                    lambda: generate_risk_report(
                        math_json, ticker, api_key=GEMINI_API_KEY
                    ),
                    timeout_s=LLM_CHAIN_WALL_CLOCK_S,
                    step=f"llm_risk:{ticker}",
                )
            except MasterBotScanError as llm_err:
                if llm_err.is_timeout:
                    _isolate_ticker(ticker, ticker_summary, llm_err)
                    continue
                risk_report = f"Risk report unavailable ({llm_err.message})."

            try:
                sentiment_report = _call_with_timeout(
                    lambda: generate_sentiment_report(
                        news_string, ticker, api_key=GEMINI_API_KEY
                    ),
                    timeout_s=LLM_CHAIN_WALL_CLOCK_S,
                    step=f"llm_sentiment:{ticker}",
                )
            except MasterBotScanError as llm_err:
                if llm_err.is_timeout:
                    _isolate_ticker(ticker, ticker_summary, llm_err)
                    continue
                sentiment_report = f"Sentiment report unavailable ({llm_err.message})."

            quant_report = run_quant_manager(math_json, ticker)

            try:
                macro_vector = _call_with_timeout(
                    lambda: generate_macro_catalyst_vector(
                        ticker, api_key=GEMINI_API_KEY
                    ),
                    timeout_s=LLM_CHAIN_WALL_CLOCK_S,
                    step=f"llm_macro:{ticker}",
                )
            except MasterBotScanError as llm_err:
                if llm_err.is_timeout:
                    _isolate_ticker(ticker, ticker_summary, llm_err)
                    continue
                macro_vector = "Neutral macroeconomic backdrop (LLM offline)."

            # ---- Deterministic weighted scoring (Task 2) ----
            card = scoring_engine.score_ticker(
                ticker, options_dict, pivot_data, news_string,
                macro_vector=macro_vector, futures_pct=futures_pct,
                atr_pct=atr_pct, weights=weights,
            )
            print(f"[{ticker}] ⚙️ Scoring Engine: L {card.liquidity_score} + "
                  f"T {card.technical_score} + S {card.sentiment_score} = "
                  f"{card.total_score}/100 -> {card.action_flag}")

            # ---- Devil's Advocate intercept ----
            adv_result = None
            vetoed = False
            veto_reason = None
            if card.action_flag == "EXECUTE":
                advocate = DevilsAdvocate(api_key=GEMINI_API_KEY)
                try:
                    adv_result = _call_with_timeout(
                        lambda: advocate.evaluate_trade({
                            "ticker": ticker,
                            "liquidity_score": card.liquidity_score,
                            "tech_score": card.technical_score,
                            "sentiment_score": card.sentiment_score,
                            "raw_metrics": card.metrics,
                        }),
                        timeout_s=LLM_CHAIN_WALL_CLOCK_S,
                        step=f"llm_adversarial:{ticker}",
                    )
                except MasterBotScanError as llm_err:
                    # Never kill the rest of the book for one adversarial timeout.
                    print(
                        f"[{ticker}] 👹 Devil's Advocate timeout/fail "
                        f"({llm_err.message}); using non-veto fallback."
                    )
                    adv_result = {
                        "veto_triggered": False,
                        "risk_confidence": 0.20,
                        "reason": f"Fallback after {llm_err.step}: {llm_err.message}",
                    }
                if adv_result.get("veto_triggered") and float(adv_result.get("risk_confidence", 0)) > 0.75:
                    veto_reason = adv_result.get("reason", "") or "Adversarial veto"
                    scoring_engine.apply_adversarial_penalty(
                        card, 15.0, veto_reason)
                    vetoed = True
                    print(f"[{ticker}] 🛑 Devil's Advocate veto -15 -> "
                          f"{card.total_score}/100 ({card.action_flag}).")
                else:
                    print(f"[{ticker}] ✅ Devil's Advocate cleared the trade.")

            # ---- Strike selection on EXECUTE (Task 3a) ----
            contract = None
            if card.action_flag == "EXECUTE":
                contract = strike_selector.select_optimal_contract(
                    options_dict, pivot_data, atr_abs=atr_abs)
                if "error" in contract:
                    print(f"[{ticker}] ⚠️ Strike selector found no tradeable contract: "
                          f"{contract['error']} — downgrading to PASS.")
                    card.action_flag = "PASS"
                    card.reasons.append(f"Downgraded: {contract['error']}")
                else:
                    print(f"[{ticker}] 🎯 Contract: {contract['direction']} "
                          f"{contract['strike']} {contract['expiration']} @ "
                          f"${contract['entry_premium']}")

            # ---- Executive tier ----
            corporate_brief = run_cos_synthesis(
                ticker, ticker_manager_report, sentiment_report, risk_report, quant_report)
            trade_decision = run_ceo_decision(
                corporate_brief, morning_macro_context, card, contract)
            print(f"\n--- CEO DECISION OUTPUT ---\n{trade_decision}\n---------------------------")

            # ---- Telemetry (Task 3c) + broadcast ----
            telemetry.log_scan_result(
                scan_id, card,
                adversarial_result=adv_result,
                selected_contract=contract,
                agent_params={"weights": weights, "futures_pct": futures_pct,
                              "macro_vector": macro_vector[:300] if macro_vector else ""},
            )

            ticker_summary["action_flag"] = card.action_flag
            ticker_summary["total_score"] = getattr(card, "total_score", None)
            ticker_summary["vetoed"] = vetoed
            ticker_summary["veto_reason"] = veto_reason

            # Live path: always broadcast CEO decision; record vetoes.
            _send_discord(trade_decision)
            if vetoed:
                result["vetoes"].append({
                    "ticker": ticker,
                    "reason": veto_reason,
                    "total_score": card.total_score,
                    "action_flag": card.action_flag,
                })

            # ---- Persist confirmed EXECUTE for Tracker micro-loop ----
            # Scoring / strike selection already decided action_flag; this only
            # hands the open position to active_trades.json (atomic append).
            if card.action_flag == "EXECUTE" and contract and "error" not in contract:
                try:
                    # Virtual paper ledger: debit premium * 100 from buying power.
                    # Does not alter scoring / AI / execution criteria.
                    entry_px = contract.get("entry_premium")
                    buy_payload = dict(contract)
                    buy_payload.setdefault("ticker", ticker)
                    try:
                        virtual_broker.paper_buy(buy_payload, entry_px)
                    except Exception as broker_err:
                        print(f"[CEO] WARNING: virtual paper_buy failed "
                              f"for {ticker}: {broker_err}")
                    record_executed_trade(
                        ticker, contract, scan_id=scan_id, card=card)
                    ticker_summary["trade_executed"] = True
                    result["trades"].append({
                        "ticker": ticker,
                        "direction": contract.get("direction"),
                        "strike": contract.get("strike"),
                        "expiration": contract.get("expiration"),
                        "entry_premium": contract.get("entry_premium"),
                        "total_score": card.total_score,
                        "action_flag": card.action_flag,
                    })
                except Exception as persist_err:
                    # Never let state I/O kill the portfolio scan
                    print(f"[CEO] WARNING: active_trades persistence failed "
                          f"for {ticker}: {persist_err}")
                    ticker_summary["error"] = f"persist: {persist_err}"

            result["results"].append(ticker_summary)

            if idx < len(universe) - 1:
                time.sleep(inter_ticker_sleep)
        except MasterBotScanError as step_err:
            # Hard timeout / network failure on yfinance or LLM for this ticker.
            # Isolate: log, continue — do NOT abort the remaining universe.
            _isolate_ticker(ticker, ticker_summary, step_err)
            continue
        except Exception as ticker_err:
            # Catch-all isolation: bad data shape, unexpected library errors, etc.
            print(f"❌ Error processing ticker {ticker}: {ticker_err}")
            ticker_summary["error"] = str(ticker_err)
            result["results"].append(ticker_summary)
            if idx < len(universe) - 1:
                time.sleep(inter_ticker_sleep)
            continue

    if result["discord_attempts"] > 0:
        result["discord_delivered"] = (
            result["discord_successes"] == result["discord_attempts"]
            and result["discord_successes"] > 0
        )
    else:
        # Empty universe or no alerts — not a delivery failure.
        result["discord_delivered"] = True

    if result.get("aborted") and result.get("failed_at"):
        print(
            f"\n⚠️ PORTFOLIO SCAN {scan_id} ABORTED at {result['failed_at']}: "
            f"{result.get('error')}"
        )
    else:
        print(f"\n✅ PORTFOLIO SCAN {scan_id} COMPLETED.")
    return result


# ==========================================
# 🚀 MISSION CONTROL (24-HOUR TIME ENGINE)
# ==========================================

def run_macro_loop():
    """
    24-hour mission-control loop (night harvest / pre-market / 30-min scans).

    IMMORTAL daemon contract:
      * The outer ``while True`` must never exit on an unhandled exception.
      * Catastrophic cycle failures page Discord with ``[CRITICAL BOT ERROR]``,
        sleep 60s (anti-spam), then ``continue``.
      * Flask / secondary trackers may die; this loop must not die silently.
    """
    print("\n--- INITIATING HIERARCHICAL MULTI-AGENT TRADING BOT (v2) ---")
    print(f"Loaded Tickers: {TICKERS}")
    config.assert_secrets(require_discord=False)   # Gemini mandatory; webhook warns via broadcaster
    telemetry.init_telemetry_table()
    breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=900)

    try:
        est_tz = pytz.timezone("America/New_York")
    except Exception as e:
        print(f"[System] Error setting EST timezone ({e}). Using local timezone.")
        est_tz = None

    morning_macro_context = ""
    last_briefing_date = None
    scan_interval = 1800  # 30-minute active loop
    # After an unhandled cycle exception: wait before re-entering the loop.
    # 60s is intentional — prevents Discord spam if a failure is sticky.
    CRITICAL_ERROR_BACKOFF_S = 60

    while True:
        try:
            now = datetime.now(est_tz)
            current_time = now.time()

            # Developer test sequence: night harvest -> briefing -> one scan -> exit
            if BYPASS_MARKET_HOURS:
                print("\n🔬 DEVELOPER BYPASS RUN: Simulating 24-Hour Cycle")
                print("\n[System State] 🌙 [Bypass Sim] NIGHT MODE...")
                run_night_harvest()
                time.sleep(2)
                print("\n[System State] 📊 [Bypass Sim] PREP MEETING...")
                try:
                    morning_macro_context = generate_morning_briefing()
                except Exception as err:
                    print(f"❌ [Bypass Sim] Briefing error: {err}")
                time.sleep(2)
                print("\n[System State] 📈 [Bypass Sim] ACTIVE TRADING MODE...")
                run_portfolio_scan(morning_macro_context, breaker, inter_ticker_sleep=5)
                print("\n🔬 DEVELOPER BYPASS RUN: Completed. Exiting loop.")
                break

            night_start_1 = datetime.strptime("16:00:00", "%H:%M:%S").time()
            night_end_1 = datetime.strptime("23:59:59", "%H:%M:%S").time()
            night_start_2 = datetime.strptime("00:00:00", "%H:%M:%S").time()
            night_end_2 = datetime.strptime("09:14:59", "%H:%M:%S").time()
            meeting_start = datetime.strptime("09:15:00", "%H:%M:%S").time()
            meeting_end = datetime.strptime("09:29:59", "%H:%M:%S").time()
            trading_start = datetime.strptime("09:30:00", "%H:%M:%S").time()
            trading_end = datetime.strptime("15:59:59", "%H:%M:%S").time()
            is_weekend = now.weekday() > 4
            is_holiday = now.strftime("%Y-%m-%d") in NYSE_HOLIDAYS
            # Holidays are treated exactly like weekends: no prep, no trading.
            is_market_closed = is_weekend or is_holiday
            if is_holiday:
                print("[System] Market closed today for NYSE holiday.")

            is_night_mode = (is_market_closed
                             or (night_start_1 <= current_time <= night_end_1)
                             or (night_start_2 <= current_time <= night_end_2))
            is_prep_meeting = (not is_market_closed and meeting_start <= current_time <= meeting_end)
            is_trading_mode = (not is_market_closed and trading_start <= current_time <= trading_end)

            if is_night_mode:
                print(f"\n[System State] 🌙 NIGHT MODE (EST {now.strftime('%Y-%m-%d %H:%M:%S')})")
                run_night_harvest()
                print("[System State] Overnight harvest complete. Sleeping until next state check...")
                for _ in range(45):
                    time.sleep(60)
                    nxt = datetime.now(est_tz)
                    # Only break toward prep on a true trading day (weekday + not holiday)
                    if (nxt.time() >= meeting_start
                            and nxt.weekday() <= 4
                            and nxt.strftime("%Y-%m-%d") not in NYSE_HOLIDAYS):
                        break
                continue

            elif is_prep_meeting:
                print(f"\n[System State] 📊 PRE-MARKET PREP MEETING (EST {now.strftime('%H:%M:%S')})")
                if last_briefing_date != now.date():
                    try:
                        morning_macro_context = generate_morning_briefing()
                        last_briefing_date = now.date()
                        print("[System State] Pre-market briefing generated and cached.")
                    except Exception as brief_err:
                        print(f"❌ Error generating pre-market briefing: {brief_err}")
                else:
                    print("[System State] Briefing already broadcast today; waiting for the open...")
                time.sleep(60)
                continue

            elif is_trading_mode:
                print(f"\n[System State] 📈 ACTIVE TRADING MODE (EST {now.strftime('%H:%M:%S')})")
                if not morning_macro_context:
                    print("[System State] Missing pre-market briefing context. Generating now...")
                    try:
                        morning_macro_context = generate_morning_briefing()
                        last_briefing_date = now.date()
                    except Exception as brief_err:
                        print(f"❌ Fallback briefing error: {brief_err}")
                        morning_macro_context = "No morning briefing context available."

                run_portfolio_scan(morning_macro_context, breaker)

                print(f"Sleeping up to {scan_interval // 60} minutes before next scan...")
                for _ in range(scan_interval // 60):
                    time.sleep(60)
                    if datetime.now(est_tz).time() >= trading_end:
                        break
                continue

            else:
                time.sleep(5)

        except Exception as cycle_err:
            # ============================================================
            # IMMORTAL GUARD: never let an unhandled exception kill the
            # macro loop. Page Discord, back off 60s, then continue.
            # ============================================================
            err_type = type(cycle_err).__name__
            err_msg = str(cycle_err) or repr(cycle_err)
            print(
                f"❌ [CRITICAL BOT ERROR] Macro loop caught {err_type}: {err_msg} "
                f"— will retry in {CRITICAL_ERROR_BACKOFF_S}s (loop stays alive)"
            )
            try:
                import traceback
                traceback.print_exc()
            except Exception:
                pass

            critical_alert = (
                f"🚨 **[CRITICAL BOT ERROR]** 🚨\n"
                f"Master Bot macro loop hit an unhandled exception and is "
                f"self-recovering (not exiting).\n"
                f"**Type:** `{err_type}`\n"
                f"**Error:** {err_msg[:1500]}\n"
                f"**Action:** sleeping {CRITICAL_ERROR_BACKOFF_S}s then resume."
            )
            try:
                delivered = broadcaster.send_discord_alert(critical_alert)
                if not delivered:
                    print(
                        "[CRITICAL BOT ERROR] Discord delivery failed after "
                        "retries — alert logged locally only:\n"
                        f"{critical_alert}"
                    )
            except Exception as alert_err:
                # Webhook path must never compound the failure.
                print(
                    f"[CRITICAL BOT ERROR] Discord alert raised: {alert_err} "
                    f"— local log only:\n{critical_alert}"
                )

            time.sleep(CRITICAL_ERROR_BACKOFF_S)
            continue


if __name__ == "__main__":
    # Trading-only entrypoint. Web/health server lives exclusively in main.py
    # (Render orchestrator). Do NOT start Flask / keep_alive here — that binds
    # PORT and collides with main.py ("Address already in use").
    run_macro_loop()
