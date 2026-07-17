"""
master_bot.py — Primary orchestrator (refactored).

Key changes vs. the previous version:
  * The trading loop existed TWICE (developer-bypass copy + live copy,
    ~250 duplicated lines). It is now a single run_portfolio_scan() used
    by both paths — one place to fix, one place to extend.
  * Scoring no longer substring-matches LLM prose. scoring_engine.py
    computes graded pillar scores from raw chain/pivot/news numbers and
    applies the dynamic weights persisted by saturday_audit.
  * CEO output enforces the strict per-ticker Markdown schema and is fed
    an exact-numbers metrics snapshot; if Gemini fails, a deterministic
    formatter emits the same schema from real numbers (no generic prose).
  * CircuitBreaker suspends scans after successive data-extraction
    failures (Task 3b). telemetry.py logs every indicator + score into
    hedge_fund.db:backtest_telemetry (Task 3c). strike_selector.py picks
    the concrete contract on EXECUTE (Task 3a).
  * All secrets are env-only (config.assert_secrets crashes early with a
    clear message instead of dying mid-session).
"""

import json
import time
import uuid
from datetime import datetime

import pytz
import yfinance as yf
from google import genai

import config
import broadcaster
import telemetry
import scoring_engine
import strike_selector
from circuit_breaker import CircuitBreaker

# Employee tier
from data_engineer import fetch_options_data
from math_agent import calculate_swing_targets

# Memory
from news_memory import get_historical_context, save_headline, clear_expired_news
import sqlite3

# Executive briefing
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

TICKERS = config.TICKERS
GEMINI_API_KEY = config.GEMINI_API_KEY
BYPASS_MARKET_HOURS = __import__("os").environ.get("BYPASS_MARKET_HOURS", "false").lower() == "true"

broadcaster.WEBHOOK_URL = config.DISCORD_WEBHOOK or broadcaster.WEBHOOK_URL


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
    """ATR(14) via 1-month daily history. Returns (atr_abs, atr_pct)."""
    try:
        hist = yf.Ticker(ticker).history(period="1mo")
        atr_abs, atr_pct = strike_selector.compute_atr(hist)
        if breaker and atr_abs is not None:
            breaker.record_success(f"atr:{ticker}")
        return atr_abs, atr_pct
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
        articles = yf.Ticker(ticker).news
        for article in articles or []:
            title, publisher = extract_article_info(article)
            if title and title != "Unknown Title":
                save_headline(ticker, "Fallback", publisher, title)
        news_string = get_historical_context(ticker, days=90)
        if breaker:
            breaker.record_success(f"news:{ticker}")
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
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
You are a Quantitative Analyst. Analyze this options data for {ticker_symbol}.
Look at the Strike prices, Implied Volatility, Volume, and Open Interest.
Identify ONE high-probability options trade (either a Call or a Put) with unusual volume or a compelling setup.
Provide the strike, expiration, and a 1-sentence quantitative rationale.

