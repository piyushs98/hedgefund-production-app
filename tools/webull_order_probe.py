"""One sandbox order-type check. Refuses every Chicago date except 2026-11-27.

Places one far 1-lot SPY limit, then one of each stop shape, and cancels all.
Does not run on any other day. Does not print credentials or full account ids.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import session_clock  # noqa: E402
from webull_exec.broker import BrokerError, SdkBroker, scrub  # noqa: E402
from webull_exec.envfile import load_dotenv  # noqa: E402
from webull_exec.ids import make_id, occ_symbol  # noqa: E402
from webull_exec.orders import entry_limit, entry_with_stop, protective_stop  # noqa: E402

REQUIRED = "2026-11-27"
OUT = ROOT / "logs" / "webull_order_probe.txt"


def _say(lines: list[str], text: str) -> None:
    lines.append(text)
    print(text)


def _shapes(symbol_fields: dict, seq: int) -> list[tuple[str, list[dict], str | None]]:
    day = REQUIRED.replace("-", "")
    ticker = symbol_fields["ticker"]
    strike = symbol_fields["strike"]
    expiration = symbol_fields["expiration"]
    option_type = symbol_fields["option_type"]
    far = entry_limit(
        client_order_id=make_id(day, seq, "b"),
        ticker=ticker,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        entry_ask=0.01,
    )
    stop_market_day = protective_stop(
        client_order_id=make_id(day, seq + 1, "s"),
        ticker=ticker,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        stop_price=0.05,
        stop_limit=0.01,
    )
    stop_market_day[0]["order_type"] = "STOP_LOSS"
    stop_market_day[0].pop("limit_price", None)
    stop_market_gtc = protective_stop(
        client_order_id=make_id(day, seq + 2, "s"),
        ticker=ticker,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        stop_price=0.05,
        stop_limit=0.01,
    )
    stop_market_gtc[0]["order_type"] = "STOP_LOSS"
    stop_market_gtc[0]["time_in_force"] = "GTC"
    stop_market_gtc[0].pop("limit_price", None)
    stop_limit_day = protective_stop(
        client_order_id=make_id(day, seq + 3, "s"),
        ticker=ticker,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        stop_price=0.05,
        stop_limit=0.01,
    )
    stop_limit_gtc = protective_stop(
        client_order_id=make_id(day, seq + 4, "s"),
        ticker=ticker,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        stop_price=0.05,
        stop_limit=0.01,
    )
    stop_limit_gtc[0]["time_in_force"] = "GTC"
    combo = entry_with_stop(
        buy_id=make_id(day, seq + 5, "b"),
        stop_id=make_id(day, seq + 6, "s"),
        ticker=ticker,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        entry_ask=0.01,
        stop_price=0.05,
        stop_limit=0.01,
    )
    combo_id = make_id(day, seq + 7, "c")
    return [
        ("LIMIT DAY 0.01", far, None),
        ("STOP_LOSS DAY", stop_market_day, None),
        ("STOP_LOSS GTC", stop_market_gtc, None),
        ("STOP_LOSS_LIMIT DAY", stop_limit_day, None),
        ("STOP_LOSS_LIMIT GTC", stop_limit_gtc, None),
        ("MASTER+STOP_LOSS_LIMIT DAY", combo, combo_id),
    ]


def _result_line(name: str, result: dict) -> str:
    status = result.get("status")
    error = scrub(str(result.get("error") or ""))[:180]
    return f"{name} ok={result.get('ok')} status={status} error={error}"


def _cancel_all(broker: SdkBroker, lines: list[str]) -> int:
    left = 0
    try:
        orders = broker.open_orders()
    except BrokerError as exc:
        _say(lines, f"open_orders=fail status={exc.status} {scrub(str(exc))}")
        return -1
    for order in orders:
        client_order_id = order.get("client_order_id")
        if not client_order_id:
            continue
        result = broker.cancel(client_order_id)
        _say(lines, f"cancel id_len={len(client_order_id)} ok={result.get('ok')} status={result.get('status')}")
    try:
        left = len(broker.open_orders())
    except BrokerError as exc:
        _say(lines, f"open_orders_after=fail status={exc.status}")
        return -1
    _say(lines, f"open_orders_after={left}")
    return left


def main() -> int:
    today = session_clock.chicago_now().date().isoformat()
    lines = [f"probe_at={datetime.now(timezone.utc).replace(microsecond=0).isoformat()}", f"chicago_date={today}"]
    if today != REQUIRED:
        _say(lines, f"refused: order probe runs only on {REQUIRED}")
        _write(lines)
        return 2
    import os

    load_dotenv(ROOT / ".env")
    key = (os.environ.get("WEBULL_SANDBOX_APP_KEY") or "").strip()
    secret = (os.environ.get("WEBULL_SANDBOX_APP_SECRET") or "").strip()
    if not key or not secret:
        _say(lines, "refused: sandbox credentials are missing")
        _write(lines)
        return 2
    broker = SdkBroker(key, secret, sandbox=True)
    try:
        broker.connect()
    except BrokerError as exc:
        _say(lines, f"auth=fail status={exc.status} {scrub(str(exc))}")
        _write(lines)
        return 1
    _say(lines, "auth=ok account=…" + broker.account_id[-4:])
    contract = _one_spy(broker, lines)
    if contract is None:
        _cancel_all(broker, lines)
        _write(lines)
        return 1
    _say(
        lines,
        f"contract {contract['ticker']} {contract['expiration']} {contract['option_type']} {contract['strike']} "
        f"occ={occ_symbol(contract['ticker'], contract['expiration'], contract['option_type'], contract['strike'])}",
    )
    for name, orders, combo_id in _shapes(contract, 9100):
        result = broker.place(orders, combo_id)
        _say(lines, _result_line(name, result))
        for order in orders:
            client_order_id = order.get("client_order_id")
            if client_order_id:
                broker.cancel(client_order_id)
    left = _cancel_all(broker, lines)
    _write(lines)
    return 0 if left == 0 else 1


def _one_spy(broker: SdkBroker, lines: list[str]) -> dict | None:
    try:
        rows = broker.list_option_contracts("SPY", end_date=REQUIRED)
    except BrokerError as exc:
        _say(lines, f"chain=fail status={exc.status} {scrub(str(exc))}")
        return None
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        strike = raw.get("strike_price") or raw.get("strikePrice") or raw.get("strike")
        expiration = str(raw.get("expire_date") or raw.get("expiration_date") or raw.get("expiration") or "")[:10]
        option_type = str(raw.get("option_type") or raw.get("optionType") or "CALL")
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        if len(expiration) == 10:
            return {
                "ticker": "SPY",
                "strike": strike_f,
                "expiration": expiration,
                "option_type": "CALL" if option_type.upper().startswith("C") else "PUT",
            }
    _say(lines, "chain=empty")
    return None


def _write(lines: list[str]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
