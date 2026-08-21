"""
config.py — Central configuration for the hedge fund bot.

Single source of truth for:
  * Ticker universe
  * Database paths (news_room.db for news memory, hedge_fund.db for telemetry)
  * API keys  (ENV-ONLY. Hardcoded fallback keys were removed deliberately —
               the old keys were committed to source and must be rotated.)
  * Scoring knobs: T+S model in scoring_engine.py (env-tunable).
    Additive 30/40/30 pillar weights are RETIRED.
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
# Scoring threshold + calibrated conviction model (env-tunable)
# Live scoring: clamp(T(0..TECH_CEIL)+S(-SENT_MAX..+SENT_MAX), 0, 100)
# Liquidity is a Part C contract reject (MAX_CONTRACT_SPREAD_PCT), not a score term.
# Additive 30/40/30 pillar weights are RETIRED — load_weights/save_weights
# remain as no-op-compatible stubs so saturday_audit does not crash.
# ------------------------------------------------------------------
EXECUTE_THRESHOLD = 70  # never change without explicit mandate

# Schema-compat only; not used by score_ticker.
DEFAULT_WEIGHTS = {"liquidity": 0, "technical": 85, "sentiment": 15}


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
# Legacy: cooldown from last entry (still applied). Prefer post-exit for churn.
GATE_REENTRY_COOLDOWN_MINUTES = _env_int("GATE_REENTRY_COOLDOWN_MINUTES", 45)
# Primary anti-churn: no re-admit until N minutes after last real EXIT.
GATE_POST_EXIT_COOLDOWN_MINUTES = _env_int("GATE_POST_EXIT_COOLDOWN_MINUTES", 45)

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
#   EXIT_INTERVAL_SECONDS       default 300  (5 min) — loop tick
FULL_SCAN_INTERVAL_SECONDS = _env_int("FULL_SCAN_INTERVAL_SECONDS", 1800)
EXIT_INTERVAL_SECONDS = _env_int("EXIT_INTERVAL_SECONDS", 300)
# Consecutive failed option marks before Discord CRITICAL (stop not checked).
MARK_FAIL_ALERT_STREAK = _env_int("MARK_FAIL_ALERT_STREAK", 2)
EXIT_BREAKEVEN_PEAK_PCT = _env_float("EXIT_BREAKEVEN_PEAK_PCT", 25.0)
EXIT_TRAIL_PEAK_PCT = _env_float("EXIT_TRAIL_PEAK_PCT", 40.0)
EXIT_TRAIL_GIVEBACK_FRAC = _env_float("EXIT_TRAIL_GIVEBACK_FRAC", 0.30)
EXIT_TIME_STOP_MINUTES = _env_int("EXIT_TIME_STOP_MINUTES", 90)
EXIT_TIME_STOP_PNL_ABS_PCT = _env_float("EXIT_TIME_STOP_PNL_ABS_PCT", 10.0)
# Skip TIME_STOP when live (or last) underlying score is still this strong.
TIME_STOP_SCORE_EXEMPT = _env_float("TIME_STOP_SCORE_EXEMPT", 80.0)
# Close a high-conviction entry when the live score collapses, regardless of P&L.
# Hysteresis: only if entry_score >= EXECUTE_THRESHOLD (70), so marginal
# entries cannot be thesis-voided. Fires in carry review and every exit pass.
THESIS_EXIT_SCORE = _env_float("THESIS_EXIT_SCORE", 55.0)
# First full score/admit of the Chicago session. 08:30 CDT is the ET open;
# Yahoo chains are empty then (no_liq_data on the whole universe). Carry
# review + exits still run on the 08:30 tick via the exit-only path.
FIRST_FULL_SCAN_HOUR, FIRST_FULL_SCAN_MINUTE = _env_hhmm(
    "FIRST_FULL_SCAN_CDT", "08:45"
)

# ------------------------------------------------------------------
# Risk-based position sizing (paper). Env-overridable; restart to apply.
#   ACCOUNT_SIZE is the single account ceiling: risk formula AND ledger seed.
#   STARTING_BUYING_POWER defaults to the same value; if you override it and
#   it disagrees, boot logs CRITICAL (silent drift is how a week gets lost).
#   contracts = floor(ACCOUNT_SIZE * RISK_PER_TRADE_PCT/100 / ((entry-SL)*100))
#   1-lot risk > RISK_PER_TRADE_DOLLARS * MAX_RISK_BREACH_PCT → reject (walk
#   to next candidate). Cap MAX_CONTRACTS_PER_TRADE. Buying power is the
#   hard block (size down; log bp_limited(n->m)).
# ------------------------------------------------------------------
ACCOUNT_SIZE = _env_float("ACCOUNT_SIZE", 10000.0)
RISK_PER_TRADE_PCT = _env_float("RISK_PER_TRADE_PCT", 1.5)
MAX_CONTRACTS_PER_TRADE = _env_int("MAX_CONTRACTS_PER_TRADE", 10)
# 1-lot stop risk may not exceed RISK_PER_TRADE_DOLLARS * this. Default 1.0
# = no overshoot: a $200 lot on a $150 budget is rejected (walk to next
# candidate), not floored to qty 1. Raise (e.g. 1.2) to allow a small breach.
MAX_RISK_BREACH_PCT = _env_float("MAX_RISK_BREACH_PCT", 1.0)
# Ledger seed. Unset → ACCOUNT_SIZE. Set only if you intentionally want them
# to differ (will CRITICAL at boot).
STARTING_BUYING_POWER = (
    _env_float("STARTING_BUYING_POWER", ACCOUNT_SIZE)
    if os.environ.get("STARTING_BUYING_POWER", "").strip()
    else ACCOUNT_SIZE
)

# ------------------------------------------------------------------
# Stage 4 Part C — entry filters (strike_selector). Env-overridable.
#   MIN_DTE                     default 1   (calendar days; primary 0DTE kill)
#   MAX_EXPIRY_CALENDAR_DTE     default 10  (how far Yahoo chains we load)
#   REQUIRED_MOVE_ATR_K         default 0.5 (reject if need/(ATR*√dte) > k)
#   EXIT_MAX_DECAY_DENSITY      default 8.0 (% extrinsic per RTH hour to expiry)
#   MIN_EXTRINSIC_PCT           default 10  (reject deep-ITM / synthetic stock)
#   MAX_CONTRACT_SPREAD_PCT     default 8.0 (hard reject on chosen contract)
# ------------------------------------------------------------------
MIN_DTE = _env_int("MIN_DTE", 1)
MAX_EXPIRY_CALENDAR_DTE = _env_int("MAX_EXPIRY_CALENDAR_DTE", 10)
REQUIRED_MOVE_ATR_K = _env_float("REQUIRED_MOVE_ATR_K", 0.5)
EXIT_MAX_DECAY_DENSITY = _env_float("EXIT_MAX_DECAY_DENSITY", 8.0)
MIN_EXTRINSIC_PCT = _env_float("MIN_EXTRINSIC_PCT", 10.0)
MAX_CONTRACT_SPREAD_PCT = _env_float("MAX_CONTRACT_SPREAD_PCT", 8.0)

# ------------------------------------------------------------------
# Calibrated scoring (scoring_engine). All env-tunable; restart to apply.
#   Defaults: compromise A retuned 2026-08-10 for Mon/Fri two-sided test.
#   PIVOT_SCALE=0.40 ATR, PIVOT_POWER=1.0, MOM_SCALE=0.45 %,
#   W_PIVOT=0.70 / W_MOM=0.30, TECH_CEIL=85, SENT_MAX=15,
#   DEAD_ZONE_ATR=0.30 (hard PASS when |spot−pivot|/ATR below this).
# ------------------------------------------------------------------
PIVOT_SCALE = _env_float("PIVOT_SCALE", 0.40)
PIVOT_POWER = _env_float("PIVOT_POWER", 1.0)
MOM_SCALE = _env_float("MOM_SCALE", 0.45)
W_PIVOT = _env_float("W_PIVOT", 0.70)
W_MOM = _env_float("W_MOM", 0.30)
TECH_CEIL = _env_float("TECH_CEIL", 85.0)
SENT_MAX = _env_float("SENT_MAX", 15.0)
DEAD_ZONE_ATR = _env_float("DEAD_ZONE_ATR", 0.30)


def risk_per_trade_dollars() -> float:
    """ACCOUNT_SIZE * RISK_PER_TRADE_PCT / 100. The 1-lot risk ceiling."""
    return float(ACCOUNT_SIZE) * float(RISK_PER_TRADE_PCT) / 100.0


def max_one_lot_risk_dollars() -> float:
    """Reject a contract when (entry-SL)*100 exceeds this (qty-1 floor is gone)."""
    return risk_per_trade_dollars() * float(MAX_RISK_BREACH_PCT)


def log_scoring_config() -> None:
    """Boot log: resolved scoring knobs next to [Gate] / [EntryFilters]."""
    print(
        f"[Scoring] thr={EXECUTE_THRESHOLD} "
        f"PIVOT_SCALE={PIVOT_SCALE} PIVOT_POWER={PIVOT_POWER} "
        f"MOM_SCALE={MOM_SCALE} W_PIVOT={W_PIVOT} W_MOM={W_MOM} "
        f"TECH_CEIL={TECH_CEIL} SENT_MAX={SENT_MAX} "
        f"DEAD_ZONE_ATR={DEAD_ZONE_ATR} "
        f"score=T+S (no liq_mult) "
        f"(env-tunable; restart process to apply)"
    )


def log_risk_config() -> None:
    """Boot log: thesis-void + risk sizing + first full-scan clock.

    CRITICAL if the ledger seed disagrees with ACCOUNT_SIZE — that mismatch
    is how the risk model sizes for $10k while the book funds 4× that.
    """
    seed = float(STARTING_BUYING_POWER)
    print(
        f"[Risk] ACCOUNT_SIZE={ACCOUNT_SIZE:g} "
        f"ledger_seed={seed:g} "
        f"RISK_PER_TRADE_PCT={RISK_PER_TRADE_PCT:g} "
        f"MAX_CONTRACTS_PER_TRADE={MAX_CONTRACTS_PER_TRADE} "
        f"RISK_PER_TRADE_DOLLARS={risk_per_trade_dollars():.0f} "
        f"MAX_RISK_BREACH_PCT={MAX_RISK_BREACH_PCT:g} "
        f"(1-lot cap ${max_one_lot_risk_dollars():.0f}) "
        f"THESIS_EXIT_SCORE={THESIS_EXIT_SCORE:g} "
        f"(void if live<{THESIS_EXIT_SCORE:g} and entry_score>={EXECUTE_THRESHOLD}) "
        f"FIRST_FULL_SCAN_CDT="
        f"{FIRST_FULL_SCAN_HOUR:02d}:{FIRST_FULL_SCAN_MINUTE:02d}"
    )
    if abs(seed - float(ACCOUNT_SIZE)) > 0.5:
        msg = (
            f"🚨 **CRITICAL: ACCOUNT_SIZE ${ACCOUNT_SIZE:,.0f} != "
            f"ledger seed ${seed:,.0f}** — risk model and buying power "
            f"disagree. Set STARTING_BUYING_POWER unset (or equal) so they "
            f"are one ceiling."
        )
        print(f"[Risk] {msg}")
        try:
            import broadcaster
            broadcaster.send_discord_alert(msg)
        except Exception as e:
            print(f"[Risk] CRITICAL Discord warn: {e}")


def _init_weights_table():
    """Deprecated table kept so old DBs do not error on accidental reads."""
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
    """DEPRECATED. Returns documentation-only caps; scoring_engine ignores this."""
    return dict(DEFAULT_WEIGHTS)


def save_weights(weights):
    """DEPRECATED no-op. Additive weight writes no longer affect live scoring.

    Still validates shape so saturday_audit can call it without crashing, then
    discards the payload. Does not write to the DB as active scoring input.
    """
    if weights and set(weights.keys()) != set(DEFAULT_WEIGHTS.keys()):
        print(
            f"[Config] save_weights ignored (retired scheme); "
            f"unexpected keys {sorted(weights.keys())}."
        )
    else:
        print(
            f"[Config] save_weights DEPRECATED — ignored {weights!r}. "
            "Live scoring uses scoring_engine T+S, not pillar weights."
        )
    return None
