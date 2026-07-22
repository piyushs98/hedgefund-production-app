"""
main.py — Render master orchestrator.

Single process that:
  1. Serves Flask health + Virtual Hedge Fund dashboard/API under gunicorn.
  2. Runs the macro CEO loop (master_bot, ~30-min trading cadence) on a daemon thread.
  3. Runs the micro tracker loop (tracker_agent, 5-min cadence) on a daemon thread.
  4. Exposes /api/force-briefing for on-demand DeepSeek pre-market Discord tests.

Either background thread may hit network drops or API throttling; failures are
logged and the thread backs off without taking down this process.

Serve with (workers MUST be 1 — see prepare_process):
  gunicorn main:app --workers 1 --threads 8 --bind 0.0.0.0:$PORT --timeout 120
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread

from flask import Flask, Response, jsonify, render_template

import config
import virtual_broker

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
# Absolute default next to this file so cwd / Render workdir cannot desync
# master_bot.py and tracker_agent.py (same formula in all three).
ACTIVE_TRADES_PATH = Path(
    os.environ.get(
        "ACTIVE_TRADES_PATH",
        os.path.abspath(
            os.path.join(os.path.dirname(__file__), "active_trades.json")
        ),
    )
)
MACRO_RESTART_SLEEP = int(os.environ.get("MACRO_RESTART_SLEEP", "60"))
MICRO_RESTART_SLEEP = int(os.environ.get("MICRO_RESTART_SLEEP", "30"))

# Process start for /api/telemetry uptime (dashboard-only observability).
START_TIME = time.time()

# Exactly-once guard for process bootstrap + macro/micro daemon threads
# (must survive concurrent prepare_process / start_background_loops under gthread).
# Use a single non-reentrant Lock; never call start_background_loops while holding it
# from a nested path without releasing first.
_init_lock = Lock()
_background_loops_started = False
_process_prepared = False

# Serialize on-demand briefings so two browser hits cannot fire dual DeepSeek
# + Discord storms (DeepSeek call can hold a worker thread up to ~90s).
_force_briefing_lock = Lock()

app = Flask(__name__)


def _db_size_mb(path: str | os.PathLike[str]) -> float | None:
    """Return file size in MB, or None if the path is missing/unreadable."""
    try:
        return round(os.path.getsize(path) / (1024 * 1024), 2)
    except OSError:
        return None


def _format_uptime(seconds: float) -> str:
    """Human-readable uptime, e.g. '2h 14m' or '45m 12s'."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    if not days and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _reason_from_telemetry_row(row: sqlite3.Row) -> str:
    """Best-effort human reason for a vetoed/skipped scan."""
    # Prefer explicit reasons list from scoring
    raw_reasons = row["reasons_json"] if "reasons_json" in row.keys() else None
    if raw_reasons:
        try:
            parsed = json.loads(raw_reasons)
            if isinstance(parsed, list) and parsed:
                return "; ".join(str(x) for x in parsed[:4])
            if isinstance(parsed, str) and parsed.strip():
                return parsed.strip()
            if isinstance(parsed, dict) and parsed:
                return "; ".join(f"{k}: {v}" for k, v in list(parsed.items())[:4])
        except (TypeError, json.JSONDecodeError):
            if isinstance(raw_reasons, str) and raw_reasons.strip():
                return raw_reasons.strip()[:240]

    # Adversarial block detail as fallback
    raw_adv = row["adversarial_json"] if "adversarial_json" in row.keys() else None
    if raw_adv:
        try:
            adv = json.loads(raw_adv)
            if isinstance(adv, dict):
                for key in ("reason", "verdict", "summary", "message"):
                    if adv.get(key):
                        return str(adv[key])
                if adv.get("blocked") or adv.get("veto"):
                    return "Adversarial veto"
        except (TypeError, json.JSONDecodeError):
            pass

    flag = row["action_flag"] if "action_flag" in row.keys() else None
    score = row["total_score"] if "total_score" in row.keys() else None
    if flag and score is not None:
        return f"{flag} (score {score})"
    if flag:
        return str(flag)
    return "Vetoed / skipped"


# ===========================================================================
# STATE INITIALIZATION
# ===========================================================================

def ensure_active_trades_file(path: Path | None = None) -> None:
    """
    Create active_trades.json as {} if missing.

    Tracker load_active_trades treats empty {} as zero open positions, so the
    micro loop never crashes on a cold Render filesystem.
    """
    store = path or ACTIVE_TRADES_PATH
    try:
        if store.exists():
            return
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{}\n", encoding="utf-8")
        print(f"[main] Initialized empty state file at {store.resolve()}")
    except OSError as e:
        # Non-fatal: tracker will also attempt a seed / return []
        print(f"[main] WARNING: could not initialize {store}: {e}")


