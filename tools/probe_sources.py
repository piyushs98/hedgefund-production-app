"""One-shot connectivity probe. Read-only. Not imported by the bot.

Each source gets one HTTP call, 10s timeout, User-Agent
HedgefundPaperBot/1.0 (personal research). BLS may make one extra call
when the first response is HTTP 403. The bearer token is never printed.
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
import yfinance as yf
from requests.adapters import HTTPAdapter

USER_AGENT = "HedgefundPaperBot/1.0 (personal research)"
TIMEOUT_S = 10

FED_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
BLS_URL = "https://www.bls.gov/feed/bls_latest.rss"
TREASURY_URL = "https://home.treasury.gov/news/press-releases"
X_URL = "https://api.x.com/2/tweets/search/recent"

# Live scanner, read from the source on 2026-11-14. This probe does not
# import those modules and does not call them.
SCANNER = (
    "data_engineer.fetch_options_data supplies option chains to the "
    "master_bot scan. data_engineer.fetch_contract_quote supplies "
    "contract quotes to the exit pass in midday_delta. Both call "
    "yfinance.Ticker (option_chain, and history for the underlying) "
    "through yf_client.SESSION. uses yfinance: yes"
)


class _TimeoutAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        kwargs["timeout"] = TIMEOUT_S
        return super().send(request, **kwargs)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    adapter = _TimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _redact(text: str, secret: str = "") -> str:
    if not text:
        return ""
    out = " ".join(text.split())
    if secret:
        out = out.replace(secret, "[redacted]")
    out = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", out, flags=re.I)
    return out[:240]


def _local(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _parse_when(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rss(body: str) -> tuple[int, str]:
    """Item count and newest timezone-aware publish time. Empty if not RSS."""
    if not body:
        return 0, ""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return 0, ""
    if _local(root.tag) not in ("rss", "RDF", "feed"):
        return 0, ""
    count = 0
    newest: datetime | None = None
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        count += 1
        for child in list(node):
            if _local(child.tag) not in ("pubDate", "published", "updated", "date"):
                continue
            moment = _parse_when(child.text or "")
            if moment is not None and (newest is None or moment > newest):
                newest = moment
    return count, _iso(newest)


def press_feed_links(html: str) -> list[str]:
    """Press-release feed URLs linked from a Treasury page.

    home.treasury.gov/rss.xml is the general site feed, not the press list,
    so a bare /rss or /rss.xml link does not count.
    """
    found: list[str] = []
    for href in re.findall(r"""href\s*=\s*['"]([^'"]+)['"]""", html or "", re.I):
        low = href.lower().split("?", 1)[0].rstrip("/")
        if "rss" not in low and "atom" not in low and not low.endswith(".xml"):
            continue
        if low.endswith("/rss") or low.endswith("/rss.xml"):
            continue
        if "press" not in low and "/news/" not in low:
            continue
        if href not in found:
            found.append(href)
    return found


def _row(source, url, status, latency_ms, items, newest, error, extra=""):
    return {
        "source": source,
        "url": url,
        "http": "" if status is None else str(status),
        "latency_ms": "" if latency_ms is None else str(latency_ms),
        "items": "" if items is None else str(items),
        "newest": newest or "",
        "error": error or "",
        "extra": extra or "",
    }


def _request(session, url, headers=None, params=None):
    started = time.perf_counter()
    try:
        response = session.get(url, headers=headers, params=params, timeout=TIMEOUT_S)
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return None, "", latency, type(exc).__name__
    latency = int((time.perf_counter() - started) * 1000)
    return response, response.text or "", latency, ""


def probe_rss(session, source, url, headers=None) -> dict:
    response, body, latency, err = _request(session, url, headers=headers)
    if response is None:
        return _row(source, url, None, latency, 0, "", err)
    items, newest = 0, ""
    error = ""
    if response.status_code == 200:
        root = _xml_root(body)
        if root is None or _local(root.tag) not in ("rss", "RDF", "feed"):
            error = "not rss"
        else:
            items, newest = parse_rss(body)
    else:
        error = _redact(body[:180]) or f"HTTP {response.status_code}"
    return _row(source, url, response.status_code, latency, items, newest, error)


def _xml_root(body: str):
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def probe_bls(session) -> list[dict]:
    first = probe_rss(session, "bls", BLS_URL)
    rows = [first]
    if first["http"] == "403":
        rows.append(probe_rss(
            session,
            "bls retry",
            BLS_URL,
            headers={"Accept": "application/rss+xml"},
        ))
    return rows


def probe_treasury(session) -> dict:
    response, body, latency, err = _request(session, TREASURY_URL)
    if response is None:
        return _row("treasury", TREASURY_URL, None, latency, "", "", err)
    links = press_feed_links(body) if response.status_code == 200 else []
    if response.status_code != 200:
        return _row(
            "treasury",
            TREASURY_URL,
            response.status_code,
            latency,
            "",
            "",
            _redact(body[:180]) or f"HTTP {response.status_code}",
        )
    if not links:
        return _row("treasury", TREASURY_URL, response.status_code, latency, "", "", "no feed")
    return _row(
        "treasury",
        links[0],
        response.status_code,
        latency,
        "",
        "",
        "press feed linked from the index; not fetched",
    )


def probe_x(session) -> dict:
    token = (os.environ.get("X_BEARER_TOKEN") or "").strip()
    if not token:
        return _row("x", X_URL, None, None, "", "", "disabled: no token")
    response, body, latency, err = _request(
        session,
        X_URL,
        headers={"Authorization": "Bearer " + token},
        params={
            "query": "from:FirstSquawk -is:retweet",
            "max_results": 10,
            "tweet.fields": "created_at",
        },
    )
    if response is None:
        return _row("x", X_URL, None, latency, 0, "", _redact(err, token))
    limit = response.headers.get("x-rate-limit-limit", "")
    remaining = response.headers.get("x-rate-limit-remaining", "")
    extra = f"x-rate-limit-limit={limit or '-'} x-rate-limit-remaining={remaining or '-'}"
    items = 0
    newest = ""
    error = ""
    payload = None
    try:
        payload = json.loads(body) if body else None
    except json.JSONDecodeError:
        payload = None
        error = "not json"
    if response.status_code == 200 and isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("result_count"), int):
            items = meta["result_count"]
        elif isinstance(payload.get("data"), list):
            items = len(payload["data"])
        data = payload.get("data")
        if isinstance(data, list):
            moments = []
            for item in data:
                if isinstance(item, dict):
                    moment = _parse_when(str(item.get("created_at") or ""))
                    if moment is not None:
                        moments.append(moment)
            if moments:
                newest = _iso(max(moments))
    elif response.status_code != 200 and not error:
        error = _redact(_x_error(payload, body), token) or f"HTTP {response.status_code}"
    return _row("x", X_URL, response.status_code, latency, items, newest, error, extra)


def _x_error(payload, body: str) -> str:
    if isinstance(payload, dict):
        bits = []
        for key in ("title", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                bits.append(value.strip())
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            message = errors[0].get("message")
            if isinstance(message, str) and message.strip():
                bits.append(message.strip())
        if bits:
            return "; ".join(bits)
    return ""


def probe_yahoo(session) -> list[dict]:
    """One fast_info.last_price per symbol. No news method is called."""
    seen = {"n": 0, "last_url": "", "last_status": None}

    def _hook(response, *_args, **_kwargs):
        seen["n"] += 1
        seen["last_url"] = response.url
        seen["last_status"] = response.status_code
        return response

    session.hooks["response"].append(_hook)
    rows = []
    for symbol in ("SPY", "ES=F"):
        seen["n"] = 0
        seen["last_url"] = ""
        seen["last_status"] = None
        started = time.perf_counter()
        price = None
        error = ""
        try:
            ticker = yf.Ticker(symbol, session=session)
            price = ticker.fast_info.last_price
        except Exception as exc:
            error = _redact(type(exc).__name__)
        latency = int((time.perf_counter() - started) * 1000)
        items = 1 if price is not None else 0
        if price is None and not error:
            error = "no last price"
        note = ""
        if seen["n"] and seen["n"] != 1:
            note = f"yfinance issued {seen['n']} http responses"
        if error and note:
            error = f"{error}; {note}"
        elif note:
            error = note
        rows.append(_row(
            f"yahoo {symbol}",
            seen["last_url"] or f"yfinance.Ticker({symbol}).fast_info.last_price",
            seen["last_status"],
            latency,
            items,
            "",
            error,
            f"yfinance {yf.__version__}",
        ))
    return rows


def _print_table(rows: list[dict]) -> None:
    headers = ["source", "url", "http", "latency_ms", "items", "newest", "error"]
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        cells = [_cell(row[name]) for name in headers]
        print("| " + " | ".join(cells) + " |")
        if row.get("extra"):
            print(f"  {row['source']}: {row['extra']}")


def _cell(value: str) -> str:
    return (value or "").replace("|", "/")


def main() -> None:
    session = _session()
    rows = [
        probe_rss(session, "fed", FED_URL),
        *probe_bls(session),
        probe_treasury(session),
        probe_x(session),
        *probe_yahoo(session),
    ]
    _print_table(rows)
    print(f"yfinance {yf.__version__}")
    print(SCANNER)


if __name__ == "__main__":
    main()
