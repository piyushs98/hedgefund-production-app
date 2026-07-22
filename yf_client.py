"""
Shared Yahoo Finance client with a browser-like requests.Session.

Yahoo Finance rate-limits bare cloud / data-center IPs that look like API
clients. Every yf.Ticker / yf.download call in this repo must go through this
module so the standard web User-Agent is always attached.

Network safety: SESSION uses TimeoutHTTPAdapter so any request that does not
pass an explicit timeout inherits DEFAULT_TIMEOUT (15s) instead of hanging
a background trading thread forever.
"""

from __future__ import annotations

import time

import requests
import yfinance as yf
from requests.adapters import HTTPAdapter

# Default socket timeout (connect + read) when callers omit timeout=
DEFAULT_TIMEOUT = 15


class TimeoutHTTPAdapter(HTTPAdapter):
    """
    HTTPAdapter that injects a default timeout when none is supplied.

    requests.Session defaults to timeout=None (wait forever). Mounting this
    adapter on SESSION guarantees every Yahoo / downstream call through the
    shared session fails closed after DEFAULT_TIMEOUT seconds unless the
    caller overrides timeout explicitly.
    """

    def __init__(self, *args, timeout: float = DEFAULT_TIMEOUT, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        # Only inject when the caller left timeout unset (None).
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


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

# Strict default timeouts for all traffic through SESSION.
_timeout_adapter = TimeoutHTTPAdapter(timeout=DEFAULT_TIMEOUT)
SESSION.mount("http://", _timeout_adapter)
SESSION.mount("https://", _timeout_adapter)

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
