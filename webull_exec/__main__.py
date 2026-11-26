"""Run the executor loop. ``python -m webull_exec``."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from webull_exec.broker import AccountSuffixError, SdkBroker, mask_id, scrub
from webull_exec.clock import now_ct
from webull_exec.engine import Engine
from webull_exec.envfile import load_dotenv, read_mode
from webull_exec.mode import orders_allowed, status_line
from webull_exec.notify import Notifier
from webull_exec.paper import close_for, opens
from webull_exec.store import Store

ROOT = Path(__file__).resolve().parents[1]
PROD_ENV = ROOT / ".env"
PROD_DB = ROOT / "data" / "webull_exec.db"
PROD_HALT = ROOT / "data" / "webull_exec.halt"


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


def _parsed(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="webull_exec")
    parser.add_argument("--account-suffix", default="")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--db", default="")
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def _drill_paths(suffix: str, env_file: str, db: str) -> tuple[Path, Path] | None:
    """None keeps the production paths. A partial drill exits before any order."""
    if not suffix and not env_file and not db:
        return None
    if not suffix or not env_file or not db:
        print("[webull] drill needs --account-suffix, --env-file, and --db")
        raise SystemExit(2)
    env_path = Path(env_file)
    db_path = Path(db)
    if env_path.resolve() == PROD_ENV.resolve() or db_path.resolve() == PROD_DB.resolve():
        print("[webull] drill env file and database must not be the production files")
        raise SystemExit(2)
    return env_path, db_path


def main(argv: list[str] | None = None) -> None:
    args = _parsed(argv)
    suffix = (args.account_suffix or "").strip()
    env_file = (args.env_file or "").strip()
    db = (args.db or "").strip()
    drill = _drill_paths(suffix, env_file, db)
    os.chdir(ROOT)
    if drill is None:
        env_path = PROD_ENV
        load_dotenv(env_path)
        store = Store()
        notify = Notifier(store)
        halt = PROD_HALT
        account_suffix = None
    else:
        env_path, db_path = drill
        load_dotenv(env_path)
        store = Store(db_path)
        halt = db_path.with_name("webull_exec.halt")
        notify = Notifier(store, drill=True, drill_log=db_path.with_name("drill-notify.log"))
        account_suffix = suffix
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
        broker = SdkBroker(key, secret, sandbox=True, account_suffix=account_suffix)
        try:
            broker.connect()
        except AccountSuffixError as exc:
            print(f"[webull] {scrub(str(exc))}")
            raise SystemExit(2)
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
