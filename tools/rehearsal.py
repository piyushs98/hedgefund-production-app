"""Isolated fast-clock rehearsal for Monday 2026-11-16.

Does not stop the live bot and does not edit a stamped module. Databases
are copies. Discord is a local file. Heartbeat and the X API are off.
One Yahoo chain snapshot is taken up front; every later quote is that
snapshot with the scenario overlay applied.

The session clock advances on the scheduler's own sleeps (the 60-second
chunks). The 5-second gap between names and the 0.5-second gap between
exit quotes do not move it: those exist to pace Yahoo, and this run has
no second Yahoo call. With that, the live 5-minute / 30-minute math
lands on 08:45, 09:15, 13:45, 14:15, 14:45, and 14:55 CT.

One harness-only monotonic shift inserts a full scan at 11:30 CT.
A second shift inserts the scheduled 11:45 CT scan, and the later
scans stay on 12:15. The clock then runs into night mode
so the reconciliation can write SESSION_FINAL. The 14:45 SESSION line
is left as the live exit code wrote it. Copied rows for this session
day are removed first, so that line is the simulated book and not the
live SESSION already stored in the database.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CHICAGO = ZoneInfo("America/Chicago")
UTC = timezone.utc
SESSION_DAY = date(2026, 11, 16)
START = datetime(2026, 11, 16, 8, 15, tzinfo=CHICAGO)
SAFETY = datetime(2026, 11, 16, 16, 0, tzinfo=CHICAGO)
EXPIRY = "2026-11-20"
FAKE_EVENT_ID = "rehearsal-ppi-2026-11-16"
CPI_EVENT_ID = "rehearsal-cpi-2026-11-16"

# First print, then the admit print. MSFT's 13:45 print is only the first
# persistence cycle, so the 14:15 print is the one the live gate can block
# as late_session.
HIGH_SLOTS = {
    "SPY": {(8, 45), (9, 15), (9, 45)},
    "QQQ": {
        (10, 15), (10, 45), (11, 15), (11, 30), (11, 45), (12, 15), (12, 45),
        (13, 15), (13, 45), (14, 15), (14, 45),
    },
    "IWM": {
        (11, 15), (11, 30), (11, 45), (12, 15), (12, 45),
        (13, 15), (13, 45), (14, 15), (14, 45),
    },
    "AAPL": {(13, 15), (13, 45), (14, 15), (14, 45)},
    "MSFT": {(13, 45), (14, 15)},
    # 11:15 is the first print. 11:30 admits. 11:45 and 12:15 keep the
    # score at or above 80 so the live time-stop exempt still applies.
    # The quote crash at 12:30 is the close.
    "META": {(11, 15), (11, 30), (11, 45), (12, 15)},
}

PPI_POSTS = (
    (
        "2011111600000000001",
        "2026-11-16T17:01:00.000Z",
        "US PPI (MOM) (OCT)ACTUAL: 0.3% VS 1.3% PREVIOUS; EST 0.4%",
    ),
    (
        "2011111600000000002",
        "2026-11-16T17:01:05.000Z",
        "US PPI (YOY) (OCT)ACTUAL: 6.5% VS 6.3% PREVIOUS; EST 6.6%",
    ),
    (
        "2011111600000000003",
        "2026-11-16T17:01:10.000Z",
        "US CORE PPI (MOM) (OCT)ACTUAL: 0.3% VS 0.7% PREVIOUS; EST 0.3%",
    ),
    (
        "2011111600000000004",
        "2026-11-16T17:01:15.000Z",
        "US CORE PPI (YOY) (OCT)ACTUAL: 5.0% VS 5.0% PREVIOUS; EST 5.0%",
    ),
)

CPI_POSTS = (
    (
        "2011111600000000011",
        "2026-11-16T18:01:00.000Z",
        "US CPI (MOM) (OCT)ACTUAL: 0.2% VS 0.5% PREVIOUS; EST 0.4%",
    ),
    (
        "2011111600000000012",
        "2026-11-16T18:01:05.000Z",
        "US CPI (YOY) (OCT)ACTUAL: 3.1% VS 3.0% PREVIOUS; EST 3.2%",
    ),
    (
        "2011111600000000013",
        "2026-11-16T18:01:10.000Z",
        "US CORE CPI (MOM) (OCT)ACTUAL: 0.3% VS 0.3% PREVIOUS; EST 0.3%",
    ),
    (
        "2011111600000000014",
        "2026-11-16T18:01:15.000Z",
        "US CORE CPI (YOY) (OCT)ACTUAL: 3.0% VS 2.9% PREVIOUS; EST 3.0%",
    ),
)


class Clock:
    def __init__(self) -> None:
        self.ct = START
        self.mono = 10_000.0
        self.pacing_s = 0.0
        self.shadow = False
        self.latch_at: list[str] = []
        self._last_mark = ""
        self.stop_now = False
        self.nudged_forward = False
        self.nudged_back = False

    def wall(self, tz=None) -> datetime:
        if tz is None:
            return self.ct.replace(tzinfo=None)
        return self.ct.astimezone(tz)

    def note(self) -> None:
        mark = self.ct.strftime("%H:%M")
        if mark == self._last_mark:
            return
        if self.ct.minute in (0, 15, 30, 45):
            print(f"[rehearsal] clock {self.ct.strftime('%H:%M')} CT", file=sys.stderr)
            self._last_mark = mark


CLOCK = Clock()
REAL_DATETIME = datetime
NETWORK_BLOCKED: list[str] = []
DISCORD_PATH = ""
LOG_PATH = ""
SNAPSHOT: dict = {}
CHAOS = None


class Chaos:
    """One fault window on a fresh copy of the session. None on the base run."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.high_slots = None
        self.yahoo_down: list[tuple[time, time]] = []
        self.yahoo_429: list[tuple[time, time]] = []
        self.discord_down: list[tuple[time, time]] = []
        self.discord_500: list[tuple[time, time]] = []
        self.heartbeat_down: list[tuple[time, time]] = []
        self.dark: list[tuple[time, time]] = []
        self.spot_drop_after: time | None = None
        self.crash_at: time | None = None
        self.stop_at: time | None = None
        self.keep_running_at_night = False
        self.real_discord = False
        self.start: datetime | None = None
        self.delivered: list[str] = []


def _local_time(moment: datetime) -> time:
    return moment.astimezone(CHICAGO).time().replace(tzinfo=None)


def _in_windows(moment: datetime, windows: list[tuple[time, time]]) -> bool:
    if not windows:
        return False
    local = _local_time(moment)
    return any(start <= local < end for start, end in windows)


def _past(moment: datetime, mark: time | None) -> bool:
    if mark is None:
        return False
    local = _local_time(moment)
    return local >= mark


def _copy_db(src: Path, dst: Path) -> None:
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _prepare_env(tmp: Path) -> tuple[Path, Path]:
    news = tmp / "news_room.db"
    hedge = tmp / "hedge_fund.db"
    trades = tmp / "active_trades.json"
    _copy_db(ROOT / "data" / "news_room.db", news)
    _copy_db(ROOT / "data" / "hedge_fund.db", hedge)
    live_trades = ROOT / "active_trades.json"
    trades.write_text(live_trades.read_text(encoding="utf-8") if live_trades.exists() else "[]")
    os.environ["NEWS_DB_PATH"] = str(news)
    os.environ["HEDGE_DB_PATH"] = str(hedge)
    os.environ["ACTIVE_TRADES_PATH"] = str(trades)
    os.environ["SESSION_BASELINE_PATH"] = str(tmp / "session_baseline.json")
    os.environ["DISCORD_WEBHOOK"] = ""
    os.environ["HEARTBEAT_URL"] = ""
    os.environ["X_BEARER_TOKEN"] = "rehearsal"
    os.environ["GEMINI_API_KEY"] = "rehearsal"
    os.environ.pop("FULL_LLM_INTRADAY", None)
    os.environ.pop("BYPASS_MARKET_HOURS", None)
    os.environ["RESET_LEDGER_ON_BOOT"] = "false"
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    _forget_session_day(news)
    return news, hedge


