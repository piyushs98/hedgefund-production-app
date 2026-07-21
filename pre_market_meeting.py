import os
import re
import sqlite3
import traceback

import requests
import broadcaster

# Centralized API Key loading
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# Prefer shared config path when available; fall back to script-relative DB.
try:
    import config as _config
    _DEFAULT_DB = getattr(_config, "NEWS_DB_PATH", None)
except Exception:
    _DEFAULT_DB = None

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_DB_PATH = _DEFAULT_DB or os.path.join(_SCRIPT_DIR, "data", "news_room.db")

# Cap overnight context so the model is not overloaded (token / request limits).
MAX_OVERNIGHT_CHARS = 24000
MAX_HEADLINE_ROWS = 120


def get_overnight_data(hours_ago=15):
    """
    Helper to query the headlines database for news and futures data
    collected over the last N hours (overnight).
    """
    db_path = NEWS_DB_PATH
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
        LIMIT ?
        """, (time_offset, MAX_HEADLINE_ROWS))
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"[Chief of Staff] SQLite error reading overnight data: {type(e).__name__}: {e}")
        traceback.print_exc()
        rows = []
    finally:
        conn.close()

    if not rows:
        return "No news headlines or futures data were collected overnight."

    entries = []
    for row in rows:
        timestamp, ticker, sector, publisher, title = row
        # Sanitize for prompt safety (strip control chars, normalize whitespace)
        title_clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(title or "")).strip()
        ticker_clean = str(ticker or "N/A").strip()
        sector_clean = str(sector or "N/A").strip()
        publisher_clean = str(publisher or "N/A").strip()
        ts_clean = str(timestamp or "").strip()
        entries.append(
            f"[{ts_clean}] [{ticker_clean}] ({sector_clean}) {title_clean} ({publisher_clean})"
        )

    text = "\n".join(entries)
    if len(text) > MAX_OVERNIGHT_CHARS:
        text = text[:MAX_OVERNIGHT_CHARS] + "\n...[truncated for model context limit]"
    return text


def _parse_futures_from_context(overnight_context: str) -> dict:
    """Pull latest ES=F / NQ=F pct moves from scraped headline titles when present."""
    result = {}
    for symbol in ("ES=F", "NQ=F"):
        # Titles look like: "S&P 500 E-mini Futures is trending UP by +0.14% ..."
        # or contain the ticker tag in the formatted line.
        pattern = re.compile(
            rf"\[{re.escape(symbol)}\].*?([+-]?\d+(?:\.\d+)?)\s*%",
            re.IGNORECASE,
        )
        m = pattern.search(overnight_context)
        if m:
            try:
                result[symbol] = float(m.group(1))
            except ValueError:
                pass
    return result


def _gap_label(pct: float | None) -> str:
    if pct is None:
        return "Unknown"
    if pct >= 0.25:
        return "Gap Up"
    if pct <= -0.25:
        return "Gap Down"
    return "Flat"


def _build_live_data_briefing(overnight_context: str) -> str:
    """
    Live briefing from overnight DB rows (not hardcoded market numbers).
    Used only when DeepSeek returns no usable text after a successful API call path,
    or as a last-resort structured summary when the API hard-fails.
    """
    futures = _parse_futures_from_context(overnight_context)
    es = futures.get("ES=F")
    nq = futures.get("NQ=F")

    if es is not None or nq is not None:
        es_str = f"{es:+.2f}%" if es is not None else "n/a"
        nq_str = f"{nq:+.2f}%" if nq is not None else "n/a"
        if es is not None and nq is not None:
            primary = (es + nq) / 2.0
        else:
            primary = es if es is not None else nq
        gap = _gap_label(primary)
        futures_line = (
            f"S&P 500 futures (ES=F) {es_str} and Nasdaq futures (NQ=F) {nq_str} "
            f"→ **{gap}** open bias from overnight prints."
        )
    else:
        futures_line = (
            "No recent ES=F / NQ=F futures prints found in overnight headlines; "
            "treat the open gap as unknown until fresh futures data arrives."
        )

    # Top non-futures headlines for the alerts section
    alerts = []
    for line in overnight_context.splitlines():
        if not line.strip():
            continue
        if "[ES=F]" in line or "[NQ=F]" in line:
            continue
        # Keep the human-readable tail after the first "] "
        display = line
        if "] " in line:
            # drop first two bracket tags roughly: [ts] [ticker] ...
            parts = line.split("] ", 2)
            display = parts[-1] if parts else line
        alerts.append(f"- {display.strip()}")
        if len(alerts) >= 3:
            break
    if not alerts:
        if "No news" in overnight_context or "does not exist" in overnight_context:
            alerts = [f"- {overnight_context.strip()}"]
        else:
            alerts = ["- Overnight tape available but no non-futures headlines ranked."]

    headline_count = max(0, overnight_context.count("\n") + (1 if overnight_context.strip() else 0))
    if "No news" in overnight_context or "does not exist" in overnight_context:
        sentiment = overnight_context.strip()
    else:
        sentiment = (
            f"Scanned {headline_count} overnight database entries across macro, "
            f"sector, and portfolio tickers. Directional bias inferred from futures "
            f"and headline mix above — verify at the open."
        )

    return (
        "📊 **MORNING HEDGE FUND BRIEFING**\n\n"
        f"**Global Market Sentiment**: {sentiment}\n"
        f"**US Pre-Market Futures Status**: {futures_line}\n"
        "**Top Critical News Alerts**:\n"
        + "\n".join(alerts)
    )


def _extract_deepseek_text(payload: dict) -> str:
    """
    Safely extract assistant content from a DeepSeek chat completions JSON body.
    """
    try:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None:
            return ""
        return str(content).strip()
    except Exception as e:
        print(
            f"[Chief of Staff] DeepSeek response parse failed: "
            f"{type(e).__name__}: {e}"
        )
        traceback.print_exc()
        return ""


def _call_deepseek(api_key: str, overnight_context: str) -> str:
    """
    Call DeepSeek chat completions and return briefing text, or raise on hard failure.
    """
    system_instruction = (
        "You are the Chief of Staff (CoS) of a quantitative hedge fund. "
        "This morning briefing is designed to warn the CEO trading bot of potential "
        "liquidity vacuums and whipsaws around scheduled macro event windows. "
        "The CEO bot MUST know exact times so it can halt trading around those windows. "
        "Be precise, structured, and operational — not narrative fluff."
    )

    user_prompt = (
        "Analyze the overnight news headlines and pre-market futures data "
        "collected in the database.\n\n"
        "Overnight Database Context:\n"
        f"{overnight_context}\n\n"
        "Your Task:\n"
        "Synthesize a high-level executive morning briefing. Your briefing MUST "
        "be formatted beautifully for Discord and include:\n"
        "1. **📊 MORNING HEDGE FUND BRIEFING** (Header)\n"
        "2. **Global Market Sentiment**: Summarize overnight activity and macro "
        "direction (Europe/Asia sentiment summary).\n"
        "3. **US Pre-Market Futures Status**: Detail the current S&P 500 (ES=F) "
        "and Nasdaq (NQ=F) futures percentage changes, explicitly indicating if "
        "we have a \"Gap Up\", \"Gap Down\", or \"Flat\" market. Use the exact "
        "percentages from the Overnight Database Context when present.\n"
        "4. **Top 3 Critical News Alerts**: Select the 3 most important news "
        "headlines impacting our portfolio tickers (SPY, QQQ, IWM, AAPL, MSFT, "
        "NVDA, AMZN, META, GOOGL, TSLA). List them clearly.\n"
        "5. **⚠️ Scheduled Macro / Liquidity Risk Windows**: Critically analyze "
        "the provided news/headlines and highlight ANY upcoming economic data "
        "releases (CPI, PPI, NFP, GDP, jobless claims, retail sales, etc.), Fed "
        "speeches / FOMC speakers, central bank decisions, or other macro events. "
        "For each event, extract the EXACT TIMINGS (e.g., 8:30 AM EST, 10:00 AM EST). "
        "If a time is stated without a timezone, assume US Eastern and label it EST. "
        "If no timed macro events are found, explicitly say so. Frame each window "
        "as a halt-trading advisory for the CEO bot due to liquidity vacuums and "
        "whipsaw risk.\n\n"
        "Keep the entire briefing concise, structured, and under 5 sentences per "
        "section so it is quickly readable on a phone.\n"
        "Do NOT invent futures percentages that are not in the database context.\n"
        "Do NOT invent event times that are not supported by the headlines; if "
        "timing is ambiguous, say so clearly.\n"
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "stream": False,
    }

    response = requests.post(
        DEEPSEEK_API_URL,
        headers=headers,
        json=body,
        timeout=90,
    )
    # Raise for non-2xx so callers hit the except fallback path
    if not response.ok:
        # Include a short body snippet for diagnosis without dumping secrets
        snippet = (response.text or "")[:500]
        raise RuntimeError(
            f"DeepSeek HTTP {response.status_code}: {snippet}"
        )

    payload = response.json()
    return _extract_deepseek_text(payload)


def generate_morning_briefing(api_key=None):
    """
    Chief of Staff (CoS) Agent - Executive Tier:
    Queries SQLite for overnight data, synthesizes an executive summary briefing
    using DeepSeek, and broadcasts the formatted alert to Discord.

    Returns:
        str: Synthesized briefing text for caching.
    """
    key = (api_key or DEEPSEEK_API_KEY or "").strip()
    print("[Chief of Staff] 📋 CoS Agent (AI): Querying database for overnight data...")

    # 1. Fetch overnight news and futures
    overnight_context = get_overnight_data(hours_ago=15)
    print(
        f"[Chief of Staff] Overnight context: {len(overnight_context)} chars "
        f"from {NEWS_DB_PATH}"
    )

    print("[Chief of Staff] 📋 CoS Agent (AI): Synthesizing Morning Briefing with DeepSeek...")
    briefing_text = None

    if not key:
        print(
            "[Chief of Staff] ERROR: DEEPSEEK_API_KEY is empty — cannot call DeepSeek. "
            "Building live data-driven briefing from overnight DB instead."
        )
        briefing_text = _build_live_data_briefing(overnight_context)
    else:
        try:
            briefing_text = _call_deepseek(key, overnight_context)
            if not briefing_text:
                # Log so silent empty replies are diagnosable
                print(
                    "[Chief of Staff] ERROR: DeepSeek returned empty text after "
                    "successful call."
                )
                print(
                    "[Chief of Staff] Falling back to live overnight-data briefing "
                    "(not the hardcoded template)."
                )
                briefing_text = _build_live_data_briefing(overnight_context)
            else:
                print(
                    f"[Chief of Staff] DeepSeek briefing OK ({len(briefing_text)} chars)."
                )
        except Exception as e:
            # Never fail silently — log type, message, and full traceback
            print(
                f"[Chief of Staff] Warning: DeepSeek API call failed: "
                f"{type(e).__name__}: {e}"
            )
            traceback.print_exc()
            print(
                "[Chief of Staff] Generating live data-driven briefing from overnight "
                "DB (Fallback Mode label only if data also empty)."
            )
            briefing_text = _build_live_data_briefing(overnight_context)
            # Only tag Fallback Mode when we truly have no overnight tape
            if (
                "No news headlines" in overnight_context
                or "does not exist" in overnight_context
            ):
                briefing_text = briefing_text.replace(
                    "📊 **MORNING HEDGE FUND BRIEFING**",
                    "📊 **MORNING HEDGE FUND BRIEFING (Fallback Mode)**",
                    1,
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
