"""
main.py — Render master orchestrator.

Single process that:
  1. Serves a Flask health endpoint + Virtual Hedge Fund dashboard/API.
  2. Runs the macro CEO loop (master_bot, ~30-min trading cadence) on a daemon thread.
  3. Runs the micro tracker loop (tracker_agent, 5-min cadence) on a daemon thread.

Either background thread may hit network drops or API throttling; failures are
logged and the thread backs off without taking down this process.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

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
    """Launch macro + micro agents as daemon threads (die with the process)."""
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
    print("[main] Background daemon threads started: "
          f"{macro.name}, {micro.name}")


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


@app.route("/health")
def health():
    """Render health check — must stay cheap and always succeed if process is up."""
    return "OK"


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
# ENTRYPOINT
# ===========================================================================

def main() -> None:
    print("\n=== RENDER ORCHESTRATOR (main.py) ===")
    ensure_active_trades_file()
    try:
        virtual_broker.ensure_ledger()
        print("[main] Virtual broker ledger ready")
    except Exception as e:
        print(f"[main] WARNING: virtual broker init failed: {e}")
    start_background_loops()

    port = int(os.environ.get("PORT", 10000))
    print(f"[main] Flask binding 0.0.0.0:{port}")
    # threaded=True so /health answers while agents are busy
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
