"""
Shared Yahoo Finance client with a browser-like requests.Session.

Yahoo Finance rate-limits bare cloud / data-center IPs that look like API
clients. Every yf.Ticker / yf.download call in this repo must go through this
module so the standard web User-Agent is always attached.
"""

from __future__ import annotations

import time

import requests
import yfinance as yf

# Single shared session used by the whole process.
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

TICKER_PACING_SECONDS = 2


def ticker(symbol):
    """Return yf.Ticker(symbol, session=SESSION) — never bare."""
    return yf.Ticker(symbol, session=SESSION)


def download(*args, **kwargs):
    """Return yf.download(..., session=SESSION) — never bare."""
    kwargs["session"] = SESSION
    return yf.download(*args, **kwargs)


def pace(index: int = 0, total: int = 1, seconds: float | None = None) -> None:
    """Sleep between multi-ticker loop iterations (no sleep after the last item)."""
    if total <= 1 or index >= total - 1:
        return
    time.sleep(TICKER_PACING_SECONDS if seconds is None else seconds)
