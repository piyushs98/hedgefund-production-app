"""
tracker_agent.py — Micro-manager for open positions.

Runs independently of master_bot.py. Every 5 minutes it:
  1. Loads active trades from durable state (JSON today; swap later for DB).
  2. Pulls the last 5 minutes of 1m bars for each open ticker via yfinance.
  3. Asks Gemini (HTTP, not SDK) for a strict tactical decision:
       [HOLD] | [TAKE PROFIT] | [TRAILING STOP ADJUSTED] | [EXIT NOW]
  4. Pushes the decision to TRACKER_DISCORD_WEBHOOK.

Keep-alive: lightweight Flask server on a daemon thread so Render free-tier
health pings can hold the process open.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Any

import requests
import yfinance as yf
from flask import Flask

from yf_client import SESSION, TICKER_PACING_SECONDS

# ---------------------------------------------------------------------------
# Configuration (env-only secrets — same pattern as config.py / broadcaster)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TRACKER_DISCORD_WEBHOOK = os.environ.get("TRACKER_DISCORD_WEBHOOK", "")
GEMINI_MODEL = os.environ.get("TRACKER_GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

LOOP_SECONDS = int(os.environ.get("TRACKER_LOOP_SECONDS", "300"))  # 5 minutes
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
MAX_DISCORD_CHUNK = 1900

# Swap ACTIVE_TRADES_PATH (or the two state functions) when moving to a DB.
# Always resolve relative to this file so cwd/Render workdir cannot desync
# master_bot.py and tracker_agent.py.
_DEFAULT_ACTIVE_TRADES = os.path.join(os.path.dirname(__file__), "active_trades.json")
ACTIVE_TRADES_PATH = Path(
    os.environ.get("ACTIVE_TRADES_PATH", _DEFAULT_ACTIVE_TRADES)
)

VALID_DECISIONS = (
    "[HOLD]",
    "[TAKE PROFIT]",
    "[TRAILING STOP ADJUSTED]",
    "[EXIT NOW]",
)

# Decisions that close the position and drop it from state
CLOSING_DECISIONS = {"[TAKE PROFIT]", "[EXIT NOW]"}


# ===========================================================================
# STATE ABSTRACTION
# ---------------------------------------------------------------------------
# Isolated behind load/save helpers so a later Postgres/Redis backend only
# needs to rewrite these two functions (+ optional remove helper).
# ===========================================================================

def load_active_trades(path: Path | str | None = None) -> list[dict[str, Any]]:
    """
    Return the list of currently open trades.

    Expected trade shape (flexible — extra keys are preserved):
      {
        "trade_id": "uuid-or-string",
        "ticker": "AAPL",
        "direction": "CALL" | "PUT" | "LONG" | "SHORT",
        "entry_price": 1.25,          # premium or underlying entry
        "entry_timestamp": "ISO-8601",
        "strike": 190.0,              # optional (options)
        "expiration": "2026-07-18",   # optional
        "stop_loss": 1.00,
        "take_profit": 1.88,
        "trailing_stop": null,        # updated on TRAILING STOP ADJUSTED
        "notes": "..."
      }
    """
    store = Path(path) if path else ACTIVE_TRADES_PATH
    try:
        if not store.exists():
            return []
        with store.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw is None:
            return []
        if isinstance(raw, dict):
            # Empty {} is the orchestrator seed (main.py) — no open trades
            if not raw:
                return []
            # Allow {"trades": [...]} envelope
            if "trades" in raw and isinstance(raw["trades"], list):
                return [t for t in raw["trades"] if isinstance(t, dict)]
            # Single-trade dict only if it looks like a real position
            if raw.get("ticker"):
                return [raw]
            return []
        if isinstance(raw, list):
            return [t for t in raw if isinstance(t, dict)]
        print(f"[Tracker] Unexpected state shape in {store}; treating as empty.")
        return []
    except json.JSONDecodeError as e:
        print(f"[Tracker] Corrupt state file {store}: {e}. Returning empty list.")
        return []
    except OSError as e:
        print(f"[Tracker] Could not read state file {store}: {e}")
        return []


def save_active_trade(
    trade: dict[str, Any],
    path: Path | str | None = None,
) -> bool:
    """
    Upsert a single trade into the active-trades store.

    Match key order: trade_id → (ticker + entry_timestamp) → ticker alone
    (last-resort overwrite of first matching ticker).
    Returns True on successful write.
    """
    if not isinstance(trade, dict) or not trade.get("ticker"):
        print("[Tracker] save_active_trade: invalid trade payload.")
        return False

    store = Path(path) if path else ACTIVE_TRADES_PATH
    trades = load_active_trades(store)
    idx = _find_trade_index(trades, trade)
    if idx is None:
        trades.append(trade)
    else:
        # Preserve unknown fields from the previous record
        merged = {**trades[idx], **trade}
        trades[idx] = merged

    return _write_trades(trades, store)


def remove_active_trade(
    trade: dict[str, Any],
    path: Path | str | None = None,
) -> bool:
    """Drop a closed trade from state. Isolated for the same backend swap."""
    store = Path(path) if path else ACTIVE_TRADES_PATH
    trades = load_active_trades(store)
    idx = _find_trade_index(trades, trade)
    if idx is None:
        print(f"[Tracker] remove_active_trade: no match for {trade.get('ticker')}")
        return False
    trades.pop(idx)
    return _write_trades(trades, store)


def _find_trade_index(
    trades: list[dict[str, Any]], trade: dict[str, Any]
) -> int | None:
    tid = trade.get("trade_id")
    if tid:
        for i, t in enumerate(trades):
            if t.get("trade_id") == tid:
                return i
    ticker = trade.get("ticker")
    ts = trade.get("entry_timestamp")
    if ticker and ts:
        for i, t in enumerate(trades):
            if t.get("ticker") == ticker and t.get("entry_timestamp") == ts:
                return i
    if ticker:
        for i, t in enumerate(trades):
            if t.get("ticker") == ticker:
                return i
    return None


def _write_trades(trades: list[dict[str, Any]], store: Path) -> bool:
    """Atomic write (temp file + replace) so a crash mid-write cannot corrupt state."""
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(trades, indent=2, default=str)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(store.parent), prefix=".active_trades_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, store)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except OSError as e:
        print(f"[Tracker] Failed to write state to {store}: {e}")
        return False


# ===========================================================================
# KEEP-ALIVE (Render free-tier pings)
# ===========================================================================

_app = Flask("tracker_agent")


@_app.route("/")
def _home():
    return "Tracker Agent is awake and monitoring active trades."


@_app.route("/health")
def _health():
    trades = load_active_trades()
    return {
        "status": "ok",
        "active_trades": len(trades),
        "utc": datetime.now(timezone.utc).isoformat(),
    }


def _run_flask() -> None:
    port = int(os.environ.get("PORT", "10000"))
    # werkzeug request logs are noisy in a trading loop; suppress via default logger
    _app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


def keep_alive() -> None:
    t = Thread(target=_run_flask, name="tracker-keep-alive", daemon=True)
    t.start()
    print(f"[Tracker] Keep-alive server started on PORT={os.environ.get('PORT', 10000)}")


# ===========================================================================
# DATA FETCHING
# ===========================================================================

def fetch_micro_bars(ticker: str, minutes: int = 5) -> list[dict[str, Any]]:
    """
    Fetch recent 1-minute OHLCV bars for `ticker` and return the last
    `minutes` bars as a list of plain dicts (JSON-serializable for Gemini).

    Network / empty-market failures return [] rather than raising.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return []

    try:
        # yfinance 1m data requires a short lookback window; pull a day of
        # 1m bars then slice the tail so we only hand Gemini ~5 candles.
        hist = yf.Ticker(symbol, session=SESSION).history(period="1d", interval="1m")
        if hist is None or hist.empty:
            print(f"[Tracker] [{symbol}] No 1m history returned (market closed?).")
            return []

        tail = hist.tail(max(1, minutes))
        bars: list[dict[str, Any]] = []
        for ts, row in tail.iterrows():
            try:
                ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            except Exception:
                ts_str = str(ts)
            bars.append({
                "timestamp": ts_str,
                "open": _safe_float(row.get("Open")),
                "high": _safe_float(row.get("High")),
                "low": _safe_float(row.get("Low")),
                "close": _safe_float(row.get("Close")),
                "volume": _safe_float(row.get("Volume")),
            })
        return bars
    except Exception as e:
        # yfinance can raise a grab-bag of network / parse errors
        print(f"[Tracker] [{symbol}] yfinance fetch failed: {e}")
        return []


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
        if f != f:  # NaN
            return None
        return round(f, 6)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# AI EVALUATION (Gemini via direct HTTP — no SDK)
