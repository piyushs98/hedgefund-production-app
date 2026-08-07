"""
config.py — Central configuration for the hedge fund bot.

Single source of truth for:
  * Ticker universe
  * Database paths (news_room.db for news memory, hedge_fund.db for telemetry/weights)
  * API keys  (ENV-ONLY. Hardcoded fallback keys were removed deliberately —
               the old keys were committed to source and must be rotated.)
  * Dynamic scoring weights (persisted, so saturday_audit.py recommendations
    actually feed back into the live scoring engine instead of being ignored)
"""

import os
import json
import sqlite3

# ------------------------------------------------------------------
# Ticker universe
# ------------------------------------------------------------------
TICKERS = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"]
if os.environ.get("TEST_TICKERS"):
    TICKERS = [t.strip() for t in os.environ["TEST_TICKERS"].split(",") if t.strip()]

INDEX_ETFS = {"SPY", "QQQ", "IWM"}

# ------------------------------------------------------------------
# Databases
#   news_room.db   -> headlines / innovation data / positions (existing schema)
#   hedge_fund.db  -> backtest_telemetry + scoring_weights (new, per mandate)
# ------------------------------------------------------------------
NEWS_DB_PATH = os.environ.get("NEWS_DB_PATH", "data/news_room.db")
HEDGE_DB_PATH = os.environ.get("HEDGE_DB_PATH", "data/hedge_fund.db")

# ------------------------------------------------------------------
# Secrets — environment variables ONLY. Fail loudly, never silently
# fall back to a committed key.
#
# LLM policy (llm_chain.generate_text primary=...):
#   * Meetings (pre-market CoS, midday delta meeting): primary="gemini"
#     free-tier (GEMINI_API_KEY / gemini-flash-latest); DeepSeek backup.
#   * Trade scans (CEO/quant/CoS/managers/adversarial/trade notes):
#     primary="deepseek" (DEEPSEEK_API_KEY); Gemini optional backup.
#   * Override models via LLM_GEMINI_MODEL / LLM_DEEPSEEK_MODEL
# ------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

# Public dashboard (Discord alert deep-links). Override via env if the Render
# service name ever changes; default is the production service URL.
DASHBOARD_URL = (
    os.environ.get("DASHBOARD_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or "https://hedgefund-production-app.onrender.com"
).rstrip("/")


def assert_secrets(require_discord=True):
    """Call once at startup. Crashes early with a clear message instead of
    failing 40 minutes into a trading loop."""
    missing = []
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if require_discord and not DISCORD_WEBHOOK:
        missing.append("DISCORD_WEBHOOK")
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Export them before launching (e.g. in the tmux launcher script). "
            "Hardcoded fallback keys were removed for security — the old ones "
            "were committed to source and must be rotated."
        )


# ------------------------------------------------------------------
# Dynamic scoring weights (Task 2 + feedback loop from saturday_audit)
# ------------------------------------------------------------------
DEFAULT_WEIGHTS = {"liquidity": 30, "technical": 40, "sentiment": 30}
EXECUTE_THRESHOLD = 70

# ------------------------------------------------------------------
# Stage 3 signal gate — all knobs are env-overridable (no code change).
# Set on Render / local shell and restart the process to apply.
#   GATE_MAX_ENTRIES_PER_TICKER  default 6   (daily cap per ticker)
#   GATE_MAX_CONCURRENT          default 10  (paper: exits protect the book)
#   GATE_PERSIST_CYCLES          default 2
#   GATE_FLIP_LOCK_MINUTES       default 60
#   GATE_FLIP_OVERRIDE_SCORE     default 85
#   GATE_REENTRY_COOLDOWN_MINUTES default 45
# ------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        print(f"[Config] Invalid {name}={raw!r}; using default {default}")
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        print(f"[Config] Invalid {name}={raw!r}; using default {default}")
        return float(default)


def _env_hhmm(name: str, default: str) -> tuple:
    """Parse HH:MM env into (hour, minute). Falls back to default on bad input."""
    raw = os.environ.get(name)
    text = (raw if raw is not None and str(raw).strip() != "" else default).strip()
    try:
        parts = text.split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("out of range")
        return hour, minute
    except (TypeError, ValueError, IndexError):
        print(f"[Config] Invalid {name}={raw!r}; using default {default}")
        parts = default.split(":")
        return int(parts[0]), int(parts[1])


GATE_MAX_ENTRIES_PER_TICKER = _env_int("GATE_MAX_ENTRIES_PER_TICKER", 6)
GATE_MAX_CONCURRENT = _env_int("GATE_MAX_CONCURRENT", 10)
GATE_PERSIST_CYCLES = _env_int("GATE_PERSIST_CYCLES", 2)
GATE_FLIP_LOCK_MINUTES = _env_int("GATE_FLIP_LOCK_MINUTES", 60)
GATE_FLIP_OVERRIDE_SCORE = _env_float("GATE_FLIP_OVERRIDE_SCORE", 85.0)
GATE_REENTRY_COOLDOWN_MINUTES = _env_int("GATE_REENTRY_COOLDOWN_MINUTES", 45)

