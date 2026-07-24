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
import sqlite3
import tempfile
import time
import traceback
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from threading import Thread
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from flask import Flask

from yf_client import SESSION, TICKER_PACING_SECONDS

import virtual_broker

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
try:
    import config as _dash_config
    DASHBOARD_URL = getattr(
        _dash_config,
        "DASHBOARD_URL",
        "https://hedgefund-production-app.onrender.com",
    )
except Exception:
    DASHBOARD_URL = (
        os.environ.get("DASHBOARD_URL")
        or "https://hedgefund-production-app.onrender.com"
    )
DASHBOARD_URL = str(DASHBOARD_URL).rstrip("/")

LOOP_SECONDS = int(os.environ.get("TRACKER_LOOP_SECONDS", "300"))  # 5 minutes
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
MAX_DISCORD_CHUNK = 1900

# US equity/options session window used to skip overnight 1m bar polls.
# 9:15 AM – 4:15 PM Eastern covers RTH + a short post-close cushion for 1m data.
_ET = ZoneInfo("America/New_York")
_OPTIONS_SESSION_OPEN = dt_time(9, 15)
_OPTIONS_SESSION_CLOSE = dt_time(16, 15)

# Swap ACTIVE_TRADES_PATH (or the two state functions) when moving to a DB.
# Absolute default next to this file — same formula as main.py / master_bot.py.
ACTIVE_TRADES_PATH = Path(
    os.environ.get(
        "ACTIVE_TRADES_PATH",
        os.path.abspath(
            os.path.join(os.path.dirname(__file__), "active_trades.json")
        ),
    )
)

# SQLite durable mirror of open trades (survives empty/wiped JSON on restart).
try:
    import config as _config
    _DEFAULT_NEWS_DB = getattr(_config, "NEWS_DB_PATH", None)
except Exception:
    _DEFAULT_NEWS_DB = None
NEWS_DB_PATH = os.environ.get(
    "NEWS_DB_PATH",
    _DEFAULT_NEWS_DB or os.path.join(os.path.dirname(__file__), "data", "news_room.db"),
)

VALID_DECISIONS = (
    "[HOLD]",
    "[TAKE PROFIT]",
    "[TRAILING STOP ADJUSTED]",
    "[EXIT NOW]",
    "[STOP LOSS]",
    "[STOP LOSS TRIGGERED]",
)

# Decisions that close the position and drop it from state
CLOSING_DECISIONS = {
    "[TAKE PROFIT]",
    "[EXIT NOW]",
    "[STOP LOSS]",
    "[STOP LOSS TRIGGERED]",
}


