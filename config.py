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
