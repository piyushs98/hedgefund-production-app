"""First Squawk through the official X API v2. Verbatim posts only.

The public-page parser is gone. This module does not fetch a profile
page and does not write headlines. A number is stored only when that
number is in the post text. The bearer token is never printed.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
HANDLE = "FirstSquawk"
ITEM_SOURCE = "firstsquawk"
TIMEOUT_S = 20
COST_PER_POST = 0.005
PROBE_QUERY = "from:FirstSquawk -is:retweet"

# % is not a word character, so a trailing word boundary would drop it.
_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:%|[KkMmBb])?"
_FIELD = {
    "actual": re.compile(rf"\bActual\b\s*:?\s*({_NUMBER})(?![\d%A-Za-z])", re.I),
    "forecast": re.compile(rf"\bForecast\b\s*:?\s*({_NUMBER})(?![\d%A-Za-z])", re.I),
    "prior": re.compile(rf"\bPrior\b\s*:?\s*({_NUMBER})(?![\d%A-Za-z])", re.I),
}
# Label before the number. A clock such as "8:30 EST" is not a forecast:
# EST is not read when it follows the number.
_FORECAST_ALIAS = re.compile(
    rf"\b(?:EST|EXP|CONSENSUS)\b\s*:?\s*({_NUMBER})(?![\d%A-Za-z])",
    re.I,
)
_PRIOR_LABEL = re.compile(
    rf"\b(?:PREVIOUS|PRIOR|PREV)\b\s*:?\s*({_NUMBER})(?![\d%A-Za-z])",
    re.I,
)
_PRIOR_AFTER = re.compile(
    rf"({_NUMBER})(?![\d%A-Za-z])\s+\b(?:PREVIOUS|PRIOR|PREV)\b",
    re.I,
)
_BARE = re.compile(r"^[A-Za-z0-9_]+$")
_TWEET_ID = re.compile(r"^[0-9A-Za-z]+$")
_SPEND_PHRASES = (
    "creditsdepleted",
    "credits depleted",
    "credits-depleted",
    "usage-capped",
    "usage cap",
    "spend cap",
    "spending limit",
    "billing cycle spend",
)


def _first_number(text: str, patterns: tuple) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def parse_release_numbers(text: str) -> tuple[str | None, str | None, str | None]:
    """Actual, forecast, and prior copied from the post.

    Forecast is Forecast, or EST / EXP / CONSENSUS when the word comes
    before the number. Prior is Prior, or PREVIOUS / PRIOR / PREV on
    either side of the number. A missing token stays None.
    """
    raw = text or ""
    actual = _first_number(raw, (_FIELD["actual"],))
    forecast = _first_number(raw, (_FIELD["forecast"], _FORECAST_ALIAS))
    prior = _first_number(raw, (_FIELD["prior"], _PRIOR_LABEL, _PRIOR_AFTER))
    return actual, forecast, prior


def release_component(text: str) -> str | None:
    """headline_mom, headline_yoy, core_mom, or core_yoy.

    CORE with no YoY is core month-over-month. A post that says both
    MoM and YoY stays None so it is not filed on the wrong series.
    A post with neither marker is headline month-over-month.
    """
    raw = text or ""
    core = re.search(r"\bCORE\b", raw, re.I) is not None
    mom = re.search(r"\bMOM\b", raw, re.I) is not None
    yoy = re.search(r"\bYOY\b", raw, re.I) is not None
    if mom and yoy:
        return None
    if core and yoy:
        return "core_yoy"
    if core:
        return "core_mom"
    if yoy:
        return "headline_yoy"
    return "headline_mom"


def keyword_hit(text: str, keywords: list[str]) -> bool:
    """Whole-word, case-insensitive. One hit is enough."""
    for word in keywords:
        if word and re.search(rf"\b{re.escape(word)}\b", text or "", re.I):
            return True
    return False


def parse_time(raw) -> datetime | None:
    """Aware ISO-8601 to UTC. A naive value is rejected."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def statement_window(release_at: datetime) -> tuple[datetime, datetime]:
    """Release minus 1 minute through release plus 5 minutes, inclusive."""
    return release_at - timedelta(minutes=1), release_at + timedelta(minutes=5)


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quote_term(word: str) -> str:
    if _BARE.match(word):
        return word
    escaped = word.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def build_query(keywords: list[str]) -> str:
    """from:FirstSquawk (term OR term) -is:retweet. Bare words stay unquoted."""
    terms = " OR ".join(_quote_term(word) for word in keywords)
    return f"from:{HANDLE} ({terms}) -is:retweet"


def search_params(keywords: list[str], window_start: datetime, window_end: datetime) -> dict:
    """Recent-search query. end_time is the window end, sent as RFC3339 UTC."""
    return {
        "query": build_query(keywords),
        "start_time": utc_iso(window_start),
        "end_time": utc_iso(window_end),
        "max_results": 10,
        "tweet.fields": "created_at",
    }