def _forget_session_day(news: Path) -> None:
    """Drop this session's live rows from the copy.

    The 14:45 writer leaves an existing session row alone, and night
    reconciliation leaves a complete SESSION_FINAL alone. The copied
    database already has both for 2026-11-16, plus that day's trades
    and any CRITICAL outbox rows, so the report would grade the live
    book. The live files are not opened here.
    """
    day = SESSION_DAY.isoformat()
    conn = sqlite3.connect(news)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "session_lines" in names:
            conn.execute(
                "DELETE FROM session_lines WHERE session_date = ?",
                (day,),
            )
        if "shadow_close_tags" in names:
            conn.execute(
                "DELETE FROM shadow_close_tags WHERE session_date = ?",
                (day,),
            )
        if "session_marks" in names:
            conn.execute(
                "DELETE FROM session_marks WHERE session_date = ?",
                (day,),
            )
        dropped = 0
        if "trade_history" in names:
            rows = conn.execute(
                "SELECT id, closed_at FROM trade_history"
            ).fetchall()
            drop = [row[0] for row in rows if _ct(row[1]).startswith(day)]
            dropped = len(drop)
            if drop:
                conn.executemany(
                    "DELETE FROM trade_history WHERE id = ?",
                    [(item,) for item in drop],
                )
        if "discord_outbox" in names:
            rows = conn.execute(
                "SELECT id, created_at FROM discord_outbox"
            ).fetchall()
            drop = [row[0] for row in rows if _ct(row[1]).startswith(day)]
            if drop:
                conn.executemany(
                    "DELETE FROM discord_outbox WHERE id = ?",
                    [(item,) for item in drop],
                )
        conn.commit()
    finally:
        conn.close()
    print(
        f"[rehearsal] cleared {dropped} copied trades and the stored "
        f"session rows for {day}",
        file=sys.stderr,
    )


def _install_clock() -> None:
    import time as time_mod

    # Leave datetime.datetime itself alone. Pandas rejects a swapped
    # datetime class. Only the app modules' imported names are replaced,
    # and only after the one Yahoo snapshot has finished.
    real = REAL_DATETIME

    class Patched(real):
        @classmethod
        def _stamp(cls, wall: real) -> "Patched":
            # Instances must be this subclass. The earnings check uses
            # isinstance(when, datetime) against the module's datetime name,
            # and a base datetime fails that test once the name is Patched.
            if wall.tzinfo is None:
                return cls(
                    wall.year, wall.month, wall.day,
                    wall.hour, wall.minute, wall.second, wall.microsecond,
                )
            utc = wall.astimezone(UTC)
            base = cls(
                utc.year, utc.month, utc.day,
                utc.hour, utc.minute, utc.second, utc.microsecond,
                tzinfo=UTC,
            )
            return base.astimezone(wall.tzinfo)

        @classmethod
        def now(cls, tz=None):
            return cls._stamp(CLOCK.wall(tz))

        @classmethod
        def utcnow(cls):
            return cls._stamp(CLOCK.wall(UTC).replace(tzinfo=None))

    def in_repo(mod) -> bool:
        path = str(getattr(mod, "__file__", "") or "")
        if path.endswith("tools/rehearsal.py"):
            return False
        return (
            "hedgefund-production-app" in path
            and ".venv" not in path
            and "site-packages" not in path
        )

    def sweep() -> None:
        for mod in list(sys.modules.values()):
            if not in_repo(mod):
                continue
            mapping = getattr(mod, "__dict__", None)
            if not isinstance(mapping, dict):
                continue
            if mapping.get("datetime") is real:
                mapping["datetime"] = Patched

    def fake_sleep(seconds) -> None:
        sweep()
        # Night mode sets stop_now from the reconciliation wrapper.
        # 16:00 CT is only the fallback if that call never runs, so the
        # clock cannot walk into Tuesday. 15:00 CT is still trading's
        # handoff into night mode and must not stop the loop.
        if CLOCK.stop_now or CLOCK.ct >= SAFETY:
            raise SystemExit(0)
        span = float(seconds or 0)
        if span < 30:
            CLOCK.pacing_s += span
            return
        local = CLOCK.ct.astimezone(CHICAGO)
        on_minute = local.second == 0 and local.microsecond == 0
        # +900 at 11:15 pulls the next full scan forward to 11:30.
        # +900 at 11:30 runs after that scan has stamped its mono time,
        # so the following full scan lands on 11:45. That stamp already
        # includes the nudge, and the next interval is a normal 30 minutes
        # back on 12:15. No later subtract.
        if on_minute and local.hour == 11 and local.minute == 15 and not CLOCK.nudged_forward:
            CLOCK.mono += 900.0
            CLOCK.nudged_forward = True
            print("[rehearsal] mono +900 at 11:15 CT", file=sys.stderr)
        elif (
            on_minute
            and local.hour == 11
            and local.minute == 30
            and CLOCK.nudged_forward
            and not CLOCK.nudged_back
        ):
            CLOCK.mono += 900.0
            CLOCK.nudged_back = True
            print("[rehearsal] mono +900 at 11:30 CT", file=sys.stderr)
        left = span
        while left > 1e-9:
            step = min(20.0, left)
            CLOCK.ct = CLOCK.ct + timedelta(seconds=step)
            CLOCK.mono += step
            left -= step
            CLOCK.note()
            if CLOCK.shadow:
                _shadow_tick()
            if CHAOS is not None and (
                _past(CLOCK.ct, CHAOS.crash_at) or _past(CLOCK.ct, CHAOS.stop_at)
            ):
                raise SystemExit(0)

    def fake_monotonic() -> float:
        return CLOCK.mono

    time_mod.sleep = fake_sleep
    time_mod.monotonic = fake_monotonic
    sweep()
    CLOCK._sweep = sweep  # type: ignore[attr-defined]


def _shadow_tick() -> None:
    import shadow_news
    from master_bot import NYSE_HOLIDAYS

    try:
        shadow_news.run_tick(CLOCK.ct.astimezone(UTC), NYSE_HOLIDAYS)
    except Exception as exc:
        print(f"[rehearsal] shadow tick {type(exc).__name__}: {exc}")


def _slot(moment: datetime) -> tuple[int, int]:
    local = moment.astimezone(CHICAGO)
    return local.hour, local.minute


def _high(ticker: str, moment: datetime) -> bool:
    """Keep a chaos position's score above the time-stop exempt line.

    Full scans are the only prints that store last_live_score. The
    rehearsal clock also inserts one at 11:30. A name held only on the
    half-hour would print under 80 there and time-stop while the
    scenario still needs it open. Exit minutes are included for the
    same reason. The base day is unchanged.
    """
    if CHAOS is not None and CHAOS.name == "g1":
        local = _local_time(moment)
        if ticker == "SPY" and local < time(11, 0):
            return True
        if ticker == "QQQ" and time(10, 15) <= local < time(11, 0):
            return True
        return False
    if CHAOS is not None and CHAOS.name == "j":
        if ticker == "SPY" and _local_time(moment) < time(14, 40):
            return True
        return False
    slots = HIGH_SLOTS if CHAOS is None or CHAOS.high_slots is None else CHAOS.high_slots
    return _slot(moment) in slots.get(ticker, set())


def _quote_style(ticker: str, moment: datetime) -> str:
    if CHAOS is not None:
        if _in_windows(moment, CHAOS.dark):
            return "dark"
        return "live"
    local = moment.astimezone(CHICAGO).time().replace(tzinfo=None)
    if ticker == "SPY" and local >= time(10, 0):
        return "stop"
    if ticker == "META" and local >= time(12, 30):
        return "stop"
    if ticker == "IWM" and local >= time(14, 45):
        return "dark"
    return "live"


def _plant_contract(chain: dict, spot: float) -> None:
    premium = min(5.0, max(1.25, round(spot * 0.012, 2)))
    strike = round(spot)
    mid = premium
    bid = round(mid * 0.99, 2)
    ask = round(mid * 1.01, 2)
    if ask <= bid:
        ask = round(bid + 0.05, 2)
    contract = {
        "strike": strike,
        "lastPrice": mid,
        "bid": bid,
        "ask": ask,
        "volume": 20000,
        "openInterest": 50000,
        "impliedVolatility": 0.22,
        "delta": 0.45,
    }
    chains = chain.setdefault("chains", {})
    sides = chains.get(EXPIRY)
    if not isinstance(sides, dict):
        sides = {"calls": [], "puts": []}
        chains[EXPIRY] = sides
    calls = [c for c in (sides.get("calls") or []) if isinstance(c, dict)]
    calls = [c for c in calls if abs(float(c.get("strike") or 0) - strike) > 0.2]
    calls.append(contract)
    sides["calls"] = calls
    sides.setdefault("puts", sides.get("puts") or [])
    loaded = chain.setdefault("expiries_loaded", [])
    if EXPIRY not in loaded:
        loaded.append(EXPIRY)


