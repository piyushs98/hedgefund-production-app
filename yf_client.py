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

import threading
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

# Crumb HTTP 429. Yahoo's getcrumb is what quoteSummary (earnings, futures
# info, and anything else that needs a crumb) depends on. A 429 used to be
# retried on the next night-loop pass ~95s later, which kept the IP throttled
# until the open. Back off exponentially and do not send another getcrumb
# until the window expires. Base 15 min, then 30, 60, 120, cap 4 h.
_CRUMB_BACKOFF_BASE_S = 15 * 60
_CRUMB_BACKOFF_CAP_S = 4 * 60 * 60
_crumb_lock = threading.Lock()
_crumb_backoff_until = 0.0  # time.monotonic()
_crumb_backoff_exp = 0
_original_session_request = SESSION.request


def reset_crumb_backoff_for_tests() -> None:
    global _crumb_backoff_until, _crumb_backoff_exp
    with _crumb_lock:
        _crumb_backoff_until = 0.0
        _crumb_backoff_exp = 0


def crumb_backoff_remaining() -> float:
    with _crumb_lock:
        return max(0.0, _crumb_backoff_until - time.monotonic())


def note_crumb_429() -> float:
    """Record a getcrumb 429. Returns the new backoff length in seconds."""
    global _crumb_backoff_until, _crumb_backoff_exp
    with _crumb_lock:
        delay = min(
            _CRUMB_BACKOFF_CAP_S,
            _CRUMB_BACKOFF_BASE_S * (2 ** _crumb_backoff_exp),
        )
        _crumb_backoff_exp = min(_crumb_backoff_exp + 1, 8)
        _crumb_backoff_until = time.monotonic() + delay
        return float(delay)


def note_crumb_ok() -> None:
    """A real crumb arrived. The next 429 starts the ladder over at 15 min."""
    global _crumb_backoff_until, _crumb_backoff_exp
    with _crumb_lock:
        _crumb_backoff_until = 0.0
        _crumb_backoff_exp = 0


def _is_crumb_url(url) -> bool:
    return "getcrumb" in str(url or "").lower()


def _synthetic_crumb_429(url: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 429
    response.reason = "Too Many Requests"
    response.url = url
    response.headers["Content-Type"] = "text/plain"
    response.encoding = "utf-8"
    response._content = b"Too Many Requests"
    return response


def _note_crumb_response(url, response) -> None:
    final_url = str(getattr(response, "url", "") or "")
    if not (_is_crumb_url(url) or _is_crumb_url(final_url)):
        return
    code = getattr(response, "status_code", None)
    body = ""
    if code in (200, 429):
        try:
            body = response.text or ""
        except Exception:
            body = ""
    if code == 429 or "Too Many Requests" in body[:400]:
        delay = note_crumb_429()
        print(
            f"[Yahoo] Crumb fetch rate-limited (HTTP {code}), "
            f"backing off {int(delay)}s. Not retrying this loop."
        )
        return
    if code == 200 and body and "<html>" not in body[:200] and len(body) < 200:
        note_crumb_ok()


def _request_with_crumb_backoff(method, url, *args, **kwargs):
    url_text = url if isinstance(url, str) else str(url)
    if _is_crumb_url(url_text) and crumb_backoff_remaining() > 0:
        remaining = crumb_backoff_remaining()
        print(
            f"[Yahoo] crumb fetch suppressed — backoff {remaining:.0f}s "
            "remaining (not retrying a 429)"
        )
        return _synthetic_crumb_429(url_text)
    response = _original_session_request(method, url, *args, **kwargs)
    _note_crumb_response(url_text, response)
    return response


SESSION.request = _request_with_crumb_backoff

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
