"""
main.py — Render master orchestrator (July 20 lightweight baseline).

Single process that:
  1. Serves a static-first Flask health + Virtual Hedge Fund dashboard/API.
  2. Runs the macro CEO loop (master_bot, ~30-min trading cadence) on a daemon thread.
  3. Micro tracker loop (tracker_agent) is intentionally NOT started here.

Design constraints (100% market-hours uptime):
  * No Server-Sent Events (/stream), no long-polling, no 1s UI churn.
  * Dashboard uses short REST fetches only; clients poll every ~30s.
  * --workers MUST stay 1 so Master Bot starts exactly once per deploy.

The Master Bot thread may hit network drops or API throttling; failures are
logged and the thread backs off without taking down this process.

Serve with:
  gunicorn main:app --workers 1 --threads 8 --bind 0.0.0.0:$PORT --timeout 120
"""

from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread

from flask import Flask, jsonify, render_template

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

# Exactly-once guard for process bootstrap + macro/micro daemon threads
# (must survive concurrent prepare_process / start_background_loops under gthread).
_init_lock = Lock()
_background_loops_started = False
_process_prepared = False

app = Flask(__name__)


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
    Launch Master Bot (macro loop) as a daemon thread (dies with the process).

    Thread-safe and idempotent: under gunicorn gthread, only the first caller
    in this process starts the bot. Subsequent calls are no-ops so we never
    double-spawn Master Bot (duplicate paper trades / Discord).

    Tracker (micro) thread is intentionally not started from main.py.

    NOTE: This is per-process. --workers MUST remain 1; a second OS worker
    would import this module independently and start its own Master Bot.
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
        # cannot race another daemon into existence.
        _background_loops_started = True
        macro = Thread(
            target=_macro_worker,
            name="macro-master-bot",
            daemon=True,
        )
        # Micro tracker intentionally disabled — main.py boots Master Bot only.
        # micro = Thread(
        #     target=_micro_worker,
        #     name="micro-tracker-agent",
        #     daemon=True,
        # )
        macro.start()
        # micro.start()
        print(
            "[main] Background daemon thread started exactly once: "
            f"{macro.name} (tracker_agent not started)"
        )


# ===========================================================================
# WEB SERVICE (static-first dashboard + Render health checks)
# ===========================================================================
# Keep this surface tiny. Heavy SSE / 1s polling starved gunicorn
# (--workers 1 --threads 8) and caused 502s under Render health checks.

@app.route("/")
def index():
    """Lightweight Virtual Hedge Fund dashboard (client polls REST every 30s)."""
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


@app.route("/health")
def health():
    """Render health check — must stay cheap and always succeed if process is up."""
    return "OK"


@app.route("/status")
def status():
    """Optional ops endpoint: active trade count + state file path (best-effort)."""
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
# re-imports this module and would start its own Master Bot → duplicate
# trades. The in-process Lock only prevents double-init *within* one worker.
#
# Do NOT add --preload: the master would start daemon threads, then fork;
# children inherit _background_loops_started=True but not live threads → no bots.
#
# --threads 8 keeps /health answering while a slow REST request holds a thread.
# --timeout 120 covers long portfolio scans / LLM calls on daemon threads
# without killing the worker mid-cycle.

def prepare_process() -> None:
    """
    One-shot process init for the gunicorn worker.

    Ledger/files init is idempotent; Master Bot daemon is started exactly
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

    print("\n=== RENDER ORCHESTRATOR (main.py) — July 20 lightweight baseline ===")
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
        f"(workers=1 required) — PORT={port} — no SSE"
    )


# Gunicorn loads `main:app` with __name__ == "main" (not "__main__"), so the
# single worker bootstraps daemons on import. Skip when `python main.py` is used
# only to print the gunicorn command (avoids spawning bots that immediately die).
if __name__ != "__main__":
    prepare_process()


if __name__ == "__main__":
    # Do not use Flask's development server in production.
    port = os.environ.get("PORT", "10000")
    print(
        "[main] Refusing app.run(). Start the production WSGI server:\n"
        f"  gunicorn main:app --workers 1 --threads 8 "
        f"--bind 0.0.0.0:{port} --timeout 120"
    )
    raise SystemExit(
        "Use gunicorn (see Procfile / message above). "
        "Flask app.run() is not the production entrypoint."
    )