def _paint_quotes(chain: dict, style: str) -> None:
    if style == "live":
        return
    for sides in (chain.get("chains") or {}).values():
        if not isinstance(sides, dict):
            continue
        for rows in sides.values():
            if not isinstance(rows, list):
                continue
            for contract in rows:
                if not isinstance(contract, dict):
                    continue
                if style == "dark":
                    contract["bid"] = 0
                    contract["ask"] = 0
                    contract["lastPrice"] = 0
                else:
                    contract["bid"] = 0.30
                    contract["ask"] = 0.40
                    contract["lastPrice"] = 0.35


def _serve_chain(ticker: str, base: dict) -> dict:
    raw = json.loads(json.dumps(base[ticker]))
    spot = raw.get("current_price")
    try:
        spot_f = float(spot)
    except (TypeError, ValueError):
        return raw
    raw["current_price"] = round(spot_f, 2)
    if (
        CHAOS is not None
        and CHAOS.spot_drop_after is not None
        and _past(CLOCK.ct, CHAOS.spot_drop_after)
    ):
        raw["current_price"] = round(spot_f * 0.5, 2)
    _plant_contract(raw, spot_f)
    _paint_quotes(raw, _quote_style(str(ticker).upper(), CLOCK.ct))
    return raw


def _pivot_for(ticker: str, spot: float) -> dict:
    if _high(ticker, CLOCK.ct):
        pivot = round(spot * 0.97, 2)
        pct = 2.5
    else:
        pivot = round(spot * 0.999, 2)
        pct = 0.05
    return {
        "close": round(spot, 2),
        "pivot": pivot,
        "r1": round(pivot * 1.01, 2),
        "s1": round(pivot * 0.99, 2),
        "r2": round(pivot * 1.02, 2),
        "s2": round(pivot * 0.98, 2),
        "pct_change": pct,
        "basis_close": pivot,
        "live_from_today": True,
    }


def _install_market(base: dict) -> None:
    import data_engineer
    import master_bot
    import midday_delta
    import ticker_desk
    import yfinance as yf
    import yf_client

    def _yahoo_payload(ticker: str):
        if CHAOS is not None and _in_windows(CLOCK.ct, CHAOS.yahoo_down):
            return json.dumps({"error": "network down"})
        if CHAOS is not None and _in_windows(CLOCK.ct, CHAOS.yahoo_429):
            import yf_client
            if yf_client.crumb_backoff_remaining() <= 0:
                yf_client.note_crumb_429()
            return json.dumps({"error": "HTTP 429"})
        if CHAOS is not None and not _in_windows(CLOCK.ct, CHAOS.yahoo_429):
            import yf_client
            if yf_client.crumb_backoff_remaining() > 0:
                yf_client.note_crumb_ok()
        return json.dumps(_serve_chain(ticker, base))

    def options(ticker):
        return _yahoo_payload(str(ticker).upper())

    def quote(ticker, expiration, strike, direction="CALL"):
        return options(ticker)

    def spot(ticker):
        if CHAOS is not None and (
            _in_windows(CLOCK.ct, CHAOS.yahoo_down)
            or _in_windows(CLOCK.ct, CHAOS.yahoo_429)
        ):
            return None
        raw = base.get(str(ticker).upper()) or {}
        try:
            px = round(float(raw.get("current_price")), 2)
        except (TypeError, ValueError):
            return None
        if CHAOS is not None and _past(CLOCK.ct, CHAOS.spot_drop_after):
            return round(px * 0.5, 2)
        return px

    def pivot(ticker):
        px = spot(ticker)
        if px is None:
            return {"error": "no_pivot_data", "close": None, "pivot": None}
        return _pivot_for(str(ticker).upper(), px)

    def atr(ticker, breaker=None):
        px = spot(ticker)
        if px is None:
            return None, None
        return round(px * 0.02, 4), 2.0

    def drift(ticker, breaker=None):
        return None

    data_engineer.fetch_options_data = options
    data_engineer._fetch_options_data = options
    data_engineer.fetch_contract_quote = quote
    data_engineer._fetch_contract_quote = quote
    data_engineer.fetch_spot = spot
    midday_delta.fetch_options_data = options
    midday_delta.fetch_pivot_data = pivot
    ticker_desk.fetch_pivot_data = pivot
    master_bot.fetch_atr = atr
    master_bot.fetch_intraday_drift = drift

    def blocked_ticker(*_args, **_kwargs):
        NETWORK_BLOCKED.append("yfinance.Ticker")
        raise RuntimeError("rehearsal: Yahoo is closed after the snapshot")

    yf.Ticker = blocked_ticker
    yf_client.ticker = blocked_ticker


def _install_io() -> None:
    import broadcaster
    import first_squawk
    import llm_chain
    import requests
    import shadow_news

    def send(message, critical=False, **_kwargs):
        stamp = CLOCK.ct.strftime("%Y-%m-%d %H:%M:%S CT")
        with open(DISCORD_PATH, "a", encoding="utf-8") as handle:
            handle.write(
                f"----- {stamp} critical={bool(critical)} -----\n"
                f"{message}\n"
            )
        # The file stands in for HTTP 200/204. A CRITICAL records the
        # same delivered_at the live sender writes after Discord accepts.
        if critical and message and str(message).strip():
            import fill_accounting
            import reliability
            fill_accounting.note_critical()
            reliability.note_delivered_critical(str(message))
        return True

    def search(params, _token, timeout_s=10):
        query = str((params or {}).get("query") or "").upper()
        if "PPI" in query:
            posts = PPI_POSTS
        elif "CPI" in query:
            posts = CPI_POSTS
        else:
            posts = ()
        payload = {
            "data": [
                {"id": post_id, "text": text, "created_at": created}
                for post_id, created, text in posts
            ],
            "meta": {"result_count": len(posts)},
        }
        return 200, json.dumps(payload), 1

    def no_model(*_args, **_kwargs):
        return (
            "Rehearsal brief. The language model was not called. "
            "Monday 2026-11-16 paper session."
        )

    if CHAOS is not None and CHAOS.real_discord:
        _install_discord_transport()
    else:
        broadcaster.send_discord_alert = send
        broadcaster.WEBHOOK_URL = ""
    first_squawk.search_recent = search
    llm_chain.generate_text = no_model

    def blocked_request(self, method, url, *args, **kwargs):
        NETWORK_BLOCKED.append(f"{method} {url}"[:180])
        raise RuntimeError("rehearsal: network is closed")

    requests.sessions.Session.request = blocked_request

    def quiet_history(*_args, **_kwargs):
        return {
            "frame": None,
            "error": "rehearsal",
            "latency_ms": 0,
            "crumb429": False,
        }

    def reaction_history(symbol, start, end, prepost=True):
        # Two snapshot closes, same price. The reaction is 0.0.
        # A pandas timestamp fails the shadow clock's datetime check, so
        # the frame yields the module's datetime class directly.
        # This does not call Yahoo.
        raw = SNAPSHOT.get(str(symbol).upper()) or {}
        try:
            spot = float(raw.get("current_price"))
        except (TypeError, ValueError):
            spot = 0.0
        if spot <= 0 or start is None or end is None:
            return {
                "frame": None,
                "error": "rehearsal",
                "latency_ms": 0,
                "crumb429": False,
            }

        def _as_clock(moment):
            cls = shadow_news.datetime
            aware = moment if getattr(moment, "tzinfo", None) else moment.replace(tzinfo=UTC)
            if isinstance(aware, cls):
                return aware
            if hasattr(cls, "_stamp"):
                return cls._stamp(aware)
            return aware

        class _Tick:
            def __init__(self, moment):
                self._moment = moment

            def to_pydatetime(self):
                return _as_clock(self._moment)

        class _Frame:
            empty = False
            columns = ["Close"]

            def __init__(self, pairs):
                self._pairs = pairs

            def iterrows(self):
                for moment, close in self._pairs:
                    yield _Tick(moment), {"Close": close}

        frame = _Frame(((_as_clock(start), spot), (_as_clock(end), spot)))
        return {
            "frame": frame,
            "error": None,
            "latency_ms": 0,
            "crumb429": False,
        }

    shadow_news._tnx_history = quiet_history
    shadow_news._symbol_history = reaction_history

    real_start = shadow_news.start_shadow_thread

    def start_later(holidays) -> None:
        CLOCK.shadow = True
        print("[Shadow] rehearsal clock will call run_tick; no wall-clock thread")
        _ = real_start
        _ = holidays

    shadow_news.start_shadow_thread = start_later


