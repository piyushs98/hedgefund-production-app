"""Official Webull SDK adapter. The SDK is imported only when connecting."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

SANDBOX_HOST = "api.sandbox.webull.com"
LIVE_HOST = "api.webull.com"

_SECRET = re.compile(
    r"(?i)(x-access-token|app_secret|app_key|x-signature|authorization)"
    r"(\s*[=:]\s*)\S+"
)


class BrokerError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def scrub(text: str) -> str:
    cleaned = _SECRET.sub(r"\1\2[redacted]", str(text))
    return cleaned[:500]


def _body(response) -> Any:
    if response is None:
        return None
    try:
        return response.json()
    except Exception:
        raw = getattr(response, "text", "") or ""
        return {"raw": scrub(raw)}


def _status(response) -> int | None:
    code = getattr(response, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _as_list(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "accounts", "items", "positions", "orders", "contracts", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _as_list(value)
            if nested:
                return nested
    return []


# The account-list schema has no options flag. US option orders belong on the
# individual brokerage account, and a sandbox key returns cash, margin, futures,
# crypto, and event accounts together. Margin is the one equity-options account
# when the payload does not say.
EQUITY_MARGIN = "INDIVIDUAL_MARGIN"
EQUITY_CASH = "INDIVIDUAL_CASH"


def _options_enabled(account: dict) -> bool | None:
    """True, False, or None when the payload does not say."""
    for key, value in account.items():
        name = str(key).lower()
        if "option" not in name:
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value > 0
        if isinstance(value, (dict, list)):
            continue
        text = str(value).strip().upper()
        if text in ("", "NONE", "NULL", "N/A"):
            continue
        if text in ("0", "FALSE", "NO", "DISABLED", "NOT_OPEN"):
            return False
        return True
    return None


def choose_account(accounts: list[dict]) -> str:
    """Pick one account id. Raise when the choice is ambiguous."""
    enabled = [row for row in accounts if row.get("options") is True]
    if len(enabled) == 1:
        return enabled[0]["account_id"]
    if len(enabled) > 1:
        raise BrokerError(f"account selection failed: {len(enabled)} accounts with options")
    unknown = [row for row in accounts if row.get("options") is None]
    if len(accounts) == 1 and unknown:
        return accounts[0]["account_id"]
    if unknown and len(unknown) == len(accounts):
        margin = [row for row in accounts if row.get("account_class") == EQUITY_MARGIN]
        cash = [row for row in accounts if row.get("account_class") == EQUITY_CASH]
        if len(margin) == 1:
            return margin[0]["account_id"]
        if len(margin) == 0 and len(cash) == 1:
            return cash[0]["account_id"]
    raise BrokerError(
        f"account selection failed: {len(accounts)} accounts, {len(enabled)} with options"
    )


def mask_id(value: str) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return "****"
    return "…" + text[-4:]


def _walk_accounts(payload: Any) -> list[dict]:
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if any(k in node for k in ("account_id", "accountId")):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


class SdkBroker:
    def __init__(self, app_key: str, app_secret: str, *, sandbox: bool = True):
        self._app_key = app_key
        self._app_secret = app_secret
        self._sandbox = sandbox
        self._api = None
        self._trade = None
        self._data = None
        self.account_id = ""

    def connect(self) -> None:
        from webull.core.client import ApiClient
        from webull.data.data_client import DataClient
        from webull.trade.trade_client import TradeClient

        logging.getLogger("webull").setLevel(logging.ERROR)
        api = ApiClient(self._app_key, self._app_secret, "us")
        api._stream_logger_set = True
        api._file_logger_set = True
        host = SANDBOX_HOST if self._sandbox else LIVE_HOST
        api.add_endpoint("us", host)
        self._api = api
        self._trade = TradeClient(api)
        self._data = DataClient(api)
        self.account_id = self._select_account()

    def _call(self, fn):
        try:
            response = fn()
        except Exception as exc:
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            message = scrub(str(exc))
            if status is None and "403" in message:
                status = 403
            raise BrokerError(message, status) from None
        code = _status(response)
        if code is not None and code >= 400:
            raise BrokerError(scrub(getattr(response, "text", "") or f"HTTP {code}"), code)
        return response

    def _select_account(self) -> str:
        return choose_account(self.list_accounts())

    def list_accounts(self) -> list[dict]:
        response = self._call(lambda: self._trade.account_v2.get_account_list())
        rows = []
        for raw in _walk_accounts(_body(response)):
            account_id = str(raw.get("account_id") or raw.get("accountId") or "")
            if not account_id:
                continue
            account_type = str(raw.get("account_type") or raw.get("accountType") or raw.get("type") or "")
            rows.append(
                {
                    "account_id": account_id,
                    "account_type": account_type,
                    "account_class": str(raw.get("account_class") or raw.get("accountClass") or ""),
                    "account_label": str(raw.get("account_label") or raw.get("accountLabel") or ""),
                    "options": _options_enabled(raw),
                    "masked": mask_id(account_id),
                }
            )
        return rows

    def balances(self, account_id: str | None = None) -> dict:
        account_id = account_id or self.account_id
        response = self._call(
            lambda: self._trade.account_v2.get_account_balance(account_id)
        )
        return _safe_numbers(_body(response))

    def positions(self, account_id: str | None = None) -> list[dict]:
        account_id = account_id or self.account_id
        response = self._call(
            lambda: self._trade.account_v2.get_account_position(account_id)
        )
        out = []
        for raw in _as_list(_body(response)):
            if not isinstance(raw, dict):
                continue
            parsed = _position(raw)
            if parsed is not None and parsed["qty"] != 0:
                out.append(parsed)
        return out

    def open_orders(self) -> list[dict]:
        response = self._call(
            lambda: self._trade.order_v3.get_order_open(self.account_id)
        )
        out = []
        for raw in _as_list(_body(response)):
            if isinstance(raw, dict):
                parsed = _order(raw)
                if parsed is not None:
                    out.append(parsed)
        return out

    def order_status(self, client_order_id: str) -> dict:
        response = self._call(
            lambda: self._trade.order_v3.get_order_detail(self.account_id, client_order_id)
        )
        body = _body(response)
        rows = _as_list(body) or ([body] if isinstance(body, dict) else [])
        for raw in rows:
            if isinstance(raw, dict):
                parsed = _order(raw)
                if parsed is not None:
                    return parsed
        return {"client_order_id": client_order_id, "status": "UNKNOWN", "filled_qty": 0, "avg_price": None}

    def place(self, orders: list[dict], combo_id: str | None = None) -> dict:
        try:
            response = self._call(
                lambda: self._trade.order_v3.place_order(self.account_id, orders, combo_id)
            )
        except BrokerError as exc:
            return {"ok": False, "error": str(exc), "status": exc.status}
        return {"ok": True, "status": _status(response), "body": _public_body(_body(response))}

    def cancel(self, client_order_id: str) -> dict:
        try:
            response = self._call(
                lambda: self._trade.order_v3.cancel_order(self.account_id, client_order_id)
            )
        except BrokerError as exc:
            return {"ok": False, "error": str(exc), "status": exc.status}
        return {"ok": True, "status": _status(response)}

    def snapshot_option(
        self, ticker: str, expiration: str, option_type: str, strike: float
    ) -> dict:
        from webull_exec.ids import occ_symbol

        return self.snapshot(occ_symbol(ticker, expiration, option_type, strike))

    def snapshot_bid(self, ticker: str, expiration: str, option_type: str, strike: float) -> float | None:
        return self.snapshot_option(ticker, expiration, option_type, strike).get("bid")

    def list_option_contracts(
        self,
        underlying: str,
        end_date: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
    ) -> list[dict]:
        def call():
            return self._data.instrument.list_option_contracts(
                category="US_OPTION",
                underlying_symbols=underlying,
                end_date=end_date,
                status="LISTING",
                strike_price_gte=None if strike_gte is None else str(strike_gte),
                strike_price_lte=None if strike_lte is None else str(strike_lte),
            )

        response = self._call(call)
        out = []
        for raw in _as_list(_body(response)):
            if isinstance(raw, dict):
                out.append(raw)
        return out

    def snapshot(self, symbol: str) -> dict:
        response = self._call(
            lambda: self._data.option_market_data.get_option_snapshot(symbol, "US_OPTION")
        )
        rows = _as_list(_body(response))
        raw = rows[0] if rows else (_body(response) if isinstance(_body(response), dict) else {})
        if not isinstance(raw, dict):
            raw = {}
        return {
            "symbol": str(raw.get("symbol") or symbol),
            "bid": _num(raw.get("bid")),
            "ask": _num(raw.get("ask")),
            "quote_time": raw.get("quote_time") or raw.get("quoteTime"),
            "status": _status(response),
        }


def _num(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_numbers(payload: Any) -> dict:
    keep = {
        "total_cash", "cash_balance", "buying_power", "net_liquidation",
        "total_market_value", "currency", "unsettled_cash", "option_buying_power",
        "day_buying_power", "cash", "equity", "market_value",
    }
    found: dict = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key) in keep and not isinstance(value, (dict, list)):
                    found[str(key)] = value
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _public_body(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {"ok": True}
    redacted = {}
    for key, value in payload.items():
        if "account" in str(key).lower() or "token" in str(key).lower():
            continue
        if isinstance(value, (dict, list)):
            continue
        redacted[str(key)] = value
    return redacted


def _position(raw: dict) -> dict | None:
    symbol = str(raw.get("symbol") or raw.get("option_symbol") or "")
    qty = _num(raw.get("quantity") or raw.get("qty") or raw.get("position"))
    if qty is None:
        qty = _num(raw.get("position_qty"))
    ticker = str(raw.get("underlying_symbol") or raw.get("ticker") or "").upper()
    option_type = str(raw.get("option_type") or raw.get("optionType") or "").upper()
    strike = _num(raw.get("strike_price") or raw.get("strikePrice") or raw.get("strike"))
    expiration = str(raw.get("option_expire_date") or raw.get("expiration") or raw.get("expire_date") or "")[:10]
    if not ticker or not option_type or strike is None or len(expiration) != 10:
        parsed = _parse_occ(symbol)
        if parsed is None:
            if qty:
                return {
                    "ticker": ticker or symbol or "UNKNOWN",
                    "option_type": option_type or "",
                    "strike": strike,
                    "expiration": expiration,
                    "qty": int(qty),
                    "symbol": symbol,
                }
            return None
        ticker, expiration, option_type, strike = parsed
    return {
        "ticker": ticker,
        "option_type": "CALL" if option_type.startswith("C") else "PUT",
        "strike": float(strike),
        "expiration": expiration,
        "qty": int(qty or 0),
        "symbol": symbol,
    }


def _order(raw: dict) -> dict | None:
    client_order_id = str(raw.get("client_order_id") or raw.get("clientOrderId") or "")
    if not client_order_id:
        orders = raw.get("orders") or raw.get("new_orders")
        if isinstance(orders, list) and orders:
            return _order(orders[0]) if isinstance(orders[0], dict) else None
        return None
    status = str(raw.get("status") or raw.get("order_status") or "WORKING").upper()
    filled = _num(raw.get("filled_quantity") or raw.get("filled_qty") or raw.get("filledQuantity")) or 0
    avg = _num(raw.get("avg_price") or raw.get("avgPrice") or raw.get("filled_price") or raw.get("fill_price"))
    return {
        "client_order_id": client_order_id,
        "status": status,
        "filled_qty": filled,
        "avg_price": avg,
    }


def _parse_occ(symbol: str):
    match = re.match(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$", symbol.replace(" ", ""))
    if not match:
        return None
    ticker, ymd, cp, strike = match.groups()
    expiration = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}"
    option_type = "CALL" if cp == "C" else "PUT"
    return ticker, expiration, option_type, int(strike) / 1000.0