# ===========================================================================

def evaluate_trade_with_gemini(
    trade: dict[str, Any],
    micro_bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Call Gemini generateContent over HTTPS and parse a strict tactical tag.

    Returns:
      {
        "decision": "[HOLD]" | ...,
        "rationale": str,
        "raw": str,
        "new_trailing_stop": float | None,
        "error": str | None,
      }
    """
    if not GEMINI_API_KEY:
        return {
            "decision": "[HOLD]",
            "rationale": "GEMINI_API_KEY not set — defaulting to HOLD.",
            "raw": "",
            "new_trailing_stop": None,
            "error": "missing_api_key",
        }

    last_close = None
    if micro_bars:
        last_close = micro_bars[-1].get("close")

    prompt = f"""You are the risk-manager micro-agent for a quantitative hedge fund.
Your only job is short-horizon position management for ONE open trade.

ORIGINAL TRADE
- ticker: {trade.get("ticker")}
- direction: {trade.get("direction", "UNKNOWN")}
- entry_price: {trade.get("entry_price") or trade.get("entry_premium")}
- entry_timestamp: {trade.get("entry_timestamp")}
- strike: {trade.get("strike")}
- expiration: {trade.get("expiration")}
- stop_loss: {trade.get("stop_loss")}
- take_profit: {trade.get("take_profit")}
- trailing_stop: {trade.get("trailing_stop")}
- notes: {trade.get("notes", "")}

LATEST SPOT (from most recent 1m bar close): {last_close}

LAST {len(micro_bars)} ONE-MINUTE BARS (oldest → newest):
{json.dumps(micro_bars, indent=2)}

EVALUATION RULES
1. Decide whether the short-term trend that justified the entry is intact.
2. Respect existing stop_loss / take_profit / trailing_stop levels if price has
   clearly breached them — prefer [EXIT NOW] or [TAKE PROFIT] over hope.
3. If the trend is intact but risk should be locked in, choose
   [TRAILING STOP ADJUSTED] and propose a concrete new_trailing_stop number
   (absolute price, not a %).
4. If nothing material has changed, choose [HOLD].
5. You MUST return EXACTLY one of these four tactical tags, on its own first line:
   [HOLD]
   [TAKE PROFIT]
   [TRAILING STOP ADJUSTED]
   [EXIT NOW]
6. After the tag, write 1–3 short sentences of rationale with numbers (entry,
   last close, high/low of the window, stop levels). No markdown headings.
7. If and only if the tag is [TRAILING STOP ADJUSTED], include a final line:
   NEW_TRAILING_STOP=<number>

Respond now."""

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
        },
    }

    raw_text = ""
    last_err: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 429:
                wait = min(2.0 * attempt, 10.0)
                print(f"[Tracker] Gemini rate-limited; retry in {wait:.1f}s "
                      f"({attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                last_err = "rate_limited"
                continue
            if 500 <= resp.status_code < 600:
                print(f"[Tracker] Gemini HTTP {resp.status_code}; retry "
                      f"({attempt}/{MAX_RETRIES})")
                time.sleep(1.5 * attempt)
                last_err = f"http_{resp.status_code}"
                continue
            if resp.status_code != 200:
                print(f"[Tracker] Gemini HTTP {resp.status_code}: {resp.text[:300]}")
                return {
                    "decision": "[HOLD]",
                    "rationale": f"Gemini HTTP {resp.status_code}; holding by default.",
                    "raw": resp.text[:1000],
                    "new_trailing_stop": None,
                    "error": f"http_{resp.status_code}",
                }

            payload = resp.json()
            raw_text = _extract_gemini_text(payload)
            if not raw_text:
                last_err = "empty_response"
                time.sleep(1.0 * attempt)
                continue
            break
        except requests.Timeout:
            last_err = "timeout"
            print(f"[Tracker] Gemini timeout ({attempt}/{MAX_RETRIES})")
            time.sleep(1.5 * attempt)
        except requests.RequestException as e:
            last_err = f"network:{e}"
            print(f"[Tracker] Gemini network error ({attempt}/{MAX_RETRIES}): {e}")
            time.sleep(1.5 * attempt)
        except (ValueError, KeyError, TypeError) as e:
            last_err = f"parse:{e}"
            print(f"[Tracker] Gemini response parse error: {e}")
            break

    if not raw_text:
        return {
            "decision": "[HOLD]",
            "rationale": f"Gemini unavailable ({last_err}); defaulting to HOLD.",
            "raw": "",
            "new_trailing_stop": None,
            "error": last_err or "unknown",
        }

    decision = _parse_decision(raw_text)
    new_ts = _parse_trailing_stop(raw_text) if decision == "[TRAILING STOP ADJUSTED]" else None
    rationale = _strip_decision_line(raw_text)

    return {
        "decision": decision,
        "rationale": rationale,
        "raw": raw_text,
        "new_trailing_stop": new_ts,
        "error": None,
    }


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    try:
        candidates = payload.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        return "\n".join(t for t in texts if t).strip()
    except (AttributeError, IndexError, TypeError):
        return ""


def _parse_decision(text: str) -> str:
    """
    Strict bracket-tag extraction for tactical decisions.

    Uses regex ``\\[(.*?)\\]`` (first-match oriented) instead of brittle
    substring soft-matches like ``"HOLD" in text.upper()``.

    Rules:
      * Only approved tags in VALID_DECISIONS are accepted.
      * The first bracketed tag in the response must be an approved tag.
      * If multiple distinct approved tags appear (conflicting), default [HOLD].
      * Missing / unparseable / unapproved first tag → [HOLD].
    """
    if not text or not str(text).strip():
        print("[Tracker] Could not parse decision tag; defaulting to [HOLD].")
        return "[HOLD]"

    # Canonical lookup: "[HOLD]" etc. (whitespace-normalized, case-insensitive)
    valid_lookup = {
        re.sub(r"\s+", " ", tag.strip().upper()): tag for tag in VALID_DECISIONS
    }

    # Extract every bracketed segment in document order
    raw_inners = re.findall(r"\[(.*?)\]", str(text), flags=re.DOTALL)
    if not raw_inners:
        print("[Tracker] Could not parse decision tag; defaulting to [HOLD].")
        return "[HOLD]"

    normalized_tags: list[str] = []
    for inner in raw_inners:
        norm = "[" + re.sub(r"\s+", " ", inner.strip().upper()) + "]"
        normalized_tags.append(norm)

    # First bracketed tag must be on the approved list
    first = normalized_tags[0]
    if first not in valid_lookup:
        print(f"[Tracker] First tag {first!r} not approved; defaulting to [HOLD].")
        return "[HOLD]"

    # Any later approved tags that disagree with the first → conflict → HOLD
    approved_seen = {
        valid_lookup[t] for t in normalized_tags if t in valid_lookup
    }
    if len(approved_seen) > 1:
        print(f"[Tracker] Conflicting decision tags {sorted(approved_seen)}; "
              f"defaulting to [HOLD].")
        return "[HOLD]"

    return valid_lookup[first]


def _parse_trailing_stop(text: str) -> float | None:
    """
    Parse NEW_TRAILING_STOP=X with optional spaces and optional leading $.

    Accepts e.g. NEW_TRAILING_STOP=1.50, NEW_TRAILING_STOP = $1.50,
    NEW_TRAILING_STOP=$ 1.50
    """
    m = re.search(
        r"NEW_TRAILING_STOP\s*=\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
        text or "",
        re.I,
    )
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _strip_decision_line(text: str) -> str:
    lines = text.strip().splitlines()
    if not lines:
        return ""
    # Drop the first line if it is (or contains only) the decision tag
    first = lines[0].strip().upper()
    if any(tag in first for tag in VALID_DECISIONS) or first in {
        "HOLD", "TAKE PROFIT", "TRAILING STOP ADJUSTED", "EXIT NOW",
        "[HOLD]", "[TAKE PROFIT]", "[TRAILING STOP ADJUSTED]", "[EXIT NOW]",
    }:
        lines = lines[1:]
    # Drop NEW_TRAILING_STOP assignment from rationale body
    cleaned = [
        ln for ln in lines
        if not re.match(r"\s*NEW_TRAILING_STOP\s*=", ln, re.I)
    ]
    return "\n".join(cleaned).strip()


# ===========================================================================
# NOTIFICATION
# ===========================================================================

def send_tracker_alert(message: str) -> bool:
    """
    Push a message to TRACKER_DISCORD_WEBHOOK with chunking + retries.
    Never raises — a dead webhook must not kill the 5-minute loop.
    """
    if not message or not str(message).strip():
        return False

    if not TRACKER_DISCORD_WEBHOOK:
        print("[Tracker] TRACKER_DISCORD_WEBHOOK not set — local printout:")
        print(str(message)[:2000])
        return False

    chunks = _chunk_message(str(message))
    ok = True
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"[Tracker] Discord chunk {i}/{len(chunks)} ({len(chunk)} chars)")
        if not _post_discord_chunk(chunk):
            ok = False
        if i < len(chunks):
            time.sleep(0.6)
    return ok


def _chunk_message(message: str, limit: int = MAX_DISCORD_CHUNK) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    current = ""
    for line in message.split("\n"):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _post_discord_chunk(chunk: str) -> bool:
    data = {"content": chunk, "username": "Tracker Agent 📡"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                TRACKER_DISCORD_WEBHOOK,
                json=data,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("retry_after", 2.0))
                except Exception:
                    wait = 2.0
                print(f"[Tracker] Discord 429; waiting {wait:.1f}s "
                      f"({attempt}/{MAX_RETRIES})")
                time.sleep(wait + 0.25)
                continue
            print(f"[Tracker] Discord HTTP {resp.status_code}: {resp.text[:200]}")
            if 500 <= resp.status_code < 600:
                time.sleep(1.5 * attempt)
                continue
            return False
        except requests.RequestException as e:
            print(f"[Tracker] Discord network error ({attempt}/{MAX_RETRIES}): {e}")
            time.sleep(1.5 * attempt)
    return False


def _format_alert(
    trade: dict[str, Any],
    decision: str,
    rationale: str,
    micro_bars: list[dict[str, Any]],
    new_trailing_stop: float | None = None,
) -> str:
    ticker = trade.get("ticker", "?")
    entry = trade.get("entry_price") or trade.get("entry_premium")
    last = micro_bars[-1].get("close") if micro_bars else "n/a"
    high = max((b.get("high") or float("-inf") for b in micro_bars), default=None)
    low = min((b.get("low") or float("inf") for b in micro_bars), default=None)
    if high == float("-inf"):
        high = None
    if low == float("inf"):
        low = None

    emoji = {
        "[HOLD]": "⏸",
        "[TAKE PROFIT]": "💰",
        "[TRAILING STOP ADJUSTED]": "📈",
        "[EXIT NOW]": "🚨",
    }.get(decision, "📡")

    lines = [
        f"{emoji} **TRACKER {decision}** — `{ticker}`",
        f"Direction: `{trade.get('direction', 'N/A')}` | "
        f"Entry: `{entry}` @ `{trade.get('entry_timestamp', 'n/a')}`",
        f"Last 1m close: `{last}` | Window H/L: `{high}` / `{low}`",
        f"Stops — SL `{trade.get('stop_loss')}` | TP `{trade.get('take_profit')}` | "
        f"Trail `{trade.get('trailing_stop')}`",
    ]
    if new_trailing_stop is not None:
        lines.append(f"Proposed trailing stop: `{new_trailing_stop}`")
    if trade.get("strike") is not None:
        lines.append(
            f"Contract: `{trade.get('direction')}` `{trade.get('strike')}` "
            f"exp `{trade.get('expiration')}`"
        )
    if rationale:
        lines.append(f"\n{rationale}")
    lines.append(f"\n_UTC {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}_")
    return "\n".join(lines)


# ===========================================================================
# MAIN LOOP
# ===========================================================================

def process_trade(trade: dict[str, Any]) -> None:
    """Fetch micro-data → Gemini decision → notify → update state."""
    ticker = str(trade.get("ticker", "")).upper()
    print(f"\n[Tracker] Evaluating {ticker} "
          f"(id={trade.get('trade_id', 'n/a')})...")

    micro_bars = fetch_micro_bars(ticker, minutes=5)
    if not micro_bars:
        # Still surface a soft alert so silent data outages are visible
        send_tracker_alert(
            f"⚠️ **TRACKER DATA GAP** — `{ticker}`\n"
            f"No 1m bars available this cycle (market closed or network). "
            f"Holding position without AI evaluation."
        )
        return

    result = evaluate_trade_with_gemini(trade, micro_bars)
    decision = result["decision"]
    rationale = result["rationale"]
    new_ts = result.get("new_trailing_stop")

    print(f"[Tracker] [{ticker}] decision={decision} err={result.get('error')}")

    alert = _format_alert(trade, decision, rationale, micro_bars, new_ts)
    send_tracker_alert(alert)

    # State transitions
    if decision == "[TRAILING STOP ADJUSTED]" and new_ts is not None:
        trade["trailing_stop"] = new_ts
        trade["last_decision"] = decision
        trade["last_evaluated_at"] = datetime.now(timezone.utc).isoformat()
        save_active_trade(trade)
        print(f"[Tracker] [{ticker}] trailing_stop → {new_ts}")
    elif decision in CLOSING_DECISIONS:
        trade["last_decision"] = decision
        trade["closed_at"] = datetime.now(timezone.utc).isoformat()
        remove_active_trade(trade)
        print(f"[Tracker] [{ticker}] removed from active trades ({decision})")
    else:
        trade["last_decision"] = decision
        trade["last_evaluated_at"] = datetime.now(timezone.utc).isoformat()
        save_active_trade(trade)


def run_cycle() -> None:
    """One full pass over all active trades."""
    try:
        trades = load_active_trades()
    except Exception as e:
        print(f"[Tracker] load_active_trades crashed: {e}")
        traceback.print_exc()
        return

    if not trades:
        print("[Tracker] No active trades — sleeping until next cycle.")
        return

    print(f"[Tracker] {len(trades)} active trade(s) this cycle.")
    for trade in list(trades):
        try:
            process_trade(trade)
        except Exception as e:
            ticker = trade.get("ticker", "?")
            print(f"[Tracker] Unhandled error on {ticker}: {e}")
            traceback.print_exc()
            try:
                send_tracker_alert(
                    f"🔥 **TRACKER ERROR** — `{ticker}`\n```{e}```"
                )
            except Exception:
                pass
        # Brief pause between tickers to stay friendly to yfinance / Gemini
        time.sleep(TICKER_PACING_SECONDS)


def assert_runtime_config() -> None:
    if not GEMINI_API_KEY:
        print("[Tracker] WARNING: GEMINI_API_KEY missing — evaluations will HOLD.")
    if not TRACKER_DISCORD_WEBHOOK:
        print("[Tracker] WARNING: TRACKER_DISCORD_WEBHOOK missing — "
              "alerts print locally.")
    print(f"[Tracker] State file: {ACTIVE_TRADES_PATH.resolve()}")
    print(f"[Tracker] Loop interval: {LOOP_SECONDS}s | model={GEMINI_MODEL}")


def run_micro_loop() -> None:
    """
    Continuous 5-minute tracker loop (no web server).

    Safe to call from main.py as a daemon thread. Each cycle is isolated so
    network drops / Gemini throttling log + sleep rather than killing the process.
    """
    print("\n--- TRACKER AGENT ONLINE (micro loop) ---")
    assert_runtime_config()

    # Seed empty state if missing (orchestrator prefers {}; list also accepted)
    if not ACTIVE_TRADES_PATH.exists():
        try:
            ACTIVE_TRADES_PATH.write_text("{}", encoding="utf-8")
            print(f"[Tracker] Seeded empty state {{}} at {ACTIVE_TRADES_PATH}")
        except OSError as e:
            print(f"[Tracker] Could not seed state file: {e}")

    # Boot heartbeat — confirms webhook connectivity even with 0 open trades
    try:
        trade_count = len(load_active_trades())
    except Exception:
        trade_count = 0
    boot_msg = (
        "Tracker Agent Online - Monitoring Active Trades"
        f"\nState file: `{ACTIVE_TRADES_PATH}`"
        f"\nOpen positions at boot: **{trade_count}**"
        f"\nUTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print("[Tracker] Posting boot heartbeat to Discord...")
    send_tracker_alert(boot_msg)

    error_backoff = 30
    while True:
        cycle_start = time.monotonic()
        print(f"\n[Tracker] === cycle @ "
              f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC ===")
        try:
            run_cycle()
        except Exception as e:
            # Absolute last-resort guard so the process never dies mid-loop
            print(f"[Tracker] Cycle-level crash (retry in {error_backoff}s): {e}")
            traceback.print_exc()
            time.sleep(error_backoff)

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(5.0, LOOP_SECONDS - elapsed)
        print(f"[Tracker] Cycle done in {elapsed:.1f}s; sleeping {sleep_for:.1f}s.")
        time.sleep(sleep_for)


def main() -> None:
    """Standalone entrypoint: keep-alive Flask + micro loop."""
    keep_alive()
    run_micro_loop()


if __name__ == "__main__":
    main()
