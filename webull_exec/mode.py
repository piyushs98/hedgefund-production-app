"""EXECUTION_MODE and LIVE_TRADING_ENABLED. Read from the environment only."""

from __future__ import annotations

import os

MODES = ("off", "sandbox", "live")


def execution_mode() -> str:
    raw = (os.environ.get("EXECUTION_MODE") or "off").strip().lower()
    if raw not in MODES:
        return "off"
    return raw


def live_armed() -> bool:
    return (os.environ.get("LIVE_TRADING_ENABLED") or "").strip().lower() == "true"


def orders_allowed() -> bool:
    mode = execution_mode()
    if mode == "sandbox":
        return True
    if mode == "live" and live_armed():
        return True
    return False


def status_line() -> str:
    return f"execution={execution_mode()} live_armed={'true' if live_armed() else 'false'}"
