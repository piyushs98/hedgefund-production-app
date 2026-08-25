"""
Earnings blackout — no new entries around a print; optional flatten of
open lots whose expiry spans the report.

Calendar sources (later wins per ticker):
  1. innovation_data EARNINGS rows from earnings_calendar_scraper
     (night harvest, "Corporate Earnings Scheduled for YYYY-MM-DD")
  2. EARNINGS_BLACKOUT env — comma-separated TICKER:YYYY-MM-DD,
     manual override for that ticker

Window (inclusive, ET session date):
  print − BLACKOUT_DAYS_BEFORE  ..  print + BLACKOUT_DAYS_AFTER
  defaults 1 / 1 → NVDA:2026-08-26 blocks Aug 25, 26, 27.

Existing positions: EARNINGS_FLATTEN_SPANNING (default True) closes an
open lot once the session is inside the window AND expiry >= print.
False = block new entries only, hold what is already on.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import config

try:
    import pytz
except ImportError:  # pragma: no cover
    pytz = None

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# ticker -> print date. None = not loaded yet.
_calendar: dict[str, date] | None = None
# ticker -> "env" | "scraper"
_sources: dict[str, str] = {}
# First blackout-check exception this process → Discord CRITICAL.
_blackout_check_alerted: bool = False


def reset_for_tests() -> None:
    """Drop the in-process calendar so the next load sees patched env/DB."""
    global _calendar, _sources, _blackout_check_alerted
    _calendar = None
    _sources = {}
    _blackout_check_alerted = False


def set_calendar_for_tests(
    cal: dict[str, date] | None,
    sources: dict[str, str] | None = None,
) -> None:
    """Pin a calendar without touching SQLite / env. Tests only."""
    global _calendar, _sources
    if cal is None:
        _calendar = None
        _sources = {}
        return
    _calendar = {str(k).upper().strip(): v for k, v in cal.items() if v}
    _sources = {
        str(k).upper().strip(): str(v)
        for k, v in (sources or {}).items()
    }


def parse_print_date(raw: Any) -> date | None:
    """Pull a YYYY-MM-DD out of a scraper string, datetime, or date."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def parse_env_overrides(raw: str | None = None) -> dict[str, date]:
    """
    EARNINGS_BLACKOUT=NVDA:2026-08-26,AAPL:2026-09-01
    Bad pairs are skipped with a warning.
    """
    text = raw if raw is not None else os.environ.get("EARNINGS_BLACKOUT", "")
    out: dict[str, date] = {}
    if text is None or str(text).strip() == "":
        return out
    for part in str(text).split(","):
        bit = part.strip()
        if not bit:
            continue
        if ":" not in bit:
            print(f"[Earnings] skip malformed override {bit!r} (want TICKER:YYYY-MM-DD)")
            continue
        ticker, _, rest = bit.partition(":")
        key = ticker.upper().strip()
        parsed = parse_print_date(rest)
        if not key or parsed is None:
            print(f"[Earnings] skip malformed override {bit!r} (want TICKER:YYYY-MM-DD)")
            continue
        out[key] = parsed
    return out


def _scraper_calendar() -> dict[str, date]:
    """Latest EARNINGS innovation row per ticker (newest timestamp wins)."""
    out: dict[str, date] = {}
    try:
        from news_memory import list_innovation_data
        rows = list_innovation_data(source_tag="EARNINGS", days=120)
    except Exception as e:
        print(f"[Earnings] scraper calendar unavailable: {e}")
        return out
    # rows are newest-first; first valid date per ticker is the latest write
    for _ts, ticker, _tag, content in rows:
        key = str(ticker or "").upper().strip()
        if not key or key in out:
            continue
        parsed = parse_print_date(content)
        if parsed is None:
            continue
        out[key] = parsed
    return out


def load_calendar(*, force: bool = False) -> dict[str, date]:
    """
    Scraper first, env override second. Cached until reset_for_tests /
    set_calendar_for_tests / force=True.
    """
    global _calendar, _sources
    if _calendar is not None and not force:
        return _calendar
    merged: dict[str, date] = {}
    sources: dict[str, str] = {}
    for ticker, dt in _scraper_calendar().items():
        merged[ticker] = dt
        sources[ticker] = "scraper"
    for ticker, dt in parse_env_overrides().items():
        merged[ticker] = dt
        sources[ticker] = "env"
    _calendar = merged
    _sources = sources
    return merged