def _seed_event(news_path: Path) -> None:
    import shadow_news

    shadow_news.ensure_schema(str(news_path))
    rows = (
        (
            FAKE_EVENT_ID,
            "Producer Price Index for October 2026",
            "2026-11-16T17:00:00+00:00",
            '["PPI"]',
        ),
        (
            CPI_EVENT_ID,
            "Consumer Price Index for October 2026",
            "2026-11-16T18:00:00+00:00",
            '["CPI"]',
        ),
    )
    conn = sqlite3.connect(news_path)
    try:
        for event_id, name, release_at, keywords in rows:
            conn.execute(
                """
                INSERT INTO calendar_events
                    (event_id, name, release_at, keywords, kind, ticker,
                     tier, tz_source, window_after_min)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL)
                ON CONFLICT(event_id) DO UPDATE SET
                    name=excluded.name,
                    release_at=excluded.release_at,
                    keywords=excluded.keywords,
                    kind=excluded.kind,
                    tier=excluded.tier,
                    tz_source=excluded.tz_source
                """,
                (event_id, name, release_at, keywords, "macro", 1, "rehearsal"),
            )
        conn.commit()
    finally:
        conn.close()


def _watch_latch() -> None:
    import position_exits

    real = position_exits.mark_eod_done

    def wrapped(session_day=None):
        CLOCK.latch_at.append(CLOCK.ct.strftime("%Y-%m-%d %H:%M:%S CT"))
        return real(session_day)

    position_exits.mark_eod_done = wrapped


def _arm_night_stop() -> None:
    """End the simulated day once night mode has reconciled."""
    import shadow_reconcile

    real = shadow_reconcile.reconcile_session

    def wrapped(now, holidays=(), **kwargs):
        try:
            return real(now, holidays=holidays, **kwargs)
        finally:
            if CHAOS is None or not CHAOS.keep_running_at_night:
                CLOCK.stop_now = True
            print(
                f"[rehearsal] night reconciliation returned at "
                f"{CLOCK.ct.strftime('%H:%M:%S')} CT",
                file=sys.stderr,
            )

    shadow_reconcile.reconcile_session = wrapped


def _snapshot(tickers: list[str]) -> dict:
    import data_engineer

    out = {}
    for ticker in tickers:
        raw = data_engineer.fetch_options_data(ticker)
        parsed = json.loads(raw)
        price = parsed.get("current_price")
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            price_f = None
        n_exp = len(parsed.get("chains") or {})
        print(
            f"[rehearsal] snapshot {ticker} price={price} expiries={n_exp}",
            file=sys.stderr,
        )
        if price_f is None or price_f <= 0 or not parsed.get("chains"):
            raise SystemExit(f"snapshot for {ticker} has no chain ({parsed.get('error')})")
        out[ticker] = parsed
    return out


