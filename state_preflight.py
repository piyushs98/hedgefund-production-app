"""
state_preflight.py — Boot-time LOGGING ONLY (Stage 1 safety).

Logs resolved absolute paths and whether files exist. Never creates
directories, never writes files, never restores state, never changes
scoring or EXECUTE behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _exists_line(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return "MISSING"
    try:
        return f"EXISTS size={p.stat().st_size}"
    except OSError:
        return "EXISTS (stat failed)"


def _resolve_known_paths() -> dict[str, str]:
    """Read paths from config / known defaults without mutating anything."""
    paths: dict[str, str] = {}
    try:
        import config
        paths["HEDGE_DB_PATH"] = os.path.abspath(
            getattr(config, "HEDGE_DB_PATH", "data/hedge_fund.db")
        )
        paths["NEWS_DB_PATH"] = os.path.abspath(
            getattr(config, "NEWS_DB_PATH", "data/news_room.db")
        )
    except Exception as e:
        paths["HEDGE_DB_PATH"] = f"(config error: {e})"
        paths["NEWS_DB_PATH"] = f"(config error: {e})"

    # active_trades — same formula as master_bot / tracker (env or next to package)
    try:
        import master_bot
        paths["ACTIVE_TRADES_PATH"] = os.path.abspath(
            getattr(master_bot, "ACTIVE_TRADES_PATH", "active_trades.json")
        )
    except Exception:
        paths["ACTIVE_TRADES_PATH"] = os.path.abspath(
            os.environ.get(
                "ACTIVE_TRADES_PATH",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_trades.json"),
            )
        )

    try:
        import midday_delta
        paths["SESSION_BASELINE_PATH"] = os.path.abspath(
            getattr(midday_delta, "BASELINE_PATH", "data/session_baseline.json")
        )
    except Exception:
        paths["SESSION_BASELINE_PATH"] = os.path.abspath("data/session_baseline.json")

    return paths


def log_resolved_paths() -> dict[str, str]:
    paths = _resolve_known_paths()
    print("[preflight] === Durable store paths (log only; no writes) ===")
    for key, path in paths.items():
        if path.startswith("(config"):
            print(f"[preflight] {key}={path}")
        else:
            print(f"[preflight] {key}={path} | {_exists_line(path)}")
    print("[preflight] === end paths ===")
    return paths


def log_open_position_counts() -> dict[str, Any]:
    """Count open trades in JSON / SQLite if readable; never restore or write."""
    result: dict[str, Any] = {"json_open": None, "sqlite_open": None}
    try:
        from tracker_agent import load_active_trades, ACTIVE_TRADES_PATH
        result["json_open"] = len(load_active_trades(ACTIVE_TRADES_PATH))
        result["json_path"] = str(ACTIVE_TRADES_PATH)
    except Exception as e:
        result["json_error"] = str(e)

    try:
        import sqlite3
        import config
        news = config.NEWS_DB_PATH
        result["sqlite_path"] = os.path.abspath(news)
        if os.path.exists(news):
            with sqlite3.connect(news, timeout=5.0) as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='active_trades_store'"
                ).fetchone()
                if row:
                    result["sqlite_open"] = int(
                        conn.execute("SELECT COUNT(*) FROM active_trades_store").fetchone()[0]
                    )
                else:
                    result["sqlite_open"] = 0
        else:
            result["sqlite_open"] = None  # file missing
    except Exception as e:
        result["sqlite_error"] = str(e)

    print(
        f"[preflight] open positions (read-only): "
        f"json={result.get('json_open')} sqlite={result.get('sqlite_open')}"
    )
    if (
        result.get("json_open") == 0
        and isinstance(result.get("sqlite_open"), int)
        and result["sqlite_open"] > 0
    ):
        print(
            "[preflight] WARNING: JSON empty but active_trades_store has rows "
            f"({result['sqlite_open']}). Log only — no auto-restore in Stage 1."
        )
    return result


def run_preflight() -> dict[str, Any]:
    """Full boot preflight: paths + open counts. No side effects on disk."""
    paths = log_resolved_paths()
    counts = log_open_position_counts()
    return {"paths": paths, "open_counts": counts}
