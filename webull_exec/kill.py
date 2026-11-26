"""Cancel open orders, flatten positions, and set EXECUTION_MODE=off.

Run: .venv-webull/bin/python -m webull_exec.kill
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from webull_exec.broker import SdkBroker
from webull_exec.clock import now_ct
from webull_exec.envfile import load_dotenv, set_mode_off
from webull_exec.mode import live_armed
from webull_exec.store import Store

ROOT = Path(__file__).resolve().parents[1]


def flatten(broker, store, now: datetime) -> tuple[int, int]:
    """Cancel every open order and sell every position. Returns (cancels, sells)."""
    from webull_exec.engine import Engine
    from webull_exec.notify import Notifier

    engine = Engine(broker, store, Notifier(store), lambda: [], lambda _tid: None)
    orders = broker.open_orders()
    positions = broker.positions()
    engine._cancel_orders(orders)
    engine._sell_positions(positions, now, intent_name="kill")
    return len(orders), len(positions)


def shutdown(store, env_path: Path, halt_path: Path) -> None:
    store.set_control("killed", "1")
    store.set_control("disabled", "1")
    halt_path.parent.mkdir(parents=True, exist_ok=True)
    halt_path.write_text("killed\n", encoding="utf-8")
    set_mode_off(env_path)


def main() -> int:
    os.chdir(ROOT)
    env_path = ROOT / ".env"
    load_dotenv(env_path)
    if live_armed() and (os.environ.get("EXECUTION_MODE") or "").strip().lower() == "live":
        print("[webull] kill refused: live mode is not used. Sandbox only.")
        return 2
    key = (os.environ.get("WEBULL_SANDBOX_APP_KEY") or "").strip()
    secret = (os.environ.get("WEBULL_SANDBOX_APP_SECRET") or "").strip()
    if not key or not secret:
        print("[webull] kill: sandbox credentials are missing")
        return 2
    store = Store()
    halt = ROOT / "data" / "webull_exec.halt"
    shutdown(store, env_path, halt)
    broker = SdkBroker(key, secret, sandbox=True)
    try:
        broker.connect()
    except Exception as exc:
        print("[webull] kill: connect failed")
        print(type(exc).__name__)
        return 1
    cancels, sells = flatten(broker, store, now_ct())
    left_orders = broker.open_orders()
    left_positions = broker.positions()
    print(f"[webull] kill cancelled {cancels} orders and sent {sells} sells")
    print(f"[webull] remaining orders {len(left_orders)} positions {len(left_positions)}")
    print("[webull] EXECUTION_MODE=off")
    if left_orders or left_positions:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