def _ct(raw: str | None) -> str:
    if not raw:
        return ""
    text = str(raw).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return str(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(CHICAGO).strftime("%Y-%m-%d %H:%M:%S CT")


def _report(news: Path, log_text: str) -> int:
    import tracker_agent

    conn = sqlite3.connect(f"file:{news}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    scan_marks = []
    critical_rows = []
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(trade_history)")]
        has_fill = "fill_est" in cols
        fill_sql = "fill_est" if has_fill else "0 AS fill_est"
        rows = conn.execute(
            f"""
            SELECT id, closed_at, ticker, direction, strike, expiration,
                   entry_price, exit_price, notes, {fill_sql}
            FROM trade_history
            WHERE closed_at LIKE '2026-11-16%'
            ORDER BY id
            """,
        ).fetchall()
        sessions = []
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "session_lines" in names:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(session_lines)")
            }
            if "kind" in cols:
                sessions = conn.execute(
                    """
                    SELECT session_date, kind, line, stored_at, incomplete
                    FROM session_lines
                    WHERE session_date = ?
                    ORDER BY kind
                    """,
                    (SESSION_DAY.isoformat(),),
                ).fetchall()
            else:
                sessions = conn.execute(
                    """
                    SELECT session_date, line, stored_at
                    FROM session_lines
                    WHERE session_date = ?
                    ORDER BY stored_at
                    """,
                    (SESSION_DAY.isoformat(),),
                ).fetchall()
        items = []
        if "shadow_items" in names:
            items = conn.execute(
                """
                SELECT event_id, component, actual, forecast, prior, published_at
                FROM shadow_items
                WHERE event_id IN (?, ?)
                ORDER BY event_id, published_at
                """,
                (FAKE_EVENT_ID, CPI_EVENT_ID),
            ).fetchall()
        close_tags = []
        if "shadow_close_tags" in names:
            close_tags = conn.execute(
                """
                SELECT trade_id, ticker, event_id, event_name, actual, forecast,
                       prior, surprise, label, reaction_spy, reaction_qqq
                FROM shadow_close_tags
                WHERE session_date = ?
                ORDER BY ticker, event_id
                """,
                (SESSION_DAY.isoformat(),),
            ).fetchall()
        if "session_marks" in names:
            scan_marks = conn.execute(
                """
                SELECT at_ct FROM session_marks
                WHERE session_date = ? AND kind = 'scan'
                ORDER BY at_ct
                """,
                (SESSION_DAY.isoformat(),),
            ).fetchall()
        if "discord_outbox" in names:
            crit_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(discord_outbox)")
            }
            if {"body", "delivered_at", "critical"} <= crit_cols:
                critical_rows = conn.execute(
                    """
                    SELECT body, created_at, delivered_at
                    FROM discord_outbox
                    WHERE critical = 1
                    ORDER BY id
                    """
                ).fetchall()
    finally:
        conn.close()

    open_json = tracker_agent.load_active_trades()
    discord = Path(DISCORD_PATH).read_text(encoding="utf-8") if Path(DISCORD_PATH).exists() else ""

    admits = []
    blocked = []
    scores = []
    tags = []
    close_lines = []
    for line in log_text.splitlines():
        if "GATE [" in line or "Gate blocked" in line or "Gate admitted" in line:
            admits.append(line.strip())
        if "late_session" in line or "🚧 Gate blocked" in line:
            blocked.append(line.strip())
        if "⚙️ score" in line or "score subs" in line:
            scores.append(line.strip())
        if "[Shadow] context" in line:
            tags.append(line.strip())
        if "[Shadow] close " in line:
            close_lines.append(line.strip())

    def score_at(ticker: str, hhmm: str) -> str:
        needle = f"[{ticker}]"
        want = f"{hhmm}"
        # The score line does not carry the clock. Pair it with the scan banner.
        current = ""
        found = ""
        for line in log_text.splitlines():
            if "30-MIN SCAN" in line and "CT" in line:
                current = line
            if needle in line and "⚙️ score" in line and want in current:
                found = line.strip()
        return found

    def gate_at(hhmm: str) -> str:
        current = ""
        found = ""
        for line in log_text.splitlines():
            if "30-MIN SCAN" in line and "CT" in line:
                current = line
            if "[scan] GATE [" in line and hhmm in current:
                found = line.strip()
        return found

    spy_stop = [
        row for row in rows
        if row["ticker"] == "SPY" and "STOP_LOSS" in str(row["notes"] or "")
    ]
    aapl_flat = [
        row for row in rows
        if row["ticker"] == "AAPL" and str(row["notes"] or "") == "EXIT:EOD_FLATTEN"
    ]
    iwm_est = [
        row for row in rows
        if row["ticker"] == "IWM" and "EOD_FLATTEN_ESTIMATED" in str(row["notes"] or "")
    ]
    qqq_rows = [row for row in rows if row["ticker"] == "QQQ"]
    msft_rows = [row for row in rows if row["ticker"] == "MSFT"]

    spy_0915 = score_at("SPY", "09:15")
    aapl_1345 = score_at("AAPL", "13:45")
    msft_1415 = score_at("MSFT", "14:15")
    qqq_1045 = score_at("QQQ", "10:45")
    gate_0915 = gate_at("09:15")
    gate_1345 = gate_at("13:45")
    gate_1415 = gate_at("14:15")
    gate_1045 = gate_at("10:45")

    latch = CLOCK.latch_at[-1] if CLOCK.latch_at else ""
    critical = "CRITICAL: EOD FORCED CLOSE" in discord
    # Live lines are "CT 14:46" (cdt_clock_str), not "14:46 CT".
    retries = (
        ("CT 14:46" in log_text or "14:46 CT" in log_text)
        and ("CT 14:55" in log_text or "14:55 CT" in log_text)
    )

    a_ok = (
        "SPY:" in gate_0915
        and "ADMIT" in gate_0915
        and bool(spy_stop)
        and not bool(spy_stop[0]["fill_est"])
        and _ct(spy_stop[0]["closed_at"]) < "2026-11-16 12:00:00 CT"
    )
    # score line has no clock of its own; the banner match is enough for >= 80
    a_score_ok = "EXECUTE" in spy_0915 and _ge_80(spy_0915)
    b_ok = (
        "AAPL:" in gate_1345
        and "ADMIT" in gate_1345
        and bool(aapl_flat)
        and not bool(aapl_flat[0]["fill_est"])
        and _ge_80(aapl_1345)
        and _ct(aapl_flat[0]["closed_at"]).startswith("2026-11-16 14:45")
    )
    c_ok = (
        bool(iwm_est)
        and bool(iwm_est[0]["fill_est"])
        and _ct(iwm_est[0]["closed_at"]).startswith("2026-11-16 14:55")
        and critical
        and retries
        and "14:55" in latch
        and "14:45" not in (CLOCK.latch_at[0] if CLOCK.latch_at else "")
    )
    d_ok = (
        _ge_80(msft_1415)
        and "EXECUTE" in msft_1415
        and "late_session" in gate_1415
        and not msft_rows
    )
    qqq_tag = next((line for line in tags if " QQQ " in f" {line} "), "")
    meta_tag = next((line for line in tags if " META " in f" {line} "), "")
    meta_open = [
        row for row in rows
        if row["ticker"] == "META" and str(row["notes"] or "") == "PAPER_BUY_OPEN"
    ]
    meta_stop = [
        row for row in rows
        if row["ticker"] == "META" and "STOP_LOSS" in str(row["notes"] or "")
    ]
    gate_1130 = gate_at("11:30")
    meta_1130 = score_at("META", "11:30")

    def _by_kind(kind: str):
        for row in sessions:
            if "kind" in row.keys() and row["kind"] == kind:
                return row
        return None

    def _num_field(line: str, name: str):
        for part in str(line or "").split("|"):
            bits = part.split()
            if len(bits) == 2 and bits[0] == name:
                try:
                    return int(bits[1])
                except ValueError:
                    return None
        return None

    def _close(ticker: str, event_id: str):
        for row in close_tags:
            if row["ticker"] == ticker and row["event_id"] == event_id:
                return row
        return None

    def _number(value) -> bool:
        if value is None:
            return False
        try:
            float(value)
        except (TypeError, ValueError):
            return False
        return True

    session_row = _by_kind("session")
    final_row = _by_kind("final")
    session_text = "" if session_row is None else str(session_row["line"])
    final_text = "" if final_row is None else str(final_row["line"])
    session_closes = _num_field(session_text, "closes")
    ppi_close = _close("QQQ", FAKE_EVENT_ID)
    cpi_close = _close("META", CPI_EVENT_ID)
    session_ok = (
        session_row is not None
        and session_text.startswith("SESSION|")
        and _num_field(session_text, "open_value") not in (None, 0)
    )
    final_ok = (
        final_row is not None
        and final_text.startswith("SESSION_FINAL|")
        and _num_field(final_text, "open_value") == 0
        and session_closes is not None
        and _num_field(final_text, "closes") == session_closes + 1
        and int(final_row["incomplete"] or 0) == 0
        and "SESSION_FINAL|" in discord
    )
    e_ok = (
        "QQQ:" in gate_1045
        and "ADMIT" in gate_1045
        and _ge_80(qqq_1045)
        and "would_block=True" in qqq_tag
        and "next_event_name=Producer Price Index for October 2026" in qqq_tag
        and "minutes_to_next_event=15" in qqq_tag
        and ppi_close is not None
        and ppi_close["actual"] == "0.3%"
        and ppi_close["forecast"] == "0.4%"
        and ppi_close["prior"] == "1.3%"
        and str(ppi_close["surprise"]) == "-0.1"
        and ppi_close["label"] == "COLD"
        and _number(ppi_close["reaction_spy"])
        and _number(ppi_close["reaction_qqq"])
    )
    e2_ok = (
        bool(meta_open)
        and _ct(meta_open[0]["closed_at"]).startswith("2026-11-16 11:30")
        and bool(meta_stop)
        and "STOP_LOSS" in str(meta_stop[0]["notes"] or "")
        and _ct(meta_stop[0]["closed_at"]).startswith("2026-11-16 12:30")
        and "META:" in gate_1130
        and "ADMIT" in gate_1130
        and _ge_80(meta_1130)
        and "would_block=True" in meta_tag
        and "next_event_name=Consumer Price Index for October 2026" in meta_tag
        and "minutes_to_next_event=30" in meta_tag
        and cpi_close is not None
        and cpi_close["actual"] == "0.2%"
        and cpi_close["forecast"] == "0.4%"
        and cpi_close["prior"] == "0.5%"
        and str(cpi_close["surprise"]) == "-0.2"
        and cpi_close["label"] == "COLD"
        and _number(cpi_close["reaction_spy"])
        and _number(cpi_close["reaction_qqq"])
    )

    verdicts = [
        ("a", a_ok and a_score_ok, "SPY >= 80 at 09:15, admitted, STOP_LOSS later that morning"),
        ("b", b_ok, "AAPL admitted at 13:45 and EOD_FLATTEN at 14:45 with a real quote"),
        ("c", c_ok, "IWM dark at 14:45, 60s retries, EOD_FLATTEN_ESTIMATED at 14:55"),
        ("d", d_ok, "MSFT >= 80 at 14:15 blocked late_session and never opened"),
        ("e", e_ok, "QQQ 10:45 would_block true, next event PPI in 15 min, close tag has the print"),
        ("e2", e2_ok, "META 11:30 entry, CPI at 12:00, STOP_LOSS at 12:30, close tag has the print"),
        ("session", session_ok and final_ok, "14:45 SESSION keeps the open book; SESSION_FINAL is the flat book"),
    ]

    print("\n========== REHEARSAL REPORT ==========")
    print(f"stamp check is external; this process did not write a stamped file")
    print(f"discord file: {DISCORD_PATH}")
    print(f"log file: {LOG_PATH}")
    print(f"pacing seconds not applied to the session clock: {CLOCK.pacing_s:.1f}")
    print(f"network calls after the snapshot: {len(NETWORK_BLOCKED)}")
    if NETWORK_BLOCKED:
        for item in NETWORK_BLOCKED[:8]:
            print(f"  blocked {item}")
    print("\n--- Discord file ---")
    print(discord if discord else "(empty)")
    print("--- trade ledger rows opened on the simulated session ---")
    if not rows:
        print("(none)")
    for row in rows:
        print(
            f"id={row['id']} {row['ticker']} {row['direction']} "
            f"{row['strike']} exp={row['expiration']} "
            f"entry={row['entry_price']} exit={row['exit_price']} "
            f"reason={row['notes']} fill_est={row['fill_est']} "
            f"closed_at={_ct(row['closed_at'])}"
        )
    print("\n--- 14:45 SESSION line ---")
    print(session_text or "(none)")
    print("\n--- SESSION_FINAL line ---")
    print(final_text or "(none)")
    print("\n--- scan marks (FULL SCAN) ---")
    if not scan_marks:
        print("(none)")
    else:
        clocks = []
        for row in scan_marks:
            text = _ct(row["at_ct"])
            clocks.append(text[11:16] if len(text) >= 16 else text)
        print(" ".join(clocks))
    print("\n--- critical outbox ---")
    if not critical_rows:
        print("(none)")
    undelivered = 0
    for row in critical_rows:
        delivered = str(row["delivered_at"] or "").strip()
        if not delivered:
            undelivered += 1
        snippet = " ".join(str(row["body"] or "").split())
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        print(f"delivered_at={delivered or '(missing)'} {snippet}")
    print(f"critical rows: {len(critical_rows)}  missing delivered_at: {undelivered}")
    print("\n--- session latch ---")
    print(latch or "(never)")
    print(f"all latch calls: {CLOCK.latch_at or '(none)'}")
    print("\n--- open tags ---")
    if not tags:
        print("(none)")
    for line in tags:
        print(line)
    print("\n--- close tags ---")
    if not close_lines:
        print("(none)")
    for line in close_lines:
        print(line)
    print("\n--- close tag rows ---")
    if not close_tags:
        print("(none)")
    for row in close_tags:
        print(
            f"{row['ticker']} trade={row['trade_id']} event={row['event_id']} "
            f"actual={row['actual']} forecast={row['forecast']} prior={row['prior']} "
            f"surprise={row['surprise']} label={row['label']} "
            f"reaction_spy={row['reaction_spy']} reaction_qqq={row['reaction_qqq']}"
        )
    print("\n--- shadow_items for the fake events ---")
    if not items:
        print("(none)")
    for row in items:
        print(
            f"{row['event_id']} {row['component']} "
            f"actual={row['actual']} forecast={row['forecast']} prior={row['prior']} "
            f"at={row['published_at']}"
        )
    print(f"\nopen positions at the end: {len(open_json)}")
    print("\n--- evidence ---")
    print(f"09:15 gate: {gate_0915}")
    print(f"09:15 SPY: {spy_0915}")
    print(f"10:45 gate: {gate_1045}")
    print(f"10:45 QQQ: {qqq_1045}")
    print(f"13:45 gate: {gate_1345}")
    print(f"13:45 AAPL: {aapl_1345}")
    print(f"14:15 gate: {gate_1415}")
    print(f"14:15 MSFT: {msft_1415}")
    print(f"QQQ tag: {qqq_tag}")
    print(f"META tag: {meta_tag}")
    print(f"11:30 gate: {gate_1130}")
    print(f"11:30 META: {meta_1130}")
    print("\n--- scenarios ---")
    failed = False
    for name, ok, text in verdicts:
        mark = "PASS" if ok else "FAIL"
        print(f"{name}. {mark} — {text}")
        if not ok:
            failed = True
    if len(open_json) != 0:
        print("FAIL — open positions remain")
        failed = True
    else:
        print("open book: 0")
    if failed:
        print("Stopping. Stamped files were not edited.")
    return 1 if failed else 0


