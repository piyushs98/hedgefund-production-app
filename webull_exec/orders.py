"""US option order payloads. Single leg only."""

from __future__ import annotations

from webull_exec.limits import money


def _leg(ticker: str, side: str, qty: int, strike: float, expiration: str, option_type: str) -> dict:
    cp = "CALL" if str(option_type).upper().startswith("C") else "PUT"
    return {
        "side": side,
        "quantity": str(int(qty)),
        "symbol": ticker.upper(),
        "strike_price": money(strike),
        "option_expire_date": expiration[:10],
        "instrument_type": "OPTION",
        "option_type": cp,
        "market": "US",
    }


def _base(
    *,
    client_order_id: str,
    combo_type: str,
    order_type: str,
    side: str,
    ticker: str,
    qty: int,
    strike: float,
    expiration: str,
    option_type: str,
    position_intent: str,
    time_in_force: str = "DAY",
    limit_price: float | None = None,
    stop_price: float | None = None,
) -> dict:
    body = {
        "client_order_id": client_order_id,
        "combo_type": combo_type,
        "order_type": order_type,
        "quantity": str(int(qty)),
        "option_strategy": "SINGLE",
        "side": side,
        "time_in_force": time_in_force,
        "entrust_type": "QTY",
        "instrument_type": "OPTION",
        "market": "US",
        "symbol": ticker.upper(),
        "position_intent": position_intent,
        "legs": [_leg(ticker, side, qty, strike, expiration, option_type)],
    }
    if limit_price is not None:
        body["limit_price"] = money(limit_price)
    if stop_price is not None:
        body["stop_price"] = money(stop_price)
    return body


def entry_with_stop(
    *,
    buy_id: str,
    stop_id: str,
    ticker: str,
    strike: float,
    expiration: str,
    option_type: str,
    entry_ask: float,
    stop_price: float,
    stop_limit: float,
) -> list[dict]:
    buy = _base(
        client_order_id=buy_id,
        combo_type="MASTER",
        order_type="LIMIT",
        side="BUY",
        ticker=ticker,
        qty=1,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        position_intent="BUY_TO_OPEN",
        limit_price=entry_ask,
    )
    stop = _base(
        client_order_id=stop_id,
        combo_type="STOP_LOSS",
        order_type="STOP_LOSS_LIMIT",
        side="SELL",
        ticker=ticker,
        qty=1,
        strike=strike,
        expiration=expiration,
        option_type=option_type,
        position_intent="SELL_TO_CLOSE",
        limit_price=stop_limit,
        stop_price=stop_price,
    )
    return [buy, stop]


def entry_limit(
    *,
    client_order_id: str,
    ticker: str,
    strike: float,
    expiration: str,
    option_type: str,
    entry_ask: float,
) -> list[dict]:
    return [
        _base(
            client_order_id=client_order_id,
            combo_type="NORMAL",
            order_type="LIMIT",
            side="BUY",
            ticker=ticker,
            qty=1,
            strike=strike,
            expiration=expiration,
            option_type=option_type,
            position_intent="BUY_TO_OPEN",
            limit_price=entry_ask,
        )
    ]


def protective_stop(
    *,
    client_order_id: str,
    ticker: str,
    strike: float,
    expiration: str,
    option_type: str,
    stop_price: float,
    stop_limit: float,
) -> list[dict]:
    return [
        _base(
            client_order_id=client_order_id,
            combo_type="NORMAL",
            order_type="STOP_LOSS_LIMIT",
            side="SELL",
            ticker=ticker,
            qty=1,
            strike=strike,
            expiration=expiration,
            option_type=option_type,
            position_intent="SELL_TO_CLOSE",
            limit_price=stop_limit,
            stop_price=stop_price,
        )
    ]


def sell_limit(
    *,
    client_order_id: str,
    ticker: str,
    strike: float,
    expiration: str,
    option_type: str,
    limit_price: float,
) -> list[dict]:
    return [
        _base(
            client_order_id=client_order_id,
            combo_type="NORMAL",
            order_type="LIMIT",
            side="SELL",
            ticker=ticker,
            qty=1,
            strike=strike,
            expiration=expiration,
            option_type=option_type,
            position_intent="SELL_TO_CLOSE",
            limit_price=limit_price,
        )
    ]
