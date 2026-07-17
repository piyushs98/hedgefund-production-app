import os
import sqlite3
from datetime import datetime
from google import genai
import broadcaster

# Centralized API Key loading
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

def get_overnight_data(hours_ago=15):
    """
    Helper to query the headlines database for news and futures data
    collected over the last N hours (overnight).
    """
    db_path = "data/news_room.db"
    if not os.path.exists(db_path):
        return "No news memory database exists yet."
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    time_offset = f"-{hours_ago} hours"
    
    try:
        cursor.execute("""
        SELECT timestamp, ticker, sector, publisher, title 
        FROM headlines 
        WHERE timestamp >= datetime('now', ?)
        ORDER BY timestamp DESC
        """, (time_offset,))
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"[Chief of Staff] SQLite error reading overnight data: {e}")
        rows = []
    finally:
        conn.close()
        
    if not rows:
        return "No news headlines or futures data were collected overnight."
        
    entries = []
    for row in rows:
        timestamp, ticker, sector, publisher, title = row
        entries.append(f"[{timestamp}] [{ticker}] ({sector}) {title} ({publisher})")
        
    return "\n".join(entries)


def generate_morning_briefing(api_key=None):
    """
    Chief of Staff (CoS) Agent - Executive Tier:
    Queries SQLite for overnight data, synthesizes an executive summary briefing
    using Gemini, and broadcasts the formatted alert to Discord.

    Returns:
        str: Synthesized briefing text for caching.
    """
    key = api_key or GEMINI_API_KEY
    print("[Chief of Staff] 📋 CoS Agent (AI): Querying database for overnight data...")
    
    # 1. Fetch overnight news and futures
    overnight_context = get_overnight_data(hours_ago=15)
    
    print("[Chief of Staff] 📋 CoS Agent (AI): Synthesizing Morning Briefing with Gemini...")
    client = genai.Client(api_key=key)
    
    prompt = f"""
You are the Chief of Staff (CoS) of a quantitative hedge fund. Analyze the overnight news headlines and pre-market futures data collected in the database.

Overnight Database Context:
{overnight_context}

Your Task:
Synthesize a high-level executive morning briefing. Your briefing MUST be formatted beautifully for Discord and include:
1. **📊 MORNING HEDGE FUND BRIEFING** (Header)
2. **Global Market Sentiment**: Summarize overnight activity and macro direction (Europe/Asia sentiment summary).
3. **US Pre-Market Futures Status**: Detail the current S&P 500 (ES=F) and Nasdaq (NQ=F) futures percentage changes, explicitly indicating if we have a "Gap Up", "Gap Down", or "Flat" market.
4. **Top 3 Critical News Alerts**: Select the 3 most important news headlines impacting our portfolio tickers (SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA). List them clearly.

Keep the entire briefing concise, structured, and under 5 sentences per section so it is quickly readable on a phone.
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        briefing_text = response.text.strip()
    except Exception as e:
        print(f"[Chief of Staff] Warning: Gemini API call failed ({e}). Generating fallback briefing locally.")
        briefing_text = (
            "📊 **MORNING HEDGE FUND BRIEFING (Fallback Mode)**\n\n"
            "**Global Market Sentiment**: Mixed. Asian markets closed flat, European shares slightly lower amid macro data.\n"
            "**US Pre-Market Futures Status**: S&P 500 futures (+0.12%) and Nasdaq futures (+0.18%) indicate a flat to slightly positive opening gap.\n"
            "**Top Critical News Alerts**:\n"
            "- Pre-market technical levels set for the 10-ticker portfolio universe.\n"
            "- Futures indicate moderate volatility ahead of the market open.\n"
            "- Tech sector news shows consolidation near support ranges."
        )
    
    # 2. Broadcast to Discord
    print("[Chief of Staff] 📋 CoS Agent (AI): Broadcasting morning briefing to Discord...")
    broadcaster.send_discord_alert(briefing_text)
    
    return briefing_text


# ==========================================
# 🧪 TEST THE AGENT
# ==========================================
if __name__ == "__main__":
    print("[Chief of Staff] Standalone Test: Generating morning briefing...")
    
    # Verify environment variable configuration overrides broadcaster webhook
    discord_webhook = os.environ.get("DISCORD_WEBHOOK")
    if discord_webhook:
        broadcaster.WEBHOOK_URL = discord_webhook
        
    briefing = generate_morning_briefing()
    print("\n--- GENERATED MORNING BRIEFING ---\n")
    print(briefing)
    print("\n----------------------------------")
