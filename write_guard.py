"""
write_guard.py — Loud durable-write failure tracking (Stage 1 safety).

Does not participate in scoring or EXECUTE decisions. Call only from existing
write success/failure paths. Three consecutive failures on the same store
send a CRITICAL Discord alert (best-effort); the scan continues either way.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_consecutive: dict[str, int] = {}
_last_critical_at: dict[str, float] = {}
_CRITICAL_COOLDOWN_S = 900
_THRESHOLD = 3


def record_write_ok(store: str) -> None:
    with _lock:
        _consecutive[store] = 0


def record_write_fail(store: str, error: Any = None, *, detail: str = "") -> int:
    with _lock:
        n = _consecutive.get(store, 0) + 1
        _consecutive[store] = n
        should_alert = n >= _THRESHOLD
        now = time.time()
        if should_alert:
            last = _last_critical_at.get(store, 0.0)
            if now - last < _CRITICAL_COOLDOWN_S:
                should_alert = False
            else:
                _last_critical_at[store] = now

    msg_tail = f": {error}" if error else ""
    if detail:
        msg_tail = f"{msg_tail} ({detail})" if msg_tail else f": {detail}"
    print(f"[write_guard] FAIL store={store} consecutive={n}{msg_tail}")

    if should_alert:
        _emit_critical(store, n, error, detail)
    return n


def _emit_critical(store: str, n: int, error: Any, detail: str) -> None:
    text = (
        f"🚨 **CRITICAL: durable write failures**\n"
        f"Store `{store}` failed **{n}** times in a row.\n"
        f"Detail: {detail or error or 'unknown'}\n"
        f"Scan continues — check logs / disk / permissions (state may be lost)."
    )
    try:
        import broadcaster
        ok = broadcaster.send_discord_alert(text)
        print(f"[write_guard] CRITICAL Discord alert for {store} delivered={ok}")
    except Exception as e:
        print(f"[write_guard] CRITICAL Discord alert FAILED for {store}: {e}")


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_consecutive)
