"""Hard caps. They do not change paper sizing."""

from __future__ import annotations

TICKERS = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
)
MAX_QTY = 1
MAX_OPEN = 3
MAX_PREMIUM = 7.50
DAILY_STOP_R = -3.0
CUMULATIVE_LOSS = -1000.0
ENTRY_WAIT_S = 60
EXIT_REPRICE_S = 30
EXIT_CRITICAL_S = 120
STOP_UNPROTECTED_S = 60


def stop_limit_price(stop: float) -> float:
    """Limit one paper spread-cap (8%) through the stop, floored at one cent."""
    price = round(float(stop) * 0.92, 2)
    if price > float(stop):
        price = round(float(stop), 2)
    return max(0.01, price)


def money(price: float) -> str:
    return f"{float(price):.2f}"