def _ge_80(line: str) -> bool:
    if "EXECUTE" not in line or "/100" not in line:
        return False
    try:
        number = line.split("/100", 1)[0].rsplit("=", 1)[1]
        return float(number.strip()) >= 80.0
    except (IndexError, ValueError):
        return False


def _import_lazy_clocks() -> None:
    """Import modules the loop loads after boot, then the caller sweeps them.

    A module imported inside the first scan would otherwise keep the real
    datetime class until the next sleep. The gate, exits, and session
    stamp would then read the wall clock.
    """
    import carry_shadow  # noqa: F401
    import earnings_blackout  # noqa: F401
    import fill_accounting  # noqa: F401
    import midday_delta  # noqa: F401
    import news_memory  # noqa: F401
    import position_exits  # noqa: F401
    import pre_market_meeting  # noqa: F401
    import reliability  # noqa: F401
    import session_clock  # noqa: F401
    import shadow_context  # noqa: F401
    import shadow_news  # noqa: F401
    import shadow_reconcile  # noqa: F401
    import signal_gate  # noqa: F401
    import state_preflight  # noqa: F401
    import tracker_agent  # noqa: F401
    import virtual_broker  # noqa: F401


def _install_discord_transport() -> None:
    """Real sender. The rehearsal host answers 204, 500, or a dead network."""
    import requests
    import broadcaster

    broadcaster.WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK", "")

    class _Resp:
        def __init__(self, code: int, text: str = "") -> None:
            self.status_code = code
            self.text = text

        def json(self) -> dict:
            return {}

    def post(url, *args, **kwargs):
        target = str(url)
        if "rehearsal.local/discord" not in target:
            NETWORK_BLOCKED.append(f"POST {target}"[:180])
            raise RuntimeError("rehearsal: network is closed")
        if CHAOS is not None and _in_windows(CLOCK.ct, CHAOS.discord_down):
            raise requests.ConnectionError("rehearsal: discord unreachable")
        if CHAOS is not None and _in_windows(CLOCK.ct, CHAOS.discord_500):
            return _Resp(500, "server error")
        payload = kwargs.get("json") or {}
        body = str(payload.get("content") or "") if isinstance(payload, dict) else ""
        if CHAOS is not None:
            CHAOS.delivered.append(body)
        return _Resp(204, "")

    def get(url, *args, **kwargs):
        target = str(url)
        if "rehearsal.local/heartbeat" not in target:
            NETWORK_BLOCKED.append(f"GET {target}"[:180])
            raise RuntimeError("rehearsal: network is closed")
        if CHAOS is not None and _in_windows(CLOCK.ct, CHAOS.heartbeat_down):
            raise requests.ConnectionError("rehearsal: heartbeat unreachable")
        return _Resp(200, "ok")

    requests.post = post
    requests.get = get


def _arm_chaos_env() -> None:
    if CHAOS is None or not CHAOS.real_discord:
        return
    os.environ["DISCORD_WEBHOOK"] = "http://rehearsal.local/discord"
    os.environ["HEARTBEAT_URL"] = "http://rehearsal.local/heartbeat"


def _point_env(state: Path) -> Path:
    news = state / "news_room.db"
    os.environ["NEWS_DB_PATH"] = str(news)
    os.environ["HEDGE_DB_PATH"] = str(state / "hedge_fund.db")
    os.environ["ACTIVE_TRADES_PATH"] = str(state / "active_trades.json")
    os.environ["SESSION_BASELINE_PATH"] = str(state / "session_baseline.json")
    os.environ["X_BEARER_TOKEN"] = "rehearsal"
    os.environ["GEMINI_API_KEY"] = "rehearsal"
    os.environ["RESET_LEDGER_ON_BOOT"] = "false"
    os.environ.pop("FULL_LLM_INTRADAY", None)
    os.environ.pop("BYPASS_MARKET_HOURS", None)
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    return news


