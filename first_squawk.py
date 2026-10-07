"""Public @FirstSquawk headlines. One page read, no retry.

The logged-out profile page embeds the newest posts. The separate
timeline URL returns 429, so this reader does not call it. A non-200
response is logged and dropped. Nothing is invented.

A line that names one of the ten tickers is stored on that ticker. The
scorer counts that row only for the session it belongs to. A Fed or macro
line that names none of them is stored as FED and does not enter those
ten scores.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import requests

PAGE_URL = "https://x.com/FirstSquawk"
PUBLISHER = "First Squawk"
MAX_AGE = timedelta(hours=36)
_TIMEOUT_S = 20

# Whole words only. Index aliases are the phrases the account actually prints.
_NAMES: dict[str, re.Pattern[str]] = {
    "AAPL": re.compile(r"\b(AAPL|APPLE)\b", re.I),
    "MSFT": re.compile(r"\b(MSFT|MICROSOFT)\b", re.I),
    "NVDA": re.compile(r"\b(NVDA|NVIDIA)\b", re.I),
    "AMZN": re.compile(r"\b(AMZN|AMAZON)\b", re.I),
    "META": re.compile(r"\b(META|FACEBOOK)\b", re.I),
    "GOOGL": re.compile(r"\b(GOOGL|GOOG|GOOGLE|ALPHABET)\b", re.I),
    "TSLA": re.compile(r"\b(TSLA|TESLA)\b", re.I),
    "SPY": re.compile(r"\b(SPY|S&P\s*500|S&P500)\b", re.I),
    "QQQ": re.compile(r"\b(QQQ|NASDAQ[\s-]*100)\b", re.I),
    "IWM": re.compile(r"\b(IWM|RUSSELL\s*2000)\b", re.I),
}
_MACRO = re.compile(
    r"\b(FOMC|FEDERAL RESERVE|POWELL|NONFARM|PAYROLLS|CPI|PCE|"
    r"ECB|BOJ|BANK OF JAPAN|BANK OF ENGLAND|TREASURY)\b|"
    r"\bFED(?:['’]S)?\b",
    re.I,
)
_PAIR = re.compile(
    r"created_at_ms:(\d+)(?:(?!created_at_ms:).){0,900}?"
    r'full_text:"((?:\\.|[^"\\])*)"',
    re.S,
)


def _unescape(raw: str) -> str:
    try:
        text = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        text = raw.replace(r"\"", '"').replace(r"\n", " ")
    return " ".join(str(text).split())


def posts_from_html(html: str, now: datetime | None = None) -> list[str]:
    """Newest embedded posts, dropping anything older than 36 hours."""
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    found: list[str] = []
    seen: set[str] = set()
    for ms_raw, raw in _PAIR.findall(html or ""):
        try:
            posted = datetime.fromtimestamp(int(ms_raw) / 1000, timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if clock - posted > MAX_AGE or posted - clock > timedelta(hours=1):
            continue
        text = _unescape(raw)
        if len(text) < 20 or text in seen:
            continue
        seen.add(text)
        found.append(text)
    return found


def route(text: str) -> list[str]:
    """Tickers the line belongs on. FED when it is macro and names nobody."""
    named = [ticker for ticker, pat in _NAMES.items() if pat.search(text or "")]
    if named:
        return named
    if _MACRO.search(text or ""):
        return ["FED"]
    return []


def fetch_first_squawk(save=None) -> int:
    """Read the public page once. Return how many new rows were stored."""
    if save is None:
        from news_memory import save_headline
        save = save_headline
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html",
    }
    try:
        response = requests.get(PAGE_URL, headers=headers, timeout=_TIMEOUT_S)
    except requests.RequestException as exc:
        print(f"[Squawk] page read failed ({type(exc).__name__}). Not retrying.")
        return 0
    if response.status_code != 200:
        print(
            f"[Squawk] page HTTP {response.status_code}. Not retrying."
        )
        return 0
    posts = posts_from_html(response.text or "")
    saved = 0
    kept = 0
    for text in posts:
        homes = route(text)
        if not homes:
            continue
        kept += 1
        for ticker in homes:
            if save(ticker, "Squawk", PUBLISHER, text):
                saved += 1
    print(
        f"[Squawk] page posts={len(posts)} kept={kept} new rows={saved}"
    )
    return saved