def calendar_source(ticker: str) -> str | None:
    load_calendar()
    return _sources.get(str(ticker).upper().strip())


def session_date_for(when: date | datetime | None = None) -> date:
    """America/New_York session date (equity calendar). Naive → .date()."""
    if when is None:
        if pytz is None:
            return datetime.now().date()
        return datetime.now(pytz.timezone("America/New_York")).date()
    if isinstance(when, datetime):
        if when.tzinfo is not None and pytz is not None:
            return when.astimezone(pytz.timezone("America/New_York")).date()
        return when.date()
    return when


def print_date_for(ticker: str) -> date | None:
    cal = load_calendar()
    return cal.get(str(ticker).upper().strip())


def blackout_window(print_on: date) -> tuple[date, date]:
    before = max(0, int(getattr(config, "BLACKOUT_DAYS_BEFORE", 1)))
    after = max(0, int(getattr(config, "BLACKOUT_DAYS_AFTER", 1)))
    return print_on - timedelta(days=before), print_on + timedelta(days=after)


def is_blacked_out(
    ticker: str,
    when: date | datetime | None = None,
) -> bool:
    """True when new entries on this ticker must not open."""
    print_on = print_date_for(ticker)
    if print_on is None:
        return False
    sess = session_date_for(when)
    start, end = blackout_window(print_on)
    return start <= sess <= end


def should_flatten_trade(
    trade: dict[str, Any],
    sess: date | None = None,
) -> bool:
    """
    Close an open lot whose expiry spans the print, once the session is
    inside the blackout window. Off when EARNINGS_FLATTEN_SPANNING is false.
    """
    if not bool(getattr(config, "EARNINGS_FLATTEN_SPANNING", True)):
        return False
    ticker = str((trade or {}).get("ticker") or "").upper().strip()
    if not ticker:
        return False
    print_on = print_date_for(ticker)
    if print_on is None:
        return False
    day = sess or session_date_for()
    start, end = blackout_window(print_on)
    if not (start <= day <= end):
        return False
    exp_raw = trade.get("expiration")
    if not exp_raw and isinstance(trade.get("option_contract"), dict):
        exp_raw = trade["option_contract"].get("expiration")
    exp = parse_print_date(exp_raw)
    if exp is None:
        return False
    return exp >= print_on


def log_config() -> None:
    """Boot log: resolved prints, windows, flatten policy."""
    cal = load_calendar(force=True)
    before = int(getattr(config, "BLACKOUT_DAYS_BEFORE", 1))
    after = int(getattr(config, "BLACKOUT_DAYS_AFTER", 1))
    flatten = bool(getattr(config, "EARNINGS_FLATTEN_SPANNING", True))
    env_raw = (os.environ.get("EARNINGS_BLACKOUT") or "").strip() or "(unset)"
    print(
        f"[Earnings] BLACKOUT_DAYS_BEFORE={before} "
        f"BLACKOUT_DAYS_AFTER={after} "
        f"EARNINGS_FLATTEN_SPANNING={flatten} "
        f"EARNINGS_BLACKOUT={env_raw}"
    )
    if not cal:
        print("[Earnings] calendar empty — no ticker blacked out")
        return
    for ticker in sorted(cal):
        print_on = cal[ticker]
        start, end = blackout_window(print_on)
        src = _sources.get(ticker, "?")
        print(
            f"[Earnings]   {ticker} print={print_on.isoformat()} "
            f"({src}) blackout={start.isoformat()}..{end.isoformat()}"
        )


def alert_blackout_check_failed(ticker: str, err: BaseException) -> None:
    """
    Fail-closed lookup: ticker already blocked at the gate.
    Discord CRITICAL once per process so a thrown calendar cannot admit
    silently into a print.
    """
    global _blackout_check_alerted
    print(f"[Earnings] blackout check failed ticker={ticker}: {err}")
    if _blackout_check_alerted:
        return
    _blackout_check_alerted = True
    msg = (
        f"🚨 **CRITICAL: BLACKOUT CHECK FAILED** `{ticker}` — "
        f"{type(err).__name__}: {err}. "
        "Ticker blocked (fail closed). Would rather miss a trade than "
        "admit into an earnings gap on a lookup error."
    )
    print(f"[Earnings] {msg}")
    try:
        import broadcaster
        delivered = broadcaster.send_discord_alert(msg)
        print(f"[Earnings] blackout-check CRITICAL delivered={delivered}")
    except Exception as send_err:
        print(f"[Earnings] blackout-check CRITICAL send failed: {send_err}")