def _resolve_exit_price(
    trade: dict[str, Any],
    decision: str,
    micro_bars: list[dict[str, Any]] | None = None,
) -> float:
    """
    Best-effort premium exit for the virtual ledger.

    Prefer explicit TP / SL / trailing levels (stored as option premium).
    Fall back to entry (flat) if nothing else is usable — never use
    underlying 1m closes as premium substitutes.
    """
    entry = trade.get("entry_price") or trade.get("entry_premium") or 0.0
    try:
        entry_f = float(entry)
    except (TypeError, ValueError):
        entry_f = 0.0

    candidates: list[Any] = []
    if decision == "[TAKE PROFIT]":
        candidates.append(trade.get("take_profit") or trade.get("target_price"))
    elif decision in ("[STOP LOSS]", "[STOP LOSS TRIGGERED]"):
        candidates.append(trade.get("stop_loss"))
        if trade.get("trailing_stop") is not None:
            candidates.append(trade.get("trailing_stop"))
    elif decision == "[EXIT NOW]":
        if trade.get("trailing_stop") is not None:
            candidates.append(trade.get("trailing_stop"))
        candidates.append(trade.get("stop_loss"))
        # Mid between entry and TP as a soft estimate when no stop set
        tp = trade.get("take_profit") or trade.get("target_price")
        if tp is not None and entry_f:
            try:
                candidates.append((entry_f + float(tp)) / 2.0)
            except (TypeError, ValueError):
                pass

    for c in candidates:
        if c is None:
            continue
        try:
            v = float(c)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return entry_f


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
    SQLite `active_trades_store` is full-synced via `_write_trades`.
    Returns True on successful write.
    """
    if not isinstance(trade, dict) or not trade.get("ticker"):
        print("[Tracker] save_active_trade: invalid trade payload.")
        return False

    store = Path(path) if path else ACTIVE_TRADES_PATH
    trades = load_active_trades(store)
    idx = _find_trade_index(trades, trade)
    if idx is None:
        saved = dict(trade)
        trades.append(saved)
    else:
        # Preserve unknown fields from the previous record
        saved = {**trades[idx], **trade}
        trades[idx] = saved

    return _write_trades(trades, store)


def remove_active_trade(
    trade: dict[str, Any],
    path: Path | str | None = None,
) -> bool:
    """
    Drop a closed trade from JSON state.

    SQLite `active_trades_store` is full-synced to the remaining list via
    `_write_trades` (absolute parity — no selective DELETE).
    """
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
    """
    Atomic write (temp file + replace) so a crash mid-write cannot corrupt state.

    On success, full-syncs SQLite `active_trades_store` to match the JSON list
    exactly (absolute parity — no selective upsert/delete).
    """
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
        # JSON is source of truth for this write; mirror entire open set to SQLite
        _sync_sqlite_to_json(trades)
        return True
    except OSError as e:
        print(f"[Tracker] Failed to write state to {store}: {e}")
        return False


# ===========================================================================
# SQLITE MIRROR (active_trades_store in news_room.db)
# ---------------------------------------------------------------------------
# Survives process restarts when active_trades.json is empty / wiped by the
# orchestrator. JSON remains the hot path; every successful JSON write
# full-replaces the SQLite table so zombie/ghost trades are impossible.
# ===========================================================================

def _trade_store_key(trade: dict[str, Any]) -> str:
    """Stable primary key for active_trades_store."""
    tid = trade.get("trade_id")
    if tid:
        return str(tid)
    ticker = str(trade.get("ticker") or "UNKNOWN").upper()
    ts = trade.get("entry_timestamp") or trade.get("entry_time") or ""
    return f"{ticker}|{ts}"


def _ensure_active_trades_store(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS active_trades_store (
            trade_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            entry_timestamp TEXT,
            payload TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )


def _sync_sqlite_to_json(current_trades_list: list[dict[str, Any]]) -> bool:
    """
    Absolute state sync: wipe active_trades_store and re-insert the current
    open-trades list so SQLite always matches JSON after a successful write.

    Called from `_write_trades` only after the JSON file has been updated.
    """
    trades = current_trades_list if isinstance(current_trades_list, list) else []
    try:
        db_path = NEWS_DB_PATH
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            _ensure_active_trades_store(conn)
            conn.execute("DELETE FROM active_trades_store")
            for trade in trades:
                if not isinstance(trade, dict) or not trade.get("ticker"):
                    continue
                trade_id = _trade_store_key(trade)
                ticker = str(trade.get("ticker", "")).upper()
                entry_ts = trade.get("entry_timestamp") or trade.get("entry_time")
                payload_obj = dict(trade)
                if not payload_obj.get("trade_id"):
                    payload_obj["trade_id"] = trade_id
                conn.execute(
                    """
                    INSERT INTO active_trades_store
                        (trade_id, ticker, entry_timestamp, payload, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        trade_id,
                        ticker,
                        str(entry_ts) if entry_ts is not None else None,
                        json.dumps(payload_obj, default=str),
                    ),
                )
            conn.commit()
        return True
    except Exception as e:
        print(f"[Tracker] SQLite full-sync failed: {e}")
        traceback.print_exc()
        return False


def _sqlite_load_all_trades() -> list[dict[str, Any]]:
    """Load all open trades mirrored in active_trades_store."""
    if not os.path.exists(NEWS_DB_PATH):
        return []
    try:
        with sqlite3.connect(NEWS_DB_PATH, timeout=30.0) as conn:
            _ensure_active_trades_store(conn)
            rows = conn.execute(
                "SELECT payload FROM active_trades_store ORDER BY updated_at ASC"
            ).fetchall()
        trades: list[dict[str, Any]] = []
        for (payload,) in rows:
            try:
                obj = json.loads(payload) if isinstance(payload, str) else payload
                if isinstance(obj, dict) and obj.get("ticker"):
                    trades.append(obj)
            except (TypeError, json.JSONDecodeError) as e:
                print(f"[Tracker] Skipping corrupt SQLite trade payload: {e}")
        return trades
    except Exception as e:
        print(f"[Tracker] SQLite load of active_trades_store failed: {e}")
        traceback.print_exc()
        return []


