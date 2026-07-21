import os
import time
import sqlite3
import yfinance as yf
from news_memory import get_historical_context
from yf_client import SESSION, TICKER_PACING_SECONDS

# CENTRALIZED GROQ CONFIGURATION
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Verify Groq import
try:
    from groq import Groq
    HAS_GROQ = True
except ImportError:
    HAS_GROQ = False

def fetch_pivot_data(ticker):
    """
    Employee Tier - Specialist Tech Assist:
    Fetches the last completed trading day's metrics from yfinance
    and calculates standard pivot points, support (S1, S2), and resistance (R1, R2).
    Includes robust defaults if yfinance fails.
    """
    try:
        stock = yf.Ticker(ticker, session=SESSION)
        # Fetch 5 days to ensure we have completed trading sessions
        hist = stock.history(period="5d")
        if hist.empty:
            # Fallback to current info if history is not available
            info = stock.info
            close = info.get("regularMarketPrice") or info.get("previousClose") or 100.0
            high = close * 1.01
            low = close * 0.99
            prev_close = close
        else:
            last_row = hist.iloc[-1]
            high = last_row["High"]
            low = last_row["Low"]
            close = last_row["Close"]
            prev_close = hist["Close"].iloc[-2] if len(hist) > 1 else close
            
        # Standard Pivot Point Calculations
        # Safeguard against zero values
        if prev_close and prev_close > 0:
            pct_change = ((close - prev_close) / prev_close) * 100.0
        else:
            pct_change = 0.0
            
        pivot = (high + low + close) / 3.0 if (high + low + close) > 0 else 100.0
        r1 = (2 * pivot) - low if pivot > 0 else 101.0
        s1 = (2 * pivot) - high if pivot > 0 else 99.0
        r2 = pivot + (high - low) if pivot > 0 else 102.0
        s2 = pivot - (high - low) if pivot > 0 else 98.0
        
        return {
            "close": round(close, 2),
            "pivot": round(pivot, 2),
            "r1": round(r1, 2),
            "s1": round(s1, 2),
            "r2": round(r2, 2),
            "s2": round(s2, 2),
            "pct_change": round(pct_change, 2)
        }
    except Exception as e:
        print(f"❌ [Specialist Desk] Error fetching pivot data for {ticker}: {e}")
        # Default placeholder safe return
        return {
            "close": 100.0,
            "pivot": 100.0,
            "r1": 101.0,
            "s1": 99.0,
            "r2": 102.0,
            "s2": 98.0,
            "pct_change": 0.0
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