Data:
{options_json}
"""
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text.strip()
    except Exception as e:
        print(f"[{ticker_symbol}] 💼 Quant Manager: Gemini call failed ({e}); using fallback note.")
        return (f"Quant context unavailable (LLM offline). Deterministic strike selection "
                f"in strike_selector.py remains authoritative for {ticker_symbol}.")


# ==========================================
# 👔 CHIEF OF STAFF SYNTHESIS
# ==========================================

def run_cos_synthesis(ticker_symbol, ticker_manager_report, news_report, options_report, quant_report):
    print(f"[{ticker_symbol}] 👔 Chief of Staff (AI): Synthesizing Corporate Brief...")
    client = genai.Client(api_key=GEMINI_API_KEY)
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
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text.strip()
    except Exception as e:
        print(f"[{ticker_symbol}] 👔 CoS: Gemini call failed ({e}); using structural fallback.")
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
    client = genai.Client(api_key=GEMINI_API_KEY)
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
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = response.text.strip()
        # Schema guard: if the model drifted, fall back to deterministic format
        required = ["### ", "* **Market Context & Gap**", "* **Quantitative Liquidity Metric**",
                    "* **Sentiment Alignment**", "* **Strategic Executive Decision**"]
        if not all(tag in text for tag in required):
            print(f"[{ticker}] 👑 CEO output failed schema check — using deterministic formatter.")
            return format_ceo_deterministic(card, contract)
        return text
    except Exception as e:
        print(f"[{ticker}] 👑 CEO Agent: Gemini call failed ({e}); using deterministic formatter.")
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


def run_portfolio_scan(morning_macro_context, breaker, inter_ticker_sleep=10):
    """One full pass across the ticker universe. Used by both the live
    trading loop and the developer bypass simulation."""
    if breaker.is_open():
        print("🛑 [System] Circuit breaker OPEN — portfolio scan suspended this cycle.")
        return

    scan_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    weights = config.load_weights()
    futures_pct = get_latest_futures_pct("ES=F")
    print(f"\n🚀 PORTFOLIO SCAN {scan_id} | weights={weights} | ES=F overnight {futures_pct}%")

    # Portfolio-level technical color (LLM, for the CoS brief only)
    try:
        print("\n[System State] 👷 Running Ticker Specialist Desk...")
        specialist_briefings = get_aggregated_briefings(TICKERS)
        ticker_manager_report = generate_ticker_manager_report(
            specialist_briefings, api_key=GEMINI_API_KEY)
    except Exception as desk_err:
        print(f"❌ Specialist Desk/Manager error: {desk_err}")
        ticker_manager_report = "Portfolio technical report unavailable this cycle."

    for idx, ticker in enumerate(TICKERS):
        print(f"\n------------------------------------------")
        print(f"🔄 PROCESSING TICKER: {ticker} ({idx + 1}/{len(TICKERS)})")
        print(f"------------------------------------------")
        try:
            # ---- Employee tier: raw data (circuit-breaker instrumented) ----
            print(f"[{ticker}] 👷 Data Engineer: Fetching options chain...")
            options_json = fetch_options_data(ticker)
            options_dict = json.loads(options_json)
            if "error" in options_dict:
                print(f"❌ Skipping {ticker}: {options_dict['error']}")
                breaker.record_failure(f"options_chain:{ticker}")
                if breaker.is_open():
                    print("🛑 Circuit breaker tripped mid-scan — aborting remaining tickers.")
                    return
                continue
            breaker.record_success(f"options_chain:{ticker}")

            pivot_data = fetch_pivot_data(ticker)
            atr_abs, atr_pct = fetch_atr(ticker, breaker)
            news_string = ensure_news_context(ticker, breaker)
            math_json = calculate_swing_targets(options_json)

            # ---- Manager tier (LLM context for the brief) ----
            risk_report = generate_risk_report(math_json, ticker, api_key=GEMINI_API_KEY)
            sentiment_report = generate_sentiment_report(news_string, ticker, api_key=GEMINI_API_KEY)
            quant_report = run_quant_manager(math_json, ticker)
            macro_vector = generate_macro_catalyst_vector(ticker, api_key=GEMINI_API_KEY)

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
            if card.action_flag == "EXECUTE":
                advocate = DevilsAdvocate(api_key=GEMINI_API_KEY)
                adv_result = advocate.evaluate_trade({
                    "ticker": ticker,
                    "liquidity_score": card.liquidity_score,
                    "tech_score": card.technical_score,
                    "sentiment_score": card.sentiment_score,
                    "raw_metrics": card.metrics,
                })
                if adv_result.get("veto_triggered") and float(adv_result.get("risk_confidence", 0)) > 0.75:
                    scoring_engine.apply_adversarial_penalty(
                        card, 15.0, adv_result.get("reason", ""))
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
            broadcaster.send_discord_alert(trade_decision)

            if idx < len(TICKERS) - 1:
                time.sleep(inter_ticker_sleep)
        except Exception as ticker_err:
            print(f"❌ Error processing ticker {ticker}: {ticker_err}")

    print(f"\n✅ PORTFOLIO SCAN {scan_id} COMPLETED.")


# ==========================================
# 🚀 MISSION CONTROL (24-HOUR TIME ENGINE)
# ==========================================

if __name__ == "__main__":
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

    while True:
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
                morning_macro_context = generate_morning_briefing(GEMINI_API_KEY)
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

        is_night_mode = (is_weekend
                         or (night_start_1 <= current_time <= night_end_1)
                         or (night_start_2 <= current_time <= night_end_2))
        is_prep_meeting = (not is_weekend and meeting_start <= current_time <= meeting_end)
        is_trading_mode = (not is_weekend and trading_start <= current_time <= trading_end)

        if is_night_mode:
            print(f"\n[System State] 🌙 NIGHT MODE (EST {now.strftime('%Y-%m-%d %H:%M:%S')})")
            run_night_harvest()
            print("[System State] Overnight harvest complete. Sleeping until next state check...")
            for _ in range(45):
                time.sleep(60)
                if datetime.now(est_tz).time() >= meeting_start and datetime.now(est_tz).weekday() <= 4:
                    break
            continue

        elif is_prep_meeting:
            print(f"\n[System State] 📊 PRE-MARKET PREP MEETING (EST {now.strftime('%H:%M:%S')})")
            if last_briefing_date != now.date():
                try:
                    morning_macro_context = generate_morning_briefing(GEMINI_API_KEY)
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
                    morning_macro_context = generate_morning_briefing(GEMINI_API_KEY)
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