def restore_active_trades_from_sqlite_if_empty(
    path: Path | str | None = None,
) -> int:
    """
    If active_trades.json has no open trades, reload from SQLite mirror.

    Returns the number of trades restored (0 if nothing to do).
    """
    store = Path(path) if path else ACTIVE_TRADES_PATH
    existing = load_active_trades(store)
    if existing:
        return 0
    restored = _sqlite_load_all_trades()
    if not restored:
        return 0
    if _write_trades(restored, store):
        print("[Tracker] Restored active trades from SQLite database")
        return len(restored)
    print("[Tracker] WARNING: SQLite restore loaded trades but JSON write failed.")
    return 0


def is_options_session_open(now: datetime | None = None) -> bool:
    """
    True on weekdays between 9:15 AM and 4:15 PM US/Eastern.

    Outside this window (overnight / weekends) 1m option bars are unavailable
    or stale — skip fetches to avoid noisy DATA GAP alerts.
    """
    now_et = (now or datetime.now(timezone.utc)).astimezone(_ET)
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now_et.time()
    return _OPTIONS_SESSION_OPEN <= t <= _OPTIONS_SESSION_CLOSE


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
        "STOP LOSS", "STOP LOSS TRIGGERED",
        "[HOLD]", "[TAKE PROFIT]", "[TRAILING STOP ADJUSTED]", "[EXIT NOW]",
        "[STOP LOSS]", "[STOP LOSS TRIGGERED]",
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
        "[STOP LOSS]": "🛑",
        "[STOP LOSS TRIGGERED]": "🛑",
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
    lines.append(f"\nDashboard: {DASHBOARD_URL}")
    lines.append(f"_UTC {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}_")
    return "\n".join(lines)


# ===========================================================================
# MAIN LOOP
# ===========================================================================

def process_trade(trade: dict[str, Any]) -> None:
    """Fetch micro-data → Gemini decision → notify → update state."""
    ticker = str(trade.get("ticker", "")).upper()
    print(f"\n[Tracker] Evaluating {ticker} "
          f"(id={trade.get('trade_id', 'n/a')})...")

    # Skip 1m option bar polls outside regular session (weekends / overnight)
    if not is_options_session_open():
        print(
            f"[Tracker] [{ticker}] Market closed (outside 9:15–16:15 ET or weekend) "
            f"— skipping 1m bar fetch."
        )
        return

    micro_bars = fetch_micro_bars(ticker, minutes=5)
    if not micro_bars:
        # Still surface a soft alert so silent data outages are visible
        # (only during session hours — off-hours already returned above)
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
        # Virtual paper ledger: credit exit premium * 100, log realized PnL.
        # Runs before remove so the open trade still has entry metadata.
        try:
            entry_px = trade.get("entry_price") or trade.get("entry_premium")
            exit_px = _resolve_exit_price(trade, decision, micro_bars)
            direction = trade.get("direction")
            virtual_broker.paper_sell(trade, exit_px, direction, entry_px)
        except Exception as broker_err:
            print(f"[Tracker] WARNING: virtual paper_sell failed "
                  f"for {ticker}: {broker_err}")
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

    if not is_options_session_open():
        print(
            f"[Tracker] Market closed (outside 9:15–16:15 ET or weekend) — "
            f"skipping 1m option bar fetches for {len(trades)} trade(s)."
        )
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

    # If JSON is empty ([] / {}), reload open trades from SQLite durable mirror
    try:
        restore_active_trades_from_sqlite_if_empty()
    except Exception as e:
        print(f"[Tracker] SQLite restore check failed: {e}")
        traceback.print_exc()

    # Boot heartbeat — confirms webhook connectivity even with 0 open trades
    try:
        trade_count = len(load_active_trades())
    except Exception:
        trade_count = 0
    boot_msg = (
        "Tracker Agent Online - Monitoring Active Trades"
        f"\nState file: `{ACTIVE_TRADES_PATH}`"
        f"\nOpen positions at boot: **{trade_count}**"
        f"\nDashboard: {DASHBOARD_URL}"
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