def _load_active_trades_raw() -> list | dict:
    """Best-effort parse of active_trades.json for API responses."""
    if not ACTIVE_TRADES_PATH.exists():
        return []
    try:
        raw = json.loads(ACTIVE_TRADES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if not raw:
            return []
        if "trades" in raw and isinstance(raw["trades"], list):
            return raw["trades"]
        if raw.get("ticker"):
            return [raw]
        return []
    return []


# ===========================================================================
# BACKGROUND WORKERS (crash-isolated)
# ===========================================================================

def _macro_worker() -> None:
    """Wrap master_bot.run_macro_loop so a fatal escape restarts after sleep."""
    # Local import keeps Flask boot fast and avoids import-time side effects
    import master_bot

    while True:
        try:
            print("[main] Starting macro loop (master_bot.run_macro_loop)...")
            master_bot.run_macro_loop()
            # Normal exit only in BYPASS_MARKET_HOURS one-shot mode
            print("[main] Macro loop exited cleanly; not restarting.")
            return
        except Exception as e:
            print(f"[main] Macro thread error (network/API/other): {e}")
            traceback.print_exc()
            print(f"[main] Macro thread sleeping {MACRO_RESTART_SLEEP}s before restart...")
            time.sleep(MACRO_RESTART_SLEEP)


def _micro_worker() -> None:
    """Wrap tracker_agent.run_micro_loop so a fatal escape restarts after sleep."""
    import tracker_agent

    while True:
        try:
            print("[main] Starting micro loop (tracker_agent.run_micro_loop)...")
            tracker_agent.run_micro_loop()
            print("[main] Micro loop exited unexpectedly; restarting after backoff...")
            time.sleep(MICRO_RESTART_SLEEP)
        except Exception as e:
            print(f"[main] Micro thread error (network/API/other): {e}")
            traceback.print_exc()
            print(f"[main] Micro thread sleeping {MICRO_RESTART_SLEEP}s before restart...")
            time.sleep(MICRO_RESTART_SLEEP)


def start_background_loops() -> None:
    """
    Launch macro + micro agents as daemon threads (die with the process).

    Thread-safe and idempotent: under gunicorn gthread, only the first caller
    in this process starts the bots. Subsequent calls are no-ops so we never
    double-spawn Master Bot / Tracker (duplicate paper trades / Discord).

    NOTE: This is per-process. --workers MUST remain 1; a second OS worker
    would import this module independently and start its own pair of bots.
    """
    global _background_loops_started
    with _init_lock:
        if _background_loops_started:
            print(
                "[main] Background loops already started in this process — "
                "skipping re-init (duplicate-bot guard)."
            )
            return
        # Set flag BEFORE .start() so a re-entrant call during thread bootstrap
        # cannot race another pair of daemons into existence.
        _background_loops_started = True
        macro = Thread(
            target=_macro_worker,
            name="macro-master-bot",
            daemon=True,
        )
        micro = Thread(
            target=_micro_worker,
            name="micro-tracker-agent",
            daemon=True,
        )
        macro.start()
        micro.start()
        print(
            "[main] Background daemon threads started exactly once: "
            f"{macro.name}, {micro.name}"
        )


# ===========================================================================
# WEB SERVICE (dashboard + Render health checks)
# ===========================================================================

@app.route("/")
def index():
    """Mobile-first Virtual Hedge Fund dashboard."""
    return render_template("index.html")


@app.route("/api/portfolio")
def api_portfolio():
    """Buying power + realized PnL from the virtual SQLite ledger."""
    try:
        portfolio = virtual_broker.get_portfolio()
        return jsonify(portfolio)
    except Exception as e:
        return jsonify({"error": str(e), "buying_power": None, "total_realized_pnl": None}), 500


@app.route("/api/active_trades")
def api_active_trades():
    """Open positions from active_trades.json."""
    try:
        trades = _load_active_trades_raw()
        return jsonify(trades)
    except Exception as e:
        return jsonify({"error": str(e), "trades": []}), 500


@app.route("/api/status")
def api_status():
    """Live heartbeat for the dashboard status pill."""
    return jsonify({
        "status": "live",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/graveyard")
def api_graveyard():
    """
    Read-only: last 5 non-EXECUTE scans from backtest_telemetry (vetoes / skips).

    Observability only — does not affect trading, scoring, or scrapers.
    """
    db_path = config.HEDGE_DB_PATH
    try:
        if not os.path.exists(db_path):
            return jsonify([])

        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    id,
                    timestamp,
                    ticker,
                    action_flag,
                    total_score,
                    adversarial_penalty,
                    reasons_json,
                    adversarial_json
                FROM backtest_telemetry
                WHERE action_flag IS NOT NULL
                  AND UPPER(TRIM(action_flag)) != 'EXECUTE'
                ORDER BY timestamp DESC, id DESC
                LIMIT 5
                """
            ).fetchall()

        out = []
        for row in rows:
            out.append({
                "id": row["id"],
                "timestamp": row["timestamp"],
                "ticker": row["ticker"] or "?",
                "action": row["action_flag"],
                "action_flag": row["action_flag"],
                "total_score": row["total_score"],
                "adversarial_penalty": row["adversarial_penalty"],
                "reason": _reason_from_telemetry_row(row),
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


@app.route("/api/telemetry")
def api_telemetry():
    """
    Lightweight system health for the Jarvis footer.

    Returns process uptime and SQLite file sizes (MB). Read-only paths only.
    """
    uptime_s = max(0.0, time.time() - START_TIME)
    news_mb = _db_size_mb(config.NEWS_DB_PATH)
    hedge_mb = _db_size_mb(config.HEDGE_DB_PATH)
    return jsonify({
        "uptime_seconds": int(uptime_s),
        "uptime": _format_uptime(uptime_s),
        "start_time": datetime.fromtimestamp(START_TIME, tz=timezone.utc).isoformat(),
        "news_db_mb": news_mb,
        "hedge_db_mb": hedge_mb,
        "news_db_path": str(config.NEWS_DB_PATH),
        "hedge_db_path": str(config.HEDGE_DB_PATH),
    })


@app.route("/stream")
def stream():
    """
    Server-Sent Events stream for the Jarvis HUD.

    Polls virtual_broker.ui_event_queue non-blockingly; yields EXECUTE/CLOSE
    events as they are emitted by paper_buy / paper_sell. Sleeps 1s when idle
    to avoid CPU thrashing. Does not touch trading logic.
    """

    def event_generator():
        while True:
            event = virtual_broker.get_ui_event()
            if event is not None:
                payload = json.dumps(event, default=str)
                yield f"data: {payload}\n\n"
            else:
                # Keep connection warm for proxies without flooding the client
                yield ": keepalive\n\n"
                time.sleep(1)

    return Response(
        event_generator(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/health")
def health():
    """Render health check — must stay cheap and always succeed if process is up."""
    return "OK"


@app.route("/api/force-briefing")
def api_force_briefing():
    """
    Hidden ops endpoint: run the DeepSeek pre-market briefing + Discord
    webhook immediately, regardless of EST clock / prep-meeting window.

    Same pipeline as the scheduled 09:15 EST prep meeting
    (pre_market_meeting.force_pre_market_briefing).

    Holds one gthread worker thread until DeepSeek (≤90s timeout) + Discord
    finish so the JSON body reflects real outcomes — not a fire-and-forget.
    Concurrent hits return 409 rather than stacking dual DeepSeek calls.
    """
    # Non-blocking acquire: a second tab must not pile another 90s DeepSeek
    # call onto an already-busy worker pool (health-check starvation risk).
    if not _force_briefing_lock.acquire(blocking=False):
        return jsonify({
            "status": "error",
            "error": "Briefing already in progress; retry shortly.",
        }), 409

    try:
        # Local import: keep cold import light; avoid trading-stack side effects
        from pre_market_meeting import force_pre_market_briefing

        print("[main] /api/force-briefing hit — running on-demand CoS briefing...")
        meta = force_pre_market_briefing()

        deepseek_ok = bool(meta.get("deepseek_ok"))
        discord_ok = bool(meta.get("discord_delivered"))
        source = meta.get("source") or "fallback"
        deepseek_error = meta.get("deepseek_error")

        # Full success: DeepSeek synthesized AND Discord accepted the webhook.
        if deepseek_ok and discord_ok:
            return jsonify({"status": "Briefing forced and sent"})

        # Partial / failed — still useful for ops diagnosis; do not claim "sent"
        # if Discord never delivered, and surface DeepSeek timeout/fallback.
        payload = {
            "status": "error",
            "deepseek_ok": deepseek_ok,
            "deepseek_error": deepseek_error,
            "discord_delivered": discord_ok,
            "source": source,
        }
        if discord_ok and not deepseek_ok:
            payload["status"] = "Briefing forced and sent (DeepSeek fallback)"
            payload["warning"] = (
                "Discord delivered a DB fallback briefing; DeepSeek did not succeed. "
                f"detail={deepseek_error}"
            )
            # 200 with warning: webhook path verified; model path needs attention
            return jsonify(payload), 200

        if deepseek_ok and not discord_ok:
            payload["error"] = (
                "DeepSeek OK but Discord webhook failed "
                "(check DISCORD_WEBHOOK / rate limits)."
            )
            return jsonify(payload), 502

        payload["error"] = (
            "DeepSeek failed and Discord did not deliver. "
            f"deepseek={deepseek_error}"
        )
        return jsonify(payload), 502

    except Exception as e:
        # Unexpected hard failures (import errors, programming bugs). DeepSeek
        # HTTP timeouts are handled inside generate_morning_briefing and do not
        # land here — they become deepseek_ok=False + fallback text above.
        print(f"[main] /api/force-briefing FAILED: {e}")
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "error": str(e),
        }), 500
    finally:
        _force_briefing_lock.release()


@app.route("/status")
def status():
    """Optional ops endpoint: active trade count + thread names (best-effort)."""
    trade_count = 0
    try:
        if ACTIVE_TRADES_PATH.exists():
            raw = json.loads(ACTIVE_TRADES_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                trade_count = len(raw)
            elif isinstance(raw, dict) and "trades" in raw:
                trade_count = len(raw.get("trades") or [])
            elif isinstance(raw, dict) and raw.get("ticker"):
                trade_count = 1
    except Exception:
        trade_count = -1
    return {
        "status": "ok",
        "active_trades": trade_count,
        "state_file": str(ACTIVE_TRADES_PATH),
    }


# ===========================================================================
# PROCESS BOOTSTRAP + WSGI ENTRY (gunicorn)
# ===========================================================================
#
# Production (Render / local):
#   gunicorn main:app --workers 1 --threads 8 --bind 0.0.0.0:$PORT --timeout 120
#
# CRITICAL: --workers MUST stay 1. Each worker is a separate OS process that
# re-imports this module and would start its own macro/micro pair → duplicate
# trades. The in-process Lock only prevents double-init *within* one worker.
#
# Do NOT add --preload: the master would start daemon threads, then fork;
# children inherit _background_loops_started=True but not live threads → no bots.
#
# --threads 8 keeps /health answering while /stream SSE and DeepSeek hold other
# threads (Flask app.run() blocked on SSE → Render 502 + restarts). Residual
# risk: each open /stream tab occupies one thread until disconnect; leave headroom
# for Render health checks (avoid 7+ simultaneous SSE clients on threads=8).
# --timeout 120 covers /api/force-briefing (DeepSeek HTTP timeout is 90s).

def prepare_process() -> None:
    """
    One-shot process init for the gunicorn worker.

    Ledger/files init is idempotent; macro/micro daemons are started exactly
    once per OS process via the locked flag in start_background_loops().
    """
    global _process_prepared

    # Fast path: already bootstrapped (common under repeated imports/tests).
    with _init_lock:
        already = _process_prepared
        if not already:
            _process_prepared = True

    if already:
        # Still enforce exactly-once bots if a partial init ever skipped them.
        start_background_loops()
        return

    print("\n=== RENDER ORCHESTRATOR (main.py) ===")
    ensure_active_trades_file()
    try:
        virtual_broker.ensure_ledger()
        print("[main] Virtual broker ledger ready")
    except Exception as e:
        print(f"[main] WARNING: virtual broker init failed: {e}")

    # Idempotent under _init_lock — safe if two gthreads race prepare_process.
    start_background_loops()
    port = os.environ.get("PORT", "10000")
    print(
        f"[main] Process ready under gunicorn gthread "
        f"(workers=1 required) — PORT={port}"
    )


# Gunicorn loads `main:app` with __name__ == "main" (not "__main__"), so the
# single worker bootstraps daemons on import. Skip when `python main.py` is used
# only to print the gunicorn command (avoids spawning bots that immediately die).
if __name__ != "__main__":
    prepare_process()


if __name__ == "__main__":
    # Do not use Flask's development server in production — it single-threads
    # poorly under SSE (/stream) and reverse proxies return 502 Bad Gateway.
    port = os.environ.get("PORT", "10000")
    print(
        "[main] Refusing app.run(). Start the production WSGI server:\n"
        f"  gunicorn main:app --workers 1 --threads 8 "
        f"--bind 0.0.0.0:{port} --timeout 120"
    )
    raise SystemExit(
        "Use gunicorn (see Procfile / message above). "
        "Flask app.run() was removed to prevent SSE 502s."
    )