def _half_hour_slots(start_min: int, end_min: int) -> set[tuple[int, int]]:
    slots = set()
    minute = start_min
    while minute <= end_min:
        slots.add((minute // 60, minute % 60))
        minute += 30
    return slots


def _chaos_for(name: str) -> Chaos | None:
    if name in ("", "base"):
        return None
    if name == "f":
        chaos = Chaos("f")
        chaos.high_slots = {"SPY": {(8, 45), (9, 15)}}
        window = (time(10, 10), time(13, 0))
        chaos.yahoo_down = [window]
        chaos.discord_down = [window]
        chaos.heartbeat_down = [window]
        chaos.dark = [(time(13, 0), time(16, 0))]
        chaos.spot_drop_after = time(13, 0)
        chaos.real_discord = True
        return chaos
    if name == "g1":
        chaos = Chaos("g1")
        chaos.high_slots = {
            "SPY": {(8, 45), (9, 15), (9, 45), (10, 15), (10, 45)},
            "QQQ": {(10, 15), (10, 45)},
        }
        chaos.crash_at = time(11, 0)
        return chaos
    if name == "g2":
        chaos = Chaos("g2")
        chaos.high_slots = {}
        chaos.start = datetime(2026, 11, 16, 11, 5, tzinfo=CHICAGO)
        return chaos
    if name == "h":
        chaos = Chaos("h")
        chaos.high_slots = {"SPY": {(8, 45), (9, 15)}}
        chaos.dark = [(time(10, 0), time(10, 30))]
        chaos.discord_500 = [(time(10, 0), time(10, 30))]
        chaos.real_discord = True
        return chaos
    if name == "i":
        chaos = Chaos("i")
        chaos.high_slots = {"SPY": {(8, 45), (9, 15), (9, 45), (10, 15), (10, 45)}}
        chaos.yahoo_429 = [(time(10, 0), time(10, 20))]
        return chaos
    if name == "j":
        chaos = Chaos("j")
        chaos.high_slots = {"SPY": _half_hour_slots(8 * 60 + 45, 14 * 60 + 15)}
        window = (time(14, 40), time(15, 10))
        chaos.yahoo_down = [window]
        chaos.discord_down = [window]
        chaos.heartbeat_down = [window]
        chaos.real_discord = True
        chaos.keep_running_at_night = True
        chaos.stop_at = time(15, 15)
        return chaos
    raise SystemExit(f"unknown scenario {name}")


def _rows(news: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{news}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, args))
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _trade_rows(news: Path) -> list[dict]:
    cols = {row[1] for row in _rows(news, "PRAGMA table_info(trade_history)")}
    if "ticker" not in cols or "notes" not in cols:
        return []
    closed = "closed_at" if "closed_at" in cols else "''"
    out = []
    for row in _rows(news, f"SELECT ticker, notes, {closed} AS closed_at FROM trade_history"):
        when = _ct(row["closed_at"])
        if not when.startswith(SESSION_DAY.isoformat()):
            continue
        out.append({"ticker": row["ticker"], "notes": str(row["notes"] or ""), "when": when})
    return out


def _final_text(news: Path) -> str:
    """This session's SESSION_FINAL. The copy still holds earlier days."""
    rows = _rows(
        news,
        """SELECT line FROM session_lines
           WHERE session_date=? AND line LIKE 'SESSION_FINAL|%'""",
        (SESSION_DAY.isoformat(),),
    )
    if not rows:
        return ""
    return str(rows[-1]["line"] or "")


def _clean_clause(final_text: str) -> str:
    mark = final_text.rfind("|CLEAN ")
    if mark < 0:
        return ""
    return final_text[mark + 1:]


def _scan_marks(news: Path) -> list[tuple[str, int]]:
    out = []
    for row in _rows(
        news,
        """SELECT at_ct, fetched FROM session_marks
           WHERE session_date=? AND kind='scan' ORDER BY at_ct""",
        (SESSION_DAY.isoformat(),),
    ):
        when = _ct(row["at_ct"])
        try:
            fetched = int(row["fetched"])
        except (TypeError, ValueError):
            fetched = -1
        out.append((when, fetched))
    return out


def _critical_rows(news: Path) -> list[dict]:
    out = []
    day = SESSION_DAY.isoformat()
    for row in _rows(
        news,
        """SELECT body, created_at, delivered_at FROM discord_outbox
           WHERE critical=1 ORDER BY id"""
    ):
        created = _ct(row["created_at"])
        if not created.startswith(day):
            continue
        out.append({
            "body": str(row["body"] or ""),
            "created": created,
            "delivered": _ct(row["delivered_at"]),
        })
    return out


def _opens_in(rows: list[dict], start: str, end: str) -> list[str]:
    found = []
    for row in rows:
        if "PAPER_BUY_OPEN" not in row["notes"]:
            continue
        clock = row["when"][11:16]
        if start <= clock < end:
            found.append(f"{row['ticker']} {clock}")
    return found


def _grade_f(news: Path, log_text: str) -> tuple[bool, str]:
    rows = _trade_rows(news)
    late = _opens_in(rows, "10:10", "13:00")
    underlying = [row for row in rows if "UNDERLYING_STOP" in row["notes"]]
    criticals = _critical_rows(news)
    failed = [row for row in criticals if "MARK FAILED" in row["body"]]
    naked = [row for row in criticals if "UNPROTECTED" in row["body"]]
    undelivered = [row for row in criticals if not row["delivered"]]
    clean = _clean_clause(_final_text(news))
    recovered = [when for when, fetched in _scan_marks(news) if when[11:16] >= "13:00" and fetched == 10]
    reasons = []
    if late:
        reasons.append(f"entries during the outage: {late}")
    if not underlying:
        reasons.append("no UNDERLYING_STOP close")
    if not failed:
        reasons.append("no MARK FAILED critical")
    if not naked:
        reasons.append("no UNPROTECTED critical")
    if undelivered:
        reasons.append(f"{len(undelivered)} criticals still missing delivered_at")
    if not clean.startswith("CLEAN no"):
        reasons.append(f"CLEAN field is {clean or '(missing)'}")
    elif "scan 0/10" not in clean and "scans " not in clean and "gap " not in clean:
        reasons.append(f"CLEAN reason does not name the missed scans: {clean}")
    if "undelivered CRITICAL" in clean:
        reasons.append(f"CLEAN still names an undelivered critical: {clean}")
    if not recovered:
        reasons.append("no completed full scan with 10 tickers after 13:00")
    if "ping failed" not in log_text:
        reasons.append("heartbeat did not fail during the outage")
    if "Traceback" in log_text:
        reasons.append("the loop traceback'd")
    evidence = (
        f"{clean or 'CLEAN missing'}; "
        f"MARK FAILED x{len(failed)} delivered; UNPROTECTED x{len(naked)}; "
        f"UNDERLYING_STOP x{len(underlying)}; "
        f"recovery scans {recovered[:3]}"
    )
    return not reasons, evidence if not reasons else evidence + " | " + "; ".join(reasons)


def _grade_g1(state: Path, log_text: str) -> tuple[bool, str]:
    import tracker_agent
    trades = tracker_agent.load_active_trades(state / "active_trades.json")
    names = sorted({str(trade.get("ticker") or "").upper() for trade in trades})
    reasons = []
    if names != ["QQQ", "SPY"]:
        reasons.append(f"open at the crash: {names or 'none'}")
    if "11:00" not in log_text and "11:00" not in CLOCK.ct.strftime("%H:%M"):
        reasons.append(f"clock stopped at {CLOCK.ct.strftime('%H:%M:%S')}")
    if "Traceback" in log_text:
        reasons.append("the loop traceback'd")
    evidence = f"open at crash {names}; clock {CLOCK.ct.strftime('%H:%M:%S')} CT"
    return not reasons, evidence if not reasons else evidence + " | " + "; ".join(reasons)


def _grade_g2(news: Path, log_text: str) -> tuple[bool, str]:
    rows = _trade_rows(news)
    opens = [row for row in rows if "PAPER_BUY_OPEN" in row["notes"]]
    late = _opens_in(rows, "11:05", "16:00")
    by_ticker: dict[str, int] = {}
    for row in opens:
        by_ticker[str(row["ticker"]).upper()] = by_ticker.get(str(row["ticker"]).upper(), 0) + 1
    boot = [row for row in rows if "BOOT_FLATTEN" in row["notes"]]
    import tracker_agent
    still = tracker_agent.load_active_trades()
    reasons = []
    if "OPEN AT BOOT" not in log_text or "2 position" not in log_text:
        reasons.append("OPEN AT BOOT for 2 positions was not logged")
    if by_ticker.get("SPY") != 1 or by_ticker.get("QQQ") != 1 or len(opens) != 2:
        reasons.append(f"open rows {by_ticker}")
    if late:
        reasons.append(f"entries after the restart: {late}")
    if len(boot) != 2:
        reasons.append(f"BOOT_FLATTEN closes {len(boot)}")
    if still:
        reasons.append(f"{len(still)} positions still open")
    if "Traceback" in log_text:
        reasons.append("the loop traceback'd")
    evidence = (
        f"opens {by_ticker}; BOOT_FLATTEN x{len(boot)}; "
        f"open book {len(still)}"
    )
    return not reasons, evidence if not reasons else evidence + " | " + "; ".join(reasons)


def _delivery_in_order(news: Path) -> bool:
    """Queued rows, in the flush order, appear in the posts that returned 204.

    A first-try send stores created_at and delivered_at together. A flush
    stamps delivered_at later. Later first-try criticals sort ahead of
    older non-critical queue rows, so the order check uses only the rows
    that actually waited.
    """
    if CHAOS is None:
        return False
    day = SESSION_DAY.isoformat()
    rows = [
        row for row in _rows(
            news,
            """SELECT body, created_at, delivered_at FROM discord_outbox
               ORDER BY critical DESC, id ASC"""
        )
        if _ct(row["created_at"]).startswith(day)
    ]
    if not rows or any(not row["delivered_at"] for row in rows):
        return False
    expected = [
        str(row["body"] or "")
        for row in rows
        if str(row["delivered_at"] or "") != str(row["created_at"] or "")
    ]
    if not expected:
        return False
    cursor = 0
    for body in CHAOS.delivered:
        if cursor < len(expected) and body == expected[cursor]:
            cursor += 1
    return cursor == len(expected)


def _grade_h(news: Path, log_text: str) -> tuple[bool, str]:
    criticals = _critical_rows(news)
    queued = [
        row for row in criticals
        if "10:00" <= row["created"][11:16] < "10:30"
    ]
    late = [row for row in queued if row["delivered"] and row["delivered"][11:16] >= "10:30"]
    missing = [row for row in criticals if not row["delivered"]]
    reasons = []
    if not queued:
        reasons.append("no CRITICAL was queued during the 500 window")
    if len(late) != len(queued):
        reasons.append("a queued CRITICAL was not delivered after 10:30")
    if missing:
        reasons.append(f"{len(missing)} criticals missing delivered_at")
    if not _delivery_in_order(news):
        reasons.append("outbox rows were not delivered critical-first in id order")
    if "Traceback" in log_text:
        reasons.append("the loop traceback'd")
    evidence = (
        f"queued during 500 x{len(queued)}; delivered after x{len(late)}; "
        f"criticals {len(criticals)}"
    )
    return not reasons, evidence if not reasons else evidence + " | " + "; ".join(reasons)


def _grade_i(news: Path, log_text: str) -> tuple[bool, str]:
    marks = _scan_marks(news)
    dark = [when for when, fetched in marks if "10:00" <= when[11:16] < "10:20" and fetched == 0]
    back = [when for when, fetched in marks if when[11:16] >= "10:20" and fetched == 10]
    closed = [
        row for row in _trade_rows(news)
        if row["ticker"] == "SPY"
        and "PAPER_BUY_OPEN" not in row["notes"]
        and "10:00" <= row["when"][11:16] < "10:20"
    ]
    reasons = []
    if "Traceback" in log_text:
        reasons.append("the loop traceback'd")
    if not dark:
        reasons.append("no 0/10 scan mark inside the 429 window")
    if not back:
        reasons.append("no 10-ticker scan mark after the 429 window")
    if closed:
        reasons.append(f"SPY closed during the 429 window: {closed[0]['notes']}")
    if "429" not in log_text:
        reasons.append("the log never recorded a 429")
    evidence = f"429 scan marks {dark[:2]}; recovery marks {back[:2]}"
    return not reasons, evidence if not reasons else evidence + " | " + "; ".join(reasons)


def _grade_j(news: Path, log_text: str) -> tuple[bool, str]:
    rows = _trade_rows(news)
    forced = [row for row in rows if "EOD_FLATTEN_ESTIMATED" in row["notes"]]
    alerts = [row for row in _critical_rows(news) if "EOD FORCED CLOSE" in row["body"]]
    delivered = [row for row in alerts if row["delivered"] and row["delivered"][11:16] >= "15:10"]
    import tracker_agent
    still = tracker_agent.load_active_trades()
    reasons = []
    if not forced:
        reasons.append("no EOD_FLATTEN_ESTIMATED ledger row")
    elif not any("14:55" <= row["when"][11:16] <= "15:05" for row in forced):
        reasons.append(f"forced close clock {forced[0]['when']}")
    if not alerts:
        reasons.append("no EOD FORCED CLOSE alert")
    if len(delivered) != len(alerts):
        reasons.append("the forced-close alert was not delivered after 15:10")
    if still:
        reasons.append(f"{len(still)} positions still open")
    if "Traceback" in log_text:
        reasons.append("the loop traceback'd")
    evidence = (
        f"forced {forced[0]['when'] if forced else 'none'}; "
        f"alert delivered {delivered[0]['delivered'] if delivered else 'no'}; "
        f"open book {len(still)}"
    )
    return not reasons, evidence if not reasons else evidence + " | " + "; ".join(reasons)


def _load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_loop(news: Path, snapshot: dict) -> str:
    global DISCORD_PATH, LOG_PATH
    import master_bot

    if CHAOS is not None and CHAOS.start is not None:
        CLOCK.ct = CHAOS.start
        CLOCK._last_mark = ""
    SNAPSHOT.clear()
    SNAPSHOT.update(snapshot)
    _install_clock()
    _install_market(snapshot)
    _install_io()
    _seed_event(news)
    _watch_latch()
    _import_lazy_clocks()
    CLOCK._sweep()
    if CHAOS is not None:
        import circuit_breaker
        circuit_breaker.time.time = lambda: CLOCK.ct.timestamp()
    _arm_night_stop()

    log_handle = open(LOG_PATH, "w", encoding="utf-8")
    real_out = sys.stdout
    sys.stdout = log_handle
    try:
        master_bot.run_macro_loop()
    except SystemExit:
        print(f"[rehearsal] stopped at {CLOCK.ct.isoformat()}")
    except Exception:
        traceback.print_exc()
    finally:
        sys.stdout = real_out
        log_handle.flush()
        log_handle.close()
    return Path(LOG_PATH).read_text(encoding="utf-8")


def _run_named(name: str, snapshot_path: Path, state: Path | None, state_out: Path | None) -> int:
    global CHAOS, DISCORD_PATH, LOG_PATH
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    CHAOS = _chaos_for(name)
    snapshot = _load_snapshot(snapshot_path)
    if state is not None:
        work = state
        news = _point_env(work)
    else:
        work = Path(tempfile.mkdtemp(prefix=f"rehearsal-{name or 'base'}-"))
        news, _hedge = _prepare_env(work)
    _arm_chaos_env()
    DISCORD_PATH = str(work / "discord.txt")
    LOG_PATH = str(work / "loop.log")
    print(f"[rehearsal] scenario {name or 'base'} temp {work}", file=sys.stderr)
    log_text = _run_loop(news, snapshot)
    if name in ("", "base"):
        code = _report(news, log_text)
        return code
    if name == "g1":
        ok, evidence = _grade_g1(work, log_text)
        if state_out is not None:
            state_out.write_text(str(work), encoding="utf-8")
    elif name == "g2":
        ok, evidence = _grade_g2(news, log_text)
    elif name == "f":
        ok, evidence = _grade_f(news, log_text)
    elif name == "h":
        ok, evidence = _grade_h(news, log_text)
    elif name == "i":
        ok, evidence = _grade_i(news, log_text)
    elif name == "j":
        ok, evidence = _grade_j(news, log_text)
    else:
        print(f"{name}. FAIL — no grader")
        return 1
    label = "g" if name in ("g1", "g2") else name
    phase = f" ({name})" if name in ("g1", "g2") else ""
    mark = "PASS" if ok else "FAIL"
    print(f"{label}. {mark}{phase} — {evidence}")
    if not ok:
        print("Stopping. Stamped files were not edited.")
        return 1
    return 0


def _spawn(name: str, snapshot: Path, state: Path | None = None, state_out: Path | None = None) -> int:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--scenario",
        name,
        "--snapshot",
        str(snapshot),
    ]
    if state is not None:
        cmd.extend(["--state", str(state)])
    if state_out is not None:
        cmd.extend(["--state-out", str(state_out)])
    proc = subprocess.run(cmd)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast-clock rehearsal on database copies")
    parser.add_argument("--scenario", default="")
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--state-out", default="")
    args = parser.parse_args()
    if args.scenario:
        if not args.snapshot:
            print("refused: a scenario run needs the shared snapshot", file=sys.stderr)
            return 2
        return _run_named(
            args.scenario,
            Path(args.snapshot),
            Path(args.state) if args.state else None,
            Path(args.state_out) if args.state_out else None,
        )

    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    hold = Path(tempfile.mkdtemp(prefix="rehearsal-snapshot-"))
    _prepare_env(hold)
    import master_bot
    print("[rehearsal] taking the one Yahoo chain snapshot", file=sys.stderr)
    snapshot = _snapshot(list(master_bot.TICKERS))
    snapshot_path = hold / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    code = _spawn("base", snapshot_path)
    if code != 0:
        print("[rehearsal] base scenarios failed; chaos runs were not started")
        return code
    code = _spawn("f", snapshot_path)
    if code != 0:
        return code
    state_out = hold / "g1-state.txt"
    code = _spawn("g1", snapshot_path, state_out=state_out)
    if code != 0:
        return code
    state = Path(state_out.read_text(encoding="utf-8").strip())
    for name in ("g2", "h", "i", "j"):
        code = _spawn(name, snapshot_path, state=state if name == "g2" else None)
        if code != 0:
            return code
    print("[rehearsal] a–j passed on database copies. Live files were not restarted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