def probe_params() -> dict:
    """Last 10 posts. No time window. Used only by the manual command."""
    return {
        "query": PROBE_QUERY,
        "max_results": 10,
        "tweet.fields": "created_at",
    }


def posts_returned_count(payload) -> int:
    """Posts the response says it returned. A body with no count is zero."""
    if not isinstance(payload, dict):
        return 0
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("result_count"), int):
        return meta["result_count"]
    data = payload.get("data")
    if isinstance(data, list):
        return len(data)
    return 0


def posts_from_search(payload, window_start: datetime | None, window_end: datetime | None) -> list[dict]:
    """Posts with an id, text, and a timezone-aware created_at inside the window.

    A None bound skips that side of the check. The query is the keyword
    filter, so a returned post is kept even when the text has no keyword.
    Number fields on the payload are ignored. actual, forecast, and prior
    are parsed from the post text.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    kept: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tweet_id = item.get("id")
        if not isinstance(tweet_id, str) or not _TWEET_ID.match(tweet_id):
            continue
        created = parse_time(item.get("created_at"))
        body = item.get("text")
        if created is None or not isinstance(body, str) or not body.strip():
            continue
        if window_start is not None and created < window_start:
            continue
        if window_end is not None and created > window_end:
            continue
        actual, forecast, prior = parse_release_numbers(body)
        kept.append(
            {
                "url": f"https://x.com/{HANDLE}/status/{tweet_id}",
                "created_at": created,
                "text": body,
                "actual": actual,
                "forecast": forecast,
                "prior": prior,
                "component": release_component(body),
            }
        )
    return kept


def estimate_cost_usd(posts_returned) -> float | None:
    """Posts returned times $0.005. None when the call never got a response."""
    if posts_returned is None:
        return None
    return round(int(posts_returned) * COST_PER_POST, 6)


def stop_reason(http_status, body: str) -> str | None:
    """'rate' on HTTP 429, 'spend' on a spend-cap, else None.

    A 200 body is never a stop, even if a post quotes a spend phrase.
    A plain 403 without a spend phrase is not a stop.
    """
    if http_status == 429:
        return "rate"
    if http_status is None or http_status < 400:
        return None
    text = (body or "").lower()
    if http_status == 402 or any(phrase in text for phrase in _SPEND_PHRASES):
        return "spend"
    return None


def _error_snippet(body: str) -> str:
    """JSON title, detail, and the first error message. No raw body."""
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    bits: list[str] = []
    for key in ("title", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            bits.append(value.strip())
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        message = errors[0].get("message")
        if isinstance(message, str) and message.strip():
            bits.append(message.strip())
    return "; ".join(bits)[:180]


def error_detail(http_status, body: str) -> str:
    """Short stop or HTTP label plus a JSON snippet. Never the raw body."""
    reason = stop_reason(http_status, body)
    if reason == "rate":
        prefix = "HTTP 429"
    elif reason == "spend":
        prefix = "spend cap"
    elif http_status is None:
        prefix = "error"
    else:
        prefix = f"HTTP {http_status}"
    snippet = _error_snippet(body)
    if not snippet:
        return prefix
    return f"{prefix}: {snippet}"[:300]


def search_recent(params: dict, token: str, timeout_s: float = TIMEOUT_S):
    """GET /2/tweets/search/recent. Returns (http_status, response_text, latency_ms).

    The token is sent as a header and is not logged.
    """
    started = time.perf_counter()
    response = requests.get(
        SEARCH_URL,
        headers={"Authorization": "Bearer " + token},
        params=params,
        timeout=timeout_s,
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    return response.status_code, response.text or "", latency_ms


def main() -> int:
    """Print the last 10 FirstSquawk posts. Not called by the bot."""
    token = (os.environ.get("X_BEARER_TOKEN") or "").strip()
    if not token:
        print("disabled: no token")
        return 0
    try:
        http, text, _latency = search_recent(probe_params(), token)
    except Exception as exc:
        print(f"request failed: {type(exc).__name__}")
        return 1
    payload = None
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    if http != 200:
        print(error_detail(http, text))
        returned = posts_returned_count(payload)
        cost = estimate_cost_usd(returned)
        print(f"posts={returned} estimated cost=${cost:.6f}")
        return 1
    if payload is None:
        print("not json")
        return 1
    returned = posts_returned_count(payload)
    posts = posts_from_search(payload, None, None)
    for post in posts:
        print(post["url"])
        print(utc_iso(post["created_at"]))
        print(post["text"])
        print()
    cost = estimate_cost_usd(returned)
    print(f"posts={returned} estimated cost=${cost:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
