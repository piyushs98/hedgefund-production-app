"""
main.py — Render entrypoint for Master Bot only.

What this process does:
  1. Starts master_bot.run_macro_loop() once (daemon thread):
       • Pre-market CoS briefing via Gemini → Discord (~09:15–09:29 ET)
       • 30-min scans (RTH): single Discord table + KEY TELEMETRY (DeepSeek)
       • Midday Macro & News once/day @ 11:00 AM CDT only (Gemini; isolated)
  2. Serves a minimal Flask app so Render health checks stay green.

What this process does NOT do:
  • Dashboard UI / portfolio REST APIs
  • Tracker (micro) agent loop

Serve with (Procfile):
  gunicorn main:app --workers 1 --threads 8 --bind 0.0.0.0:$PORT --timeout 120

--workers MUST stay 1 so Master Bot starts exactly once per deploy.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone
from threading import Lock, Thread

from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MACRO_RESTART_SLEEP = int(os.environ.get("MACRO_RESTART_SLEEP", "60"))

_init_lock = Lock()
_background_loops_started = False
_process_prepared = False

app = Flask(__name__)


# ===========================================================================
# MASTER BOT (only background worker)
# ===========================================================================

def _macro_worker() -> None:
    """Immortal wrapper: restart master_bot after any fatal escape."""
    import master_bot

    while True:
        try:
            print("[main] Starting Master Bot (master_bot.run_macro_loop)...")
            master_bot.run_macro_loop()
            # Normal exit only in BYPASS_MARKET_HOURS one-shot mode
            print("[main] Master Bot loop exited cleanly; not restarting.")
            return
        except Exception as e:
            print(f"[main] Master Bot error: {e}")
            traceback.print_exc()
            print(
                f"[main] Master Bot sleeping {MACRO_RESTART_SLEEP}s before restart..."
            )
            time.sleep(MACRO_RESTART_SLEEP)


def start_master_bot() -> None:
    """Start Master Bot exactly once per OS process (gunicorn workers=1)."""
    global _background_loops_started
    with _init_lock:
        if _background_loops_started:
            print(
                "[main] Master Bot already started in this process — "
                "skipping (duplicate-bot guard)."
            )
            return
        _background_loops_started = True
        macro = Thread(
            target=_macro_worker,
            name="macro-master-bot",
            daemon=True,
        )
        macro.start()
        print(f"[main] Master Bot daemon started exactly once: {macro.name}")


# ===========================================================================
# MINIMAL HTTP (Render health only — no dashboard)
# ===========================================================================

@app.route("/")
@app.route("/health")
def health():
    """Render / ops liveness — always cheap."""
    return "OK", 200


@app.route("/status")
@app.route("/api/status")
def status():
    """Tiny JSON heartbeat (no portfolio / trades / UI)."""
    return jsonify({
        "status": "live",
        "service": "master_bot",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ===========================================================================
# PROCESS BOOTSTRAP (gunicorn import)
# ===========================================================================

def prepare_process() -> None:
    """One-shot init for the gunicorn worker: ledger + Master Bot only."""
    global _process_prepared

    with _init_lock:
        already = _process_prepared
        if not already:
            _process_prepared = True

    if already:
        start_master_bot()
        return

    print("\n=== MASTER BOT ONLY (main.py) — no dashboard, no tracker ===")

    # Paper ledger used by master_bot EXECUTE path (not for a web UI).
    try:
        import virtual_broker
        virtual_broker.ensure_ledger()
        print("[main] Virtual broker ledger ready (paper trades)")
    except Exception as e:
        print(f"[main] WARNING: virtual broker init failed: {e}")

    start_master_bot()
    port = os.environ.get("PORT", "10000")
    print(
        f"[main] Process ready — Master Bot + health on PORT={port} "
        f"(gunicorn workers=1 required)"
    )


# Gunicorn loads main:app with __name__ == "main" → bootstrap daemons on import.
if __name__ != "__main__":
    prepare_process()


if __name__ == "__main__":
    port = os.environ.get("PORT", "10000")
    print(
        "[main] Refusing app.run(). Start production WSGI:\n"
        f"  gunicorn main:app --workers 1 --threads 8 "
        f"--bind 0.0.0.0:{port} --timeout 120"
    )
    raise SystemExit(
        "Use gunicorn (see Procfile). Flask app.run() is not the entrypoint."
    )
