import os
import re
import sqlite3
import traceback

from google import genai
import broadcaster

# Centralized API Key loading
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Prefer shared config path when available; fall back to script-relative DB.
try:
    import config as _config
    _DEFAULT_DB = getattr(_config, "NEWS_DB_PATH", None)
except Exception:
    _DEFAULT_DB = None

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_DB_PATH = _DEFAULT_DB or os.path.join(_SCRIPT_DIR, "data", "news_room.db")

# Cap overnight context so Gemini is not overloaded (token / request limits).
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
        text = text[:MAX_OVERNIGHT_CHARS] + "\n...[truncated for Gemini context limit]"
    return text


def _extract_gemini_text(response) -> str:
    """
    Safely extract model text from a google-genai GenerateContentResponse.

    response.text raises ValueError when candidates have no Parts (safety block,
    empty completion). Walk candidates/parts manually as a fallback.
    """
    try:
        text = getattr(response, "text", None)
        if text is not None and str(text).strip():
            return str(text).strip()
    except Exception as e:
        print(
            f"[Chief of Staff] response.text accessor failed: "
            f"{type(e).__name__}: {e}"
        )

    chunks = []
    try:
        candidates = getattr(response, "candidates", None) or []
        for i, cand in enumerate(candidates):
            finish = getattr(cand, "finish_reason", None)
            if finish is not None:
                print(f"[Chief of Staff] candidate[{i}] finish_reason={finish}")
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                part_text = getattr(part, "text", None)
                if part_text:
                    chunks.append(str(part_text))
    except Exception as e:
        print(
            f"[Chief of Staff] candidate/parts walk failed: "
            f"{type(e).__name__}: {e}"
        )
        traceback.print_exc()

    return "\n".join(chunks).strip()


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
    Used only when Gemini returns no usable text after a successful API call path,
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


def generate_morning_briefing(api_key=None):
    """
    Chief of Staff (CoS) Agent - Executive Tier:
    Queries SQLite for overnight data, synthesizes an executive summary briefing
    using Gemini, and broadcasts the formatted alert to Discord.

    Returns:
        str: Synthesized briefing text for caching.
    """
    key = (api_key or GEMINI_API_KEY or "").strip()
    print("[Chief of Staff] 📋 CoS Agent (AI): Querying database for overnight data...")

    # 1. Fetch overnight news and futures
    overnight_context = get_overnight_data(hours_ago=15)
    print(
        f"[Chief of Staff] Overnight context: {len(overnight_context)} chars "
        f"from {NEWS_DB_PATH}"
    )

    print("[Chief of Staff] 📋 CoS Agent (AI): Synthesizing Morning Briefing with Gemini...")
    briefing_text = None

    if not key:
        print(
            "[Chief of Staff] ERROR: GEMINI_API_KEY is empty — cannot call Gemini. "
            "Building live data-driven briefing from overnight DB instead."
        )
        briefing_text = _build_live_data_briefing(overnight_context)
    else:
        # Build prompt without embedding untrusted braces into nested f-expression issues.
        # (Variable interpolation is already complete before the API sees the string.)
        prompt = (
            "You are the Chief of Staff (CoS) of a quantitative hedge fund. "
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
            "NVDA, AMZN, META, GOOGL, TSLA). List them clearly.\n\n"
            "Keep the entire briefing concise, structured, and under 5 sentences per "
            "section so it is quickly readable on a phone.\n"
            "Do NOT invent futures percentages that are not in the database context.\n"
        )

        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            briefing_text = _extract_gemini_text(response)
            if not briefing_text:
                # Log response shape so silent empty replies are diagnosable
                print(
                    "[Chief of Staff] ERROR: Gemini returned empty text after "
                    f"successful call. raw_response_type={type(response).__name__}"
                )
                try:
                    # Best-effort debug dump (may not always be serializable)
                    print(f"[Chief of Staff] response repr (truncated): {repr(response)[:800]}")
                except Exception:
                    pass
                print(
                    "[Chief of Staff] Falling back to live overnight-data briefing "
                    "(not the hardcoded template)."
                )
                briefing_text = _build_live_data_briefing(overnight_context)
            else:
                print(
                    f"[Chief of Staff] Gemini briefing OK ({len(briefing_text)} chars)."
                )
        except Exception as e:
            # Never fail silently — log type, message, and full traceback
            print(
                f"[Chief of Staff] Warning: Gemini API call failed: "
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