# ------------------------------------------------------------------
# Stage 4 deterministic exits (30-min scan path — no tracker / no Gemini).
# All knobs env-overridable; restart process to apply.
#   EXIT_EOD_FLATTEN_CDT          default 14:45  (America/Chicago)
#   CARRY_MIN_DTE                 default 2     (EOD flattens only if cal_dte < this)
#   EXIT_ZERO_DTE_FLATTEN_CDT     default 13:00  (0DTE hard flatten)
#   EXIT_BREAKEVEN_PEAK_PCT       default 25     (B5)
#   EXIT_TRAIL_PEAK_PCT           default 40
#   EXIT_TRAIL_GIVEBACK_FRAC      default 0.30   (close when pnl <= peak*(1-frac))
#   EXIT_TIME_STOP_MINUTES        default 90
#   EXIT_TIME_STOP_PNL_ABS_PCT    default 10
# ------------------------------------------------------------------
EXIT_EOD_FLATTEN_HOUR, EXIT_EOD_FLATTEN_MINUTE = _env_hhmm(
    "EXIT_EOD_FLATTEN_CDT", "14:45"
)
CARRY_MIN_DTE = _env_int("CARRY_MIN_DTE", 2)
EXIT_ZERO_DTE_FLATTEN_HOUR, EXIT_ZERO_DTE_FLATTEN_MINUTE = _env_hhmm(
    "EXIT_ZERO_DTE_FLATTEN_CDT", "13:00"
)
# Split cadence: full score/admit scan vs exit-only mark pass.
#   FULL_SCAN_INTERVAL_SECONDS  default 1800 (30 min)
#   EXIT_INTERVAL_SECONDS       default 900  (15 min) — loop tick; full scan every other tick
FULL_SCAN_INTERVAL_SECONDS = _env_int("FULL_SCAN_INTERVAL_SECONDS", 1800)
EXIT_INTERVAL_SECONDS = _env_int("EXIT_INTERVAL_SECONDS", 900)
# Consecutive failed option marks before Discord CRITICAL (stop not checked).
MARK_FAIL_ALERT_STREAK = _env_int("MARK_FAIL_ALERT_STREAK", 2)
EXIT_BREAKEVEN_PEAK_PCT = _env_float("EXIT_BREAKEVEN_PEAK_PCT", 25.0)
EXIT_TRAIL_PEAK_PCT = _env_float("EXIT_TRAIL_PEAK_PCT", 40.0)
EXIT_TRAIL_GIVEBACK_FRAC = _env_float("EXIT_TRAIL_GIVEBACK_FRAC", 0.30)
EXIT_TIME_STOP_MINUTES = _env_int("EXIT_TIME_STOP_MINUTES", 90)
EXIT_TIME_STOP_PNL_ABS_PCT = _env_float("EXIT_TIME_STOP_PNL_ABS_PCT", 10.0)

# ------------------------------------------------------------------
# Stage 4 Part C — entry filters (strike_selector). Env-overridable.
#   MIN_DTE                     default 1   (calendar days; primary 0DTE kill)
#   MAX_EXPIRY_CALENDAR_DTE     default 10  (how far Yahoo chains we load)
#   REQUIRED_MOVE_ATR_K         default 0.5 (reject if need/(ATR*√dte) > k)
#   EXIT_MAX_DECAY_DENSITY      default 8.0 (% extrinsic per RTH hour to expiry)
# ------------------------------------------------------------------
MIN_DTE = _env_int("MIN_DTE", 1)
MAX_EXPIRY_CALENDAR_DTE = _env_int("MAX_EXPIRY_CALENDAR_DTE", 10)
REQUIRED_MOVE_ATR_K = _env_float("REQUIRED_MOVE_ATR_K", 0.5)
EXIT_MAX_DECAY_DENSITY = _env_float("EXIT_MAX_DECAY_DENSITY", 8.0)


def _init_weights_table():
    os.makedirs(os.path.dirname(HEDGE_DB_PATH), exist_ok=True)
    with sqlite3.connect(HEDGE_DB_PATH, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scoring_weights (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                weights_json TEXT NOT NULL
            )
        """)
        conn.commit()


def load_weights():
    """Returns the current pillar weights, validated to sum to 100.
    Falls back to DEFAULT_WEIGHTS if the table is empty or corrupt."""
    try:
        _init_weights_table()
        with sqlite3.connect(HEDGE_DB_PATH, timeout=30.0) as conn:
            row = conn.execute(
                "SELECT weights_json FROM scoring_weights WHERE id = 1"
            ).fetchone()
        if row:
            w = json.loads(row[0])
            if (set(w.keys()) == set(DEFAULT_WEIGHTS.keys())
                    and all(isinstance(v, (int, float)) and v >= 0 for v in w.values())
                    and abs(sum(w.values()) - 100) < 0.01):
                return {k: float(v) for k, v in w.items()}
            print("[Config] Stored weights invalid; using defaults.")
    except Exception as e:
        print(f"[Config] Could not load weights ({e}); using defaults.")
    return dict(DEFAULT_WEIGHTS)


def save_weights(weights):
    """Persist new pillar weights (called by saturday_audit). Validates sum=100."""
    if set(weights.keys()) != set(DEFAULT_WEIGHTS.keys()):
        raise ValueError(f"Weights must have keys {sorted(DEFAULT_WEIGHTS)}")
    if abs(sum(weights.values()) - 100) > 0.01:
        raise ValueError(f"Weights must sum to 100, got {sum(weights.values())}")
    _init_weights_table()
    with sqlite3.connect(HEDGE_DB_PATH, timeout=30.0) as conn:
        conn.execute(
            """INSERT INTO scoring_weights (id, weights_json, updated_at)
               VALUES (1, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                   weights_json = excluded.weights_json,
                   updated_at = CURRENT_TIMESTAMP""",
            (json.dumps(weights),),
        )
        conn.commit()
    print(f"[Config] Scoring weights persisted: {weights}")
