"""Run the executor loop. ``python -m webull_exec``."""

from __future__ import annotations

import os
import time
from pathlib import Path

from webull_exec.broker import SdkBroker, mask_id, scrub
from webull_exec.clock import now_ct
from webull_exec.engine import Engine
from webull_exec.envfile import load_dotenv, read_mode
from webull_exec.mode import orders_allowed, status_line
from webull_exec.notify import Notifier
from webull_exec.paper import close_for, opens
from webull_exec.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _refresh_mode(env_path: Path) -> None:
    mode = read_mode(env_path)
    if mode:
        os.environ["EXECUTION_MODE"] = mode


def _keys() -> tuple[str, str]:
    key = (os.environ.get("WEBULL_SANDBOX_APP_KEY") or "").strip()
    secret = (os.environ.get("WEBULL_SANDBOX_APP_SECRET") or "").strip()
    return key, secret


def _should_connect(store: Store, halt: Path) -> bool:
    """Sandbox trading, or a disabled book that still has something to flatten."""
    if store.killed():
        return False
    if halt.exists() and store.get_control("disabled") != "1":
        store.set_control("disabled", "1")
    if orders_allowed():
        return True
    return store.disabled() or bool(store.open_intents())


def main() -> None:
    os.chdir(ROOT)
    env_path = ROOT / ".env"
    load_dotenv(env_path)
    store = Store()
    notify = Notifier(store)
    halt = ROOT / "data" / "webull_exec.halt"
    print("[webull] " + status_line())
    while True:
        _refresh_mode(env_path)
        if store.killed() or not _should_connect(store, halt):
            notify.heartbeat()
            time.sleep(60)
            continue
        key, secret = _keys()
        if not key or not secret:
            notify.critical("keys", "sandbox credentials are missing")
            time.sleep(60)
            continue
        if os.environ.get("EXECUTION_MODE", "").strip().lower() == "live":
            notify.critical("live-blocked", "live trading is not enabled for this build")
            time.sleep(60)
            continue
        broker = SdkBroker(key, secret, sandbox=True)
        try:
            broker.connect()
        except Exception as exc:
            notify.critical("connect", scrub(str(exc)))
            time.sleep(60)
            continue
        print(f"[webull] connected account {mask_id(broker.account_id)}")
        engine = Engine(
            broker,
            store,
            notify,
            opens,
            close_for,
            env_path=env_path,
            halt_path=halt,
        )
        engine.boot(now_ct())
        while not store.killed():
            _refresh_mode(env_path)
            if (os.environ.get("EXECUTION_MODE") or "").strip().lower() == "live":
                notify.critical("live-blocked", "live trading is not enabled for this build")
                break
            working = bool(store.open_intents()) or engine.boot_clearing
            if not orders_allowed() and not working:
                break
            try:
                engine.tick(now_ct())
            except Exception as exc:
                notify.critical("tick", scrub(type(exc).__name__ + " " + str(exc)))
            time.sleep(10)
        notify.heartbeat()


if __name__ == "__main__":
    main()
