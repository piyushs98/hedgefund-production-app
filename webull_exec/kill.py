"""Cancel open orders, flatten positions, and set EXECUTION_MODE=off.

Run: .venv-webull/bin/python -m webull_exec.kill
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from webull_exec.broker import AccountSuffixError, SdkBroker, scrub
from webull_exec.clock import now_ct
from webull_exec.envfile import load_dotenv, set_mode_off
from webull_exec.mode import live_armed
from webull_exec.store import Store

ROOT = Path(__file__).resolve().parents[1]
PROD_ENV = ROOT / ".env"


def flatten(broker, store, now: datetime, *, drill: bool = False, drill_log=None) -> tuple[int, int]:
    """Cancel every open order and sell every position. Returns (cancels, sells)."""
    from webull_exec.engine import Engine
    from webull_exec.notify import Notifier

    engine = Engine(
        broker,
        store,
        Notifier(store, drill=drill, drill_log=drill_log),
        lambda: [],
        lambda _tid: None,
    )
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


def _parsed(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="webull_exec.kill")
    parser.add_argument("--account-suffix", default="")
    parser.add_argument("--env-file", default="")
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def _keys() -> tuple[str, str]:
    key = (os.environ.get("WEBULL_SANDBOX_APP_KEY") or "").strip()
    secret = (os.environ.get("WEBULL_SANDBOX_APP_SECRET") or "").strip()
    return key, secret


def _refuse_live() -> bool:
    if live_armed() and (os.environ.get("EXECUTION_MODE") or "").strip().lower() == "live":
        print("[webull] kill refused: live mode is not used. Sandbox only.")
        return True
    return False


def _drill_main(suffix: str, env_file: str) -> int:
    """Flatten one account and turn off only the drill env file.

    Does not write the production .env, the production halt file, or the
    production executor database.
    """
    if not suffix or not env_file:
        print("[webull] kill: a drill needs --account-suffix and --env-file")
        return 2
    env_path = Path(env_file)
    if env_path.resolve() == PROD_ENV.resolve():
        print("[webull] kill: --env-file must not be the production .env")
        return 2
    load_dotenv(env_path)
    if _refuse_live():
        return 2
    key, secret = _keys()
    if not key or not secret:
        print("[webull] kill: sandbox credentials are missing")
        return 2
    broker = SdkBroker(key, secret, sandbox=True, account_suffix=suffix)
    try:
        broker.connect()
    except AccountSuffixError as exc:
        print(f"[webull] {scrub(str(exc))}")
        return 2
    except Exception as exc:
        print("[webull] kill: connect failed")
        print(type(exc).__name__)
        return 1
    store = Store(env_path.with_name(env_path.name + ".kill.db"))
    drill_log = env_path.with_name(env_path.name + ".notify.log")
    set_mode_off(env_path)
    cancels, sells = flatten(broker, store, now_ct(), drill=True, drill_log=drill_log)
    left_orders = broker.open_orders()
    left_positions = broker.positions()
    print(f"[webull] kill cancelled {cancels} orders and sent {sells} sells")
    print(f"[webull] remaining orders {len(left_orders)} positions {len(left_positions)}")
    print("[webull] EXECUTION_MODE=off")
    if left_orders or left_positions:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parsed(argv)
    suffix = (args.account_suffix or "").strip()
    env_file = (args.env_file or "").strip()
    os.chdir(ROOT)
    if suffix or env_file:
        return _drill_main(suffix, env_file)
    env_path = PROD_ENV
    load_dotenv(env_path)
    if _refuse_live():
        return 2
    key, secret = _keys()
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
