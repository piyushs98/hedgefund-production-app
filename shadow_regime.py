"""Shadow SPY regime log. The scorer, the gate, and the executor do not read it.

One regime_marks row per full scan. One day_labels row per session.
Paper entries copy the latest mark and three alignment flags.
A missing input stays NULL. A failure is logged and swallowed.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime, time as clock_time, timedelta, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
EAST = ZoneInfo("America/New_York")

OPEN = clock_time(8, 30)
OPEN_END = clock_time(8, 31)
OR_END = clock_time(9, 0)
RTH_END = clock_time(15, 0)

MARK_TIMEOUT_S = 8.0
REFERENCE_TIMEOUT_S = 4.0
CLOSE_TIMEOUT_S = 3.0
REFERENCE_RETRY = timedelta(minutes=30)
CALL_TIMEOUT_S = 2.0
YAHOO_SHADOW = "yahoo_shadow"
SPARK_URL = "https://query2.finance.yahoo.com/v7/finance/spark"

MARK_FIELDS = (
    "session_date",
    "scanned_at",
    "spy_price",
    "spy_open",
    "spy_high",
    "spy_low",
    "spy_move_pct",
    "spy_range_pct",
    "spy_move_atr",
    "or_high",
    "or_low",
    "or_status",
    "vwap",
    "vwap_side",
    "vwap_distance_pct",
    "gap_pct",
    "gap_filled",
    "vix",
    "vix_open",
    "vix_change",
    "vix1d",
    "vix1d_open",
    "vix1d_change",
    "breadth_up",
    "breadth_down",
    "qqq_vs_spy",
    "iwm_vs_spy",
    "atr_14",
    "prior_close",
)

TAG_EXTRA = (
    "trade_id",
    "ticker",
    "direction",
    "entry_at",
    "mark_id",
    "attached_scanned_at",
    "aligned_with_spy_move",
    "aligned_with_vwap",
    "or_breakout_aligned",
)

_scan_guard = threading.Lock()
_scan_token = None

_SHADOW_HEADER = (
    "SHADOW REGIME  (logged only; not a live rule; not the measured regime table)"
)


def _under_test() -> bool:
    """True only while this process has the unit suite loaded.

    `python -m unittest tests.test_*` records the tests package.
    `discover -s tests` records the same files as top-level test_* modules.
    The paper bot and the rehearsal subprocess import neither.
    """
    for name in sys.modules:
        if name == "tests" or name.startswith("tests."):
            return True
        if "." not in name and name.startswith("test_"):
            return True
    return False


def _moment(now) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _as_ct(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=EAST)
    return moment.astimezone(CT)


def _clock(moment: datetime) -> clock_time:
    return _as_ct(moment).time().replace(tzinfo=None)


def _session_date(moment: datetime) -> str:
    return _as_ct(moment).date().isoformat()


def _utc_iso(moment: datetime) -> str:
    return _moment(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _float_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _fmt(value) -> str:
    number = _float_or_none(value)
    if number is None:
        return "NULL"
    return f"{number:.4g}"


def _bit(value):
    if value is None:
        return None
    return 1 if value else 0


def _side(direction) -> str | None:
    text = str(direction or "").strip().upper()
    if text in ("CALL", "C"):
        return "CALL"
    if text in ("PUT", "P"):
        return "PUT"
    return None


def wilder_atr(bars) -> float | None:
    """Wilder ATR. The first value is the mean of the first 14 true ranges.

    `bars` is chronological (high, low, close). Any missing OHLC, or a
    high below its low, makes the whole result NULL. Fewer than 15 bars
    is NULL.
    """
    parsed = []
    for item in bars:
        try:
            high, low, close = item[0], item[1], item[2]
        except (TypeError, IndexError):
            return None
        high = _float_or_none(high)
        low = _float_or_none(low)
        close = _float_or_none(close)
        if high is None or low is None or close is None or high < low:
            return None
        parsed.append((high, low, close))
    if len(parsed) < 15:
        return None
    true_ranges = []
    for index in range(1, len(parsed)):
        high, low, _close = parsed[index]
        prev = parsed[index - 1][2]
        true_ranges.append(max(high - low, abs(high - prev), abs(low - prev)))
    atr = sum(true_ranges[:14]) / 14.0
    for true_range in true_ranges[14:]:
        atr = (atr * 13.0 + true_range) / 14.0
    return atr


def session_vwap(bars) -> float | None:
    """Session VWAP from typical price. A bar with no price or no volume is skipped."""
    traded = 0.0
    volume = 0.0
    used = 0
    for bar in bars:
        high = _float_or_none(bar.get("high"))
        low = _float_or_none(bar.get("low"))
        close = _float_or_none(bar.get("close"))
        vol = _float_or_none(bar.get("volume"))
        if high is None or low is None or close is None or vol is None:
            continue
        if vol <= 0 or high < low:
            continue
        typical = (high + low + close) / 3.0
        traded += typical * vol
        volume += vol
        used += 1
    if used == 0 or volume <= 0:
        return None
    return traded / volume


def label_day(open_, high, low, close):
    """Return (label, range_pct, close_vs_open_pct). Missing OHLC is NULL.

    trend: range >= 1.0% and |close-open| >= 0.6 × range.
    chop: range < 0.8% or |close-open| < 0.3 × range.
    Otherwise normal. trend is tested first, so the two labels do not overlap.
    """
    open_ = _float_or_none(open_)
    high = _float_or_none(high)
    low = _float_or_none(low)
    close = _float_or_none(close)
    if open_ is None or high is None or low is None or close is None:
        return None, None, None
    if open_ == 0 or high < low:
        return None, None, None
    range_px = high - low
    range_pct = range_px / open_ * 100.0
    close_vs_open = (close - open_) / open_ * 100.0
    body = abs(close - open_)
    # 1e-9 keeps a decimal threshold that binary floats shave under the line.
    if range_pct >= 1.0 - 1e-9 and body >= 0.6 * range_px - 1e-9:
        label = "trend"
    elif range_pct < 0.8 - 1e-9 or body < 0.3 * range_px - 1e-9:
        label = "chop"
    else:
        label = "normal"
    return label, range_pct, close_vs_open


def gap_state(open_, prior, high, low):
    """Return (gap_pct, filled). A missing input is (NULL, NULL).

    A gap up is filled when the session low has touched the prior close.
    A gap down is filled when the session high has touched it.
    An unchanged open is filled.
    """
    open_ = _float_or_none(open_)
    prior = _float_or_none(prior)
    high = _float_or_none(high)
    low = _float_or_none(low)
    if open_ is None or prior is None or prior == 0 or high is None or low is None:
        return None, None
    gap_pct = (open_ - prior) / prior * 100.0
    if open_ == prior:
        filled = True
    elif open_ > prior:
        filled = low <= prior
    else:
        filled = high >= prior
    return gap_pct, filled


def aligned_with_spy_move(direction, spy_move_pct):
    """True when the option side matches the sign of SPY's move since the open.

    NULL when the move is missing or its absolute value is under 0.1%.
    """
    move = _float_or_none(spy_move_pct)
    side = _side(direction)
    if move is None or side is None or abs(move) < 0.1:
        return None
    if side == "CALL":
        return move > 0
    return move < 0


def aligned_with_vwap(direction, vwap_side):
    """Calls above VWAP, puts below VWAP. Unknown side or direction is NULL."""
    side = _side(direction)
    if side is None or vwap_side not in ("above", "below"):
        return None
    if side == "CALL":
        return vwap_side == "above"
    return vwap_side == "below"


def or_breakout_aligned(direction, or_status):
    """Calls above the opening range, puts below it. Inside is false."""
    side = _side(direction)
    if side is None or or_status not in ("above", "below", "inside"):
        return None
    if side == "CALL":
        return or_status == "above"
    return or_status == "below"


def _move_pct(open_, price):
    open_ = _float_or_none(open_)
    price = _float_or_none(price)
    if open_ is None or price is None or open_ == 0:
        return None
    return (price - open_) / open_ * 100.0


def _relation(name_move, spy_move):
    if (
        name_move is None
        or spy_move is None
        or name_move == 0
        or spy_move == 0
    ):
        return None
    if (name_move > 0) == (spy_move > 0):
        return "same"
    return "opposite"


def _prepare(bars, now):
    now_ct = _as_ct(_moment(now))
    out = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        ts = bar.get("ts")
        if not isinstance(ts, datetime):
            continue
        ts = _as_ct(ts)
        if ts > now_ct:
            continue
        clock = ts.time().replace(tzinfo=None)
        if clock < OPEN or clock >= RTH_END:
            continue
        item = dict(bar)
        item["ts"] = ts
        out.append(item)
    out.sort(key=lambda item: item["ts"])
    return out, now_ct


def _session_open(bars):
    """Open of the 08:30 CT minute. A later minute is not a substitute."""
    for bar in bars:
        clock = bar["ts"].time().replace(tzinfo=None)
        if OPEN <= clock < OPEN_END:
            return _float_or_none(bar.get("open"))
    return None


def _last_close(bars):
    price = None
    for bar in bars:
        close = _float_or_none(bar.get("close"))
        if close is not None:
            price = close
    return price


def _high_low(bars):
    highs = []
    lows = []
    for bar in bars:
        high = _float_or_none(bar.get("high"))
        low = _float_or_none(bar.get("low"))
        if high is not None:
            highs.append(high)
        if low is not None:
            lows.append(low)
    if not highs or not lows:
        return None, None
    return max(highs), min(lows)


def _opening_bounds(bars, now_ct):
    """08:30–09:00 CT high and low. Before 09:00, or with no bars, status is NULL."""
    if _clock(now_ct) < OR_END:
        return None, None, None
    window = []
    for bar in bars:
        clock = bar["ts"].time().replace(tzinfo=None)
        if OPEN <= clock < OR_END:
            window.append(bar)
    if not window:
        return None, None, None
    high, low = _high_low(window)
    if high is None or low is None:
        return None, None, None
    return high, low, True


def _or_status(price, high, low, ready):
    price = _float_or_none(price)
    if not ready or price is None or high is None or low is None:
        return None
    if price > high:
        return "above"
    if price < low:
        return "below"
    return "inside"


def _vwap_position(price, vwap):
    price = _float_or_none(price)
    vwap = _float_or_none(vwap)
    if price is None or vwap is None or vwap == 0:
        return None, None
    distance = (price - vwap) / vwap * 100.0
    if price > vwap:
        return "above", distance
    if price < vwap:
        return "below", distance
    return None, distance


def _index_snapshot(bars):
    """Level, the 08:30 open, and level minus that open. No stand-in open."""
    if not bars:
        return None, None, None
    level = _last_close(bars)
    open_ = _session_open(bars)
    change = None
    if level is not None and open_ is not None:
        change = level - open_
    return level, open_, change


def breadth_counts(quotes, tickers):
    """Up and down since the open. A failed or empty batch is NULL, not zero."""
    if quotes is None:
        return None, None
    up = 0
    down = 0
    observed = 0
    for name in tickers:
        quote = quotes.get(name) if isinstance(quotes, dict) else None
        if not isinstance(quote, dict):
            continue
        move = _move_pct(quote.get("open"), quote.get("price"))
        if move is None:
            continue
        observed += 1
        if move > 0:
            up += 1
        elif move < 0:
            down += 1
    if observed == 0:
        return None, None
    return up, down


def quote_from_bars(bars, now) -> dict:
    prepared, _now_ct = _prepare(bars, now)
    return {"open": _session_open(prepared), "price": _last_close(prepared)}


def measure(spy_bars, vix_bars, vix1d_bars, quotes, *, now, atr, prior_close, tickers=None):
    """One scan's fields. Nothing here is invented from a neighboring bar."""
    import config

    names = list(tickers) if tickers is not None else list(config.TICKERS)
    moment = _moment(now)
    spy, now_ct = _prepare(spy_bars, moment)
    vix, _vix_now = _prepare(vix_bars, moment)
    vix1d, _vix1d_now = _prepare(vix1d_bars, moment)
    row = {name: None for name in MARK_FIELDS}
    row["session_date"] = _session_date(moment)
    row["scanned_at"] = _utc_iso(moment)
    row["atr_14"] = _float_or_none(atr)
    row["prior_close"] = _float_or_none(prior_close)

    price = _last_close(spy)
    open_ = _session_open(spy)
    high, low = _high_low(spy)
    row["spy_price"] = price
    row["spy_open"] = open_
    row["spy_high"] = high
    row["spy_low"] = low
    row["spy_move_pct"] = _move_pct(open_, price)
    if open_ not in (None, 0) and high is not None and low is not None:
        row["spy_range_pct"] = (high - low) / open_ * 100.0
    atr_value = row["atr_14"]
    if (
        price is not None
        and open_ is not None
        and atr_value not in (None, 0)
    ):
        row["spy_move_atr"] = (price - open_) / atr_value

    or_high, or_low, ready = _opening_bounds(spy, now_ct)
    row["or_high"] = or_high
    row["or_low"] = or_low
    row["or_status"] = _or_status(price, or_high, or_low, ready)

    vwap = session_vwap(spy)
    side, distance = _vwap_position(price, vwap)
    row["vwap"] = vwap
    row["vwap_side"] = side
    row["vwap_distance_pct"] = distance

    gap_pct, filled = gap_state(open_, row["prior_close"], high, low)
    row["gap_pct"] = gap_pct
    row["gap_filled"] = _bit(filled)

    level, vix_open, vix_change = _index_snapshot(vix)
    row["vix"] = level
    row["vix_open"] = vix_open
    row["vix_change"] = vix_change
    level, vix1d_open, vix1d_change = _index_snapshot(vix1d)
    row["vix1d"] = level
    row["vix1d_open"] = vix1d_open
    row["vix1d_change"] = vix1d_change

    up, down = breadth_counts(quotes, names)
    row["breadth_up"] = up
    row["breadth_down"] = down
    qqq = quotes.get("QQQ") if isinstance(quotes, dict) else None
    iwm = quotes.get("IWM") if isinstance(quotes, dict) else None
    qqq_move = _move_pct(
        qqq.get("open") if isinstance(qqq, dict) else None,
        qqq.get("price") if isinstance(qqq, dict) else None,
    )
    iwm_move = _move_pct(
        iwm.get("open") if isinstance(iwm, dict) else None,
        iwm.get("price") if isinstance(iwm, dict) else None,
    )
    row["qqq_vs_spy"] = _relation(qqq_move, row["spy_move_pct"])
    row["iwm_vs_spy"] = _relation(iwm_move, row["spy_move_pct"])
    return row


def _frame_moment(ts):
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=EAST)
    return ts.astimezone(CT)


def _cell(row, name):
    try:
        value = row[name]
    except Exception:
        return None
    return _float_or_none(value)


def bars_from_frame(frame) -> list:
    """1-minute frame to CT bars. A naive index is America/New_York."""
    if frame is None or getattr(frame, "empty", True):
        return []
    columns = getattr(frame, "columns", [])
    try:
        names = set(columns)
    except TypeError:
        names = set()
    if "Open" not in names and "Close" not in names:
        return []
    bars = []
    try:
        pairs = frame.iterrows()
    except Exception:
        return []
    for ts, row in pairs:
        moment = _frame_moment(ts)
        if moment is None:
            continue
        bars.append({
            "ts": moment,
            "open": _cell(row, "Open"),
            "high": _cell(row, "High"),
            "low": _cell(row, "Low"),
            "close": _cell(row, "Close"),
            "volume": _cell(row, "Volume"),
        })
    return bars


def _daily_session_date(ts):
    moment = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            return moment.date()
        return moment.astimezone(EAST).date()
    if hasattr(moment, "year") and hasattr(moment, "month") and hasattr(moment, "day"):
        try:
            return date(moment.year, moment.month, moment.day)
        except (TypeError, ValueError):
            return None
    return None


def daily_bars_from_frame(frame) -> list:
    """Daily rows as (session_date, open, high, low, close), oldest first."""
    if frame is None or getattr(frame, "empty", True):
        return []
    bars = []
    try:
        pairs = frame.iterrows()
    except Exception:
        return []
    for ts, row in pairs:
        session = _daily_session_date(ts)
        if session is None:
            continue
        bars.append((
            session,
            _cell(row, "Open"),
            _cell(row, "High"),
            _cell(row, "Low"),
            _cell(row, "Close"),
        ))
    bars.sort(key=lambda item: item[0])
    return bars


def _bars_from_chart_block(block) -> list:
    stamps = block.get("timestamp") or []
    indicators = block.get("indicators") or {}
    quote_rows = indicators.get("quote") or []
    fields = quote_rows[0] if isinstance(quote_rows, list) and quote_rows else {}
    if not isinstance(fields, dict):
        fields = {}
    opens = fields.get("open") or []
    highs = fields.get("high") or []
    lows = fields.get("low") or []
    closes = fields.get("close") or []
    volumes = fields.get("volume") or []

    def _at(seq, index):
        if not isinstance(seq, (list, tuple)) or index >= len(seq):
            return None
        return _float_or_none(seq[index])

    bars = []
    for index, raw_ts in enumerate(stamps):
        try:
            moment = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc).astimezone(CT)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        bars.append({
            "ts": moment,
            "open": _at(opens, index),
            "high": _at(highs, index),
            "low": _at(lows, index),
            "close": _at(closes, index),
            "volume": _at(volumes, index),
        })
    return bars


def quotes_from_spark(payload, now) -> dict | None:
    """One spark payload to {symbol: {open, price}}. A bad payload is None."""
    if not isinstance(payload, dict):
        return None
    spark = payload.get("spark") if "spark" in payload else payload
    if not isinstance(spark, dict):
        return None
    result = spark.get("result")
    if not isinstance(result, list):
        return None
    quotes = {}
    for item in result:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        response = item.get("response")
        block = None
        if isinstance(response, list) and response:
            block = response[0] if isinstance(response[0], dict) else None
        elif isinstance(response, dict):
            block = response
        if not isinstance(block, dict):
            continue
        if not symbol:
            meta = block.get("meta") if isinstance(block.get("meta"), dict) else {}
            symbol = str(meta.get("symbol") or "").upper()
        if not symbol:
            continue
        quote = quote_from_bars(_bars_from_chart_block(block), now)
        if quote["open"] is None or quote["price"] is None:
            continue
        quotes[symbol] = quote
    return quotes


def _db_path(db_path):
    import shadow_news
    return shadow_news._db_path(db_path)


def _connect(db_path, timeout=2.0):
    path = _db_path(db_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _connect_ro(db_path, timeout=1.0):
    path = _db_path(db_path)
    if not path or not os.path.isfile(path):
        return None
    uri = "file:" + os.path.abspath(path) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def _has_table(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def ensure_regime_schema(db_path) -> None:
    import shadow_news
    try:
        shadow_news.ensure_schema(db_path)
    except Exception as exc:
        print(f"[Shadow] regime schema failed: {type(exc).__name__}")
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regime_marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                scanned_at TEXT NOT NULL,
                spy_price REAL,
                spy_open REAL,
                spy_high REAL,
                spy_low REAL,
                spy_move_pct REAL,
                spy_range_pct REAL,
                spy_move_atr REAL,
                or_high REAL,
                or_low REAL,
                or_status TEXT,
                vwap REAL,
                vwap_side TEXT,
                vwap_distance_pct REAL,
                gap_pct REAL,
                gap_filled INTEGER,
                vix REAL,
                vix_open REAL,
                vix_change REAL,
                vix1d REAL,
                vix1d_open REAL,
                vix1d_change REAL,
                breadth_up INTEGER,
                breadth_down INTEGER,
                qqq_vs_spy TEXT,
                iwm_vs_spy TEXT,
                atr_14 REAL,
                prior_close REAL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS regime_marks_session
            ON regime_marks (session_date, scanned_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regime_entry_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT,
                ticker TEXT,
                direction TEXT,
                entry_at TEXT,
                mark_id INTEGER,
                attached_scanned_at TEXT,
                aligned_with_spy_move INTEGER,
                aligned_with_vwap INTEGER,
                or_breakout_aligned INTEGER,
                session_date TEXT,
                scanned_at TEXT,
                spy_price REAL,
                spy_open REAL,
                spy_high REAL,
                spy_low REAL,
                spy_move_pct REAL,
                spy_range_pct REAL,
                spy_move_atr REAL,
                or_high REAL,
                or_low REAL,
                or_status TEXT,
                vwap REAL,
                vwap_side TEXT,
                vwap_distance_pct REAL,
                gap_pct REAL,
                gap_filled INTEGER,
                vix REAL,
                vix_open REAL,
                vix_change REAL,
                vix1d REAL,
                vix1d_open REAL,
                vix1d_change REAL,
                breadth_up INTEGER,
                breadth_down INTEGER,
                qqq_vs_spy TEXT,
                iwm_vs_spy TEXT,
                atr_14 REAL,
                prior_close REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS day_labels (
                session_date TEXT PRIMARY KEY,
                range_pct REAL,
                close_vs_open_pct REAL,
                label TEXT,
                vix1d_open REAL,
                spy_open REAL,
                spy_high REAL,
                spy_low REAL,
                spy_close REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regime_reference (
                session_date TEXT PRIMARY KEY,
                prior_close REAL,
                atr_14 REAL,
                attempted_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _record_health(conn, moment, slot, status, rows, latency_ms, detail) -> None:
    import shadow_news
    shadow_news._record_health(
        conn,
        source=YAHOO_SHADOW,
        status=status,
        rows_new=int(rows or 0),
        latency_ms=latency_ms,
        fetched_at=_utc_iso(moment),
        detail=(detail or "")[:240],
        slot=slot,
    )


def _blank(moment: datetime) -> dict:
    row = {name: None for name in MARK_FIELDS}
    row["session_date"] = _session_date(moment)
    row["scanned_at"] = _utc_iso(moment)
    return row


def _insert_mark(conn, row: dict) -> int:
    names = ", ".join(MARK_FIELDS)
    slots = ", ".join("?" for _ in MARK_FIELDS)
    conn.execute(
        f"INSERT INTO regime_marks ({names}) VALUES ({slots})",
        tuple(row.get(name) for name in MARK_FIELDS),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _alignment(direction, mark: dict) -> dict:
    return {
        "aligned_with_spy_move": _bit(
            aligned_with_spy_move(direction, mark.get("spy_move_pct"))
        ),
        "aligned_with_vwap": _bit(
            aligned_with_vwap(direction, mark.get("vwap_side"))
        ),
        "or_breakout_aligned": _bit(
            or_breakout_aligned(direction, mark.get("or_status"))
        ),
    }


def _refresh_tags(conn, mark: dict, mark_id: int) -> None:
    if not _has_table(conn, "regime_entry_tags"):
        return
    rows = conn.execute(
        """
        SELECT id, direction, entry_at, mark_id, attached_scanned_at
        FROM regime_entry_tags
        WHERE session_date=? AND entry_at<=?
        """,
        (mark["session_date"], mark["scanned_at"]),
    ).fetchall()
    assignments = ", ".join(f"{name}=?" for name in MARK_FIELDS)
    for item in rows:
        attached = item["attached_scanned_at"]
        if (
            item["mark_id"] is not None
            and attached is not None
            and attached >= item["entry_at"]
        ):
            continue
        flags = _alignment(item["direction"], mark)
        values = [mark.get(name) for name in MARK_FIELDS]
        values.extend([
            mark_id,
            mark["scanned_at"],
            flags["aligned_with_spy_move"],
            flags["aligned_with_vwap"],
            flags["or_breakout_aligned"],
            item["id"],
        ])
        conn.execute(
            f"""
            UPDATE regime_entry_tags
            SET {assignments},
                mark_id=?,
                attached_scanned_at=?,
                aligned_with_spy_move=?,
                aligned_with_vwap=?,
                or_breakout_aligned=?
            WHERE id=?
            """,
            values,
        )


def _store_scan(db_path, mark: dict, health: list, moment: datetime) -> None:
    ensure_regime_schema(db_path)
    conn = _connect(db_path)
    try:
        mark_id = _insert_mark(conn, mark)
        _refresh_tags(conn, mark, mark_id)
        if _has_table(conn, "source_health"):
            for item in health:
                _record_health(
                    conn,
                    moment,
                    item.get("slot") or "scan",
                    item.get("status") or "fail",
                    item.get("rows") or 0,
                    item.get("latency_ms"),
                    item.get("detail") or "",
                )
        conn.commit()
    finally:
        conn.close()
    print(
        f"[Shadow] regime scan {mark['session_date']} {mark['scanned_at']} "
        f"spy_move={_fmt(mark.get('spy_move_pct'))} "
        f"or={mark.get('or_status') or 'NULL'} "
        f"vwap={mark.get('vwap_side') or 'NULL'} "
        f"vix1d={_fmt(mark.get('vix1d'))}"
    )


def _run_bounded(fn, timeout_s):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn).result(timeout=timeout_s), None
    except FuturesTimeoutError:
        return None, "TimeoutError"
    except Exception as exc:
        return None, type(exc).__name__
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _yahoo_live() -> bool:
    import yf_client
    return getattr(yf_client.ticker, "__module__", "") == "yf_client"


def _yahoo_history(symbol, *, period=None, interval, start=None, end=None):
    import yf_client
    if not _yahoo_live():
        yf_client.ticker(symbol)
    ticker = yf_client.ticker(symbol)
    kwargs = {
        "interval": interval,
        "auto_adjust": False,
        "prepost": False,
        "actions": False,
        "repair": False,
        "timeout": CALL_TIMEOUT_S,
        "raise_errors": True,
    }
    if start is not None:
        kwargs["start"] = start
        kwargs["end"] = end
    else:
        kwargs["period"] = period or "1d"
    return ticker.history(**kwargs)


def _yahoo_breadth(symbols):
    """One spark request for the whole universe. Not ten chart calls."""
    import yf_client
    if not _yahoo_live():
        yf_client.ticker("BREADTH")
    ticker = yf_client.ticker("SPY")
    response = ticker._data.get(
        url=SPARK_URL,
        params={
            "symbols": ",".join(symbols),
            "range": "1d",
            "interval": "1m",
        },
        timeout=CALL_TIMEOUT_S,
    )
    return response.json()


def _coerce_quotes(raw, now):
    if raw is None:
        return None
    if isinstance(raw, dict) and "spark" not in raw and "result" not in raw:
        return raw
    return quotes_from_spark(raw, now)


def _timed_history(symbol, slot, history, health, now_unused, **kwargs):
    del now_unused
    started = time.perf_counter()
    error = None
    bars = []
    try:
        if history is not None:
            raw = history(symbol, **kwargs)
        else:
            raw = _yahoo_history(symbol, **kwargs)
        bars = raw if isinstance(raw, list) else bars_from_frame(raw)
    except Exception as exc:
        error = type(exc).__name__
        bars = []
    latency = int((time.perf_counter() - started) * 1000)
    if error:
        status, detail = "fail", error
    elif not bars:
        status, detail = "fail", "empty"
    else:
        status, detail = "ok", f"bars={len(bars)}"
    health.append({
        "slot": slot,
        "status": status,
        "rows": len(bars),
        "latency_ms": latency,
        "detail": detail[:240],
    })
    return bars


def _timed_breadth(batch, now):
    import config
    symbols = list(config.TICKERS)
    started = time.perf_counter()
    error = None
    quotes = None
    try:
        raw = batch(symbols) if batch is not None else _yahoo_breadth(symbols)
        quotes = _coerce_quotes(raw, now)
    except Exception as exc:
        error = type(exc).__name__
        quotes = None
    latency = int((time.perf_counter() - started) * 1000)
    count = len(quotes) if isinstance(quotes, dict) else 0
    if error:
        status, detail = "fail", error
    elif count == 0:
        status, detail = "fail", "empty"
    else:
        status, detail = "ok", f"bars={count}"
    return quotes, {
        "slot": "breadth",
        "status": status,
        "rows": count,
        "latency_ms": latency,
        "detail": detail[:240],
    }


def _fetch_scan(now, history=None, batch=None) -> dict:
    """SPY 1-minute, ^VIX, ^VIX1D, and one batched quote. Four calls."""
    health = []
    spy = _timed_history("SPY", "spy_1m", history, health, now, period="1d", interval="1m")
    vix = _timed_history("^VIX", "vix", history, health, now, period="1d", interval="1m")
    vix1d = _timed_history("^VIX1D", "vix1d", history, health, now, period="1d", interval="1m")
    quotes, breadth = _timed_breadth(batch, now)
    health.append(breadth)
    return {
        "spy_bars": spy,
        "vix_bars": vix,
        "vix1d_bars": vix1d,
        "quotes": quotes,
        "health": health,
    }


def _reference_for(moment, db_path):
    conn = _connect_ro(db_path, timeout=1.0)
    if conn is None:
        return None, None
    try:
        if not _has_table(conn, "regime_reference"):
            return None, None
        row = conn.execute(
            """
            SELECT prior_close, atr_14 FROM regime_reference
            WHERE session_date=?
            """,
            (_session_date(moment),),
        ).fetchone()
    except sqlite3.Error:
        return None, None
    finally:
        conn.close()
    if row is None:
        return None, None
    return _float_or_none(row["prior_close"]), _float_or_none(row["atr_14"])


def _mark_full_scan_work(now, db_path, fetch) -> None:
    moment = _moment(now)
    try:
        payload = (fetch or _fetch_scan)(moment)
        if not isinstance(payload, dict):
            raise TypeError("regime payload")
    except Exception as exc:
        try:
            _store_scan(
                db_path,
                _blank(moment),
                [{
                    "slot": "scan",
                    "status": "fail",
                    "rows": 0,
                    "latency_ms": None,
                    "detail": type(exc).__name__,
                }],
                moment,
            )
        except Exception as store_exc:
            print(f"[Shadow] regime store failed: {type(store_exc).__name__}")
        print(f"[Shadow] regime scan failed: {type(exc).__name__}")
        return
    try:
        prior, atr = _reference_for(moment, db_path)
        mark = measure(
            payload.get("spy_bars") or [],
            payload.get("vix_bars") or [],
            payload.get("vix1d_bars") or [],
            payload.get("quotes"),
            now=moment,
            atr=atr,
            prior_close=prior,
        )
        health = payload.get("health") or []
        _store_scan(db_path, mark, health, moment)
    except Exception as exc:
        print(f"[Shadow] regime scan failed: {type(exc).__name__}")
        try:
            _store_scan(
                db_path,
                _blank(moment),
                [{
                    "slot": "scan",
                    "status": "fail",
                    "rows": 0,
                    "latency_ms": None,
                    "detail": type(exc).__name__,
                }],
                moment,
            )
        except Exception as store_exc:
            print(f"[Shadow] regime store failed: {type(store_exc).__name__}")


def mark_full_scan(now=None, db_path=None, fetch=None, timeout_s: float = MARK_TIMEOUT_S):
    """Write one regime_marks row. Returns when the row is in, or when the timeout hits.

    The caller is not held past timeout_s. A mark already running is skipped
    so a slow Yahoo call cannot start a second set. Under the unit-test
    importer, the default network path does not run and does not touch a database.
    """
    global _scan_token
    if fetch is None and _under_test():
        return None
    with _scan_guard:
        if _scan_token is not None:
            print("[Shadow] regime scan skipped: previous mark still running")
            return None
        token = object()
        _scan_token = token

    def _finish_token() -> None:
        global _scan_token
        with _scan_guard:
            if _scan_token is token:
                _scan_token = None

    def _work():
        try:
            _mark_full_scan_work(now, db_path, fetch)
        finally:
            _finish_token()

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_work)
    try:
        future.result(timeout=timeout_s)
    except FuturesTimeoutError:
        print("[Shadow] regime scan timed out")
    except Exception as exc:
        print(f"[Shadow] regime mark failed: {type(exc).__name__}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if future.cancelled():
            _finish_token()
    return None


def _lookup_trade_id(ticker, entry_at, trades_path):
    import tracker_agent
    trades = tracker_agent.load_active_trades(trades_path)
    matches = [
        trade for trade in trades
        if str(trade.get("ticker") or "").upper() == str(ticker or "").upper()
    ]
    if not matches:
        return None
    want = _moment(entry_at)
    best = None
    best_gap = None
    for trade in matches:
        raw = trade.get("entry_timestamp") or trade.get("entry_time")
        gap = None
        if raw:
            try:
                got = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                got = None
            if got is not None:
                if got.tzinfo is None:
                    got = got.replace(tzinfo=timezone.utc)
                gap = abs((got - want).total_seconds())
        if best is None or (
            gap is not None and (best_gap is None or gap < best_gap)
        ):
            best = trade.get("trade_id")
            best_gap = gap
    if best is None:
        return None
    return str(best)


def _latest_mark_at_or_before(conn, session_date: str, entry_at: str):
    if not _has_table(conn, "regime_marks"):
        return None
    return conn.execute(
        """
        SELECT * FROM regime_marks
        WHERE session_date=? AND scanned_at<=?
        ORDER BY scanned_at DESC, id DESC
        LIMIT 1
        """,
        (session_date, entry_at),
    ).fetchone()


def tag_entry(
    *,
    ticker,
    direction,
    entry_at,
    db_path=None,
    trade_id=None,
    trades_path=None,
) -> None:
    """Copy the latest mark onto the paper entry. Never raises.

    The copy is a placeholder until this scan's mark is written. A later
    mark attaches once, when its scanned_at is at or after the entry.
    """
    try:
        if db_path is None and _under_test():
            return
        if trade_id is None and trades_path is None and _under_test():
            trade_id = None
        elif trade_id is None:
            trade_id = _lookup_trade_id(ticker, entry_at, trades_path)
        moment = _moment(entry_at)
        entry_iso = _utc_iso(moment)
        session_date = _session_date(moment)
        ensure_regime_schema(db_path)
        conn = _connect(db_path)
        try:
            mark = _latest_mark_at_or_before(conn, session_date, entry_iso)
            copied = {name: None for name in MARK_FIELDS}
            mark_id = None
            attached = None
            if mark is not None:
                copied = {name: mark[name] for name in MARK_FIELDS}
                mark_id = mark["id"]
                attached = mark["scanned_at"]
            else:
                copied["session_date"] = session_date
            flags = _alignment(direction, copied)
            columns = list(TAG_EXTRA) + list(MARK_FIELDS)
            values = [
                None if trade_id is None else str(trade_id),
                ticker,
                direction,
                entry_iso,
                mark_id,
                attached,
                flags["aligned_with_spy_move"],
                flags["aligned_with_vwap"],
                flags["or_breakout_aligned"],
            ]
            values.extend(copied.get(name) for name in MARK_FIELDS)
            slots = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO regime_entry_tags ({', '.join(columns)}) "
                f"VALUES ({slots})",
                values,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[Shadow] regime tag failed: {type(exc).__name__}")


def _parse_attempt(raw, moment: datetime):
    if not raw:
        return None
    try:
        parsed = datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return parsed


def _reference_row(day: str, db_path):
    conn = _connect_ro(db_path, timeout=1.0)
    if conn is None:
        return None
    try:
        if not _has_table(conn, "regime_reference"):
            return None
        return conn.execute(
            """
            SELECT prior_close, atr_14, attempted_at
            FROM regime_reference WHERE session_date=?
            """,
            (day,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _reference_due(moment: datetime, day: str, db_path) -> bool:
    row = _reference_row(day, db_path)
    if row is None:
        return True
    if row["prior_close"] is not None and row["atr_14"] is not None:
        return False
    attempted = _parse_attempt(row["attempted_at"], moment)
    if attempted is not None and moment - attempted < REFERENCE_RETRY:
        return False
    return True


def _mark_attempt(day: str, moment: datetime, db_path) -> None:
    ensure_regime_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO regime_reference
                (session_date, prior_close, atr_14, attempted_at)
            VALUES (?, NULL, NULL, ?)
            ON CONFLICT(session_date) DO UPDATE SET
                attempted_at=excluded.attempted_at
            """,
            (day, _utc_iso(moment)),
        )
        conn.commit()
    finally:
        conn.close()


def _save_reference(day: str, prior, atr, db_path) -> None:
    if prior is None and atr is None:
        return
    conn = _connect(db_path)
    try:
        current = conn.execute(
            """
            SELECT prior_close, atr_14 FROM regime_reference
            WHERE session_date=?
            """,
            (day,),
        ).fetchone()
        kept_prior = prior if prior is not None else (
            None if current is None else current["prior_close"]
        )
        kept_atr = atr if atr is not None else (
            None if current is None else current["atr_14"]
        )
        conn.execute(
            """
            UPDATE regime_reference
            SET prior_close=?, atr_14=?
            WHERE session_date=?
            """,
            (kept_prior, kept_atr, day),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_reference(day: date) -> dict:
    frame = _yahoo_history("SPY", period="6mo", interval="1d")
    bars = [item for item in daily_bars_from_frame(frame) if item[0] < day]
    if not bars:
        return {"prior_close": None, "atr_14": None}
    _session, _open, _high, _low, close = bars[-1]
    prior = close
    window = bars[-30:]
    series = []
    for _session, bar_open, bar_high, bar_low, bar_close in window:
        if (
            bar_open is None
            or bar_high is None
            or bar_low is None
            or bar_close is None
            or bar_high < bar_low
        ):
            return {"prior_close": prior, "atr_14": None}
        series.append((bar_high, bar_low, bar_close))
    return {"prior_close": prior, "atr_14": wilder_atr(series)}


def _write_one_health(db_path, moment, slot, status, rows, latency_ms, detail) -> None:
    try:
        ensure_regime_schema(db_path)
        conn = _connect(db_path)
        try:
            if _has_table(conn, "source_health"):
                _record_health(conn, moment, slot, status, rows, latency_ms, detail)
                conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[Shadow] regime health failed: {type(exc).__name__}")


def ensure_reference(moment, db_path=None, fetch=None, timeout_s: float = REFERENCE_TIMEOUT_S) -> None:
    """One SPY daily read per session, from the shadow thread.

    A stored prior close and ATR end the fetches. A miss waits 30 minutes
    before another try. This is not one of the four scan calls.
    """
    try:
        if fetch is None and _under_test():
            return
        moment = _moment(moment)
        day = _as_ct(moment).date()
        day_text = day.isoformat()
        if not _reference_due(moment, day_text, db_path):
            return
        _mark_attempt(day_text, moment, db_path)
        value, error = _run_bounded(
            lambda: (fetch or _fetch_reference)(day),
            timeout_s,
        )
        prior = None
        atr = None
        if isinstance(value, dict):
            prior = _float_or_none(value.get("prior_close"))
            atr = _float_or_none(value.get("atr_14"))
        _save_reference(day_text, prior, atr, db_path)
        if error and prior is None and atr is None:
            print(f"[Shadow] regime reference {day_text} failed: {error}")
            detail = error
            status = "fail"
        elif prior is None or atr is None:
            print(
                f"[Shadow] regime reference {day_text} "
                f"prior={_fmt(prior)} atr={_fmt(atr)}"
            )
            detail = "empty" if error is None else error
            status = "fail"
        else:
            print(
                f"[Shadow] regime reference {day_text} "
                f"prior={_fmt(prior)} atr={_fmt(atr)}"
            )
            detail = "bars=1"
            status = "ok"
        _write_one_health(db_path, moment, "spy_daily", status, 1 if status == "ok" else 0, None, detail)
    except Exception as exc:
        print(f"[Shadow] regime reference failed: {type(exc).__name__}")


def write_day_label(
    day,
    *,
    open_=None,
    high=None,
    low=None,
    close=None,
    vix1d_open=None,
    db_path=None,
) -> None:
    session = day.isoformat() if isinstance(day, date) else str(day)
    label, range_pct, close_vs_open = label_day(open_, high, low, close)
    ensure_regime_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO day_labels (
                session_date, range_pct, close_vs_open_pct, label,
                vix1d_open, spy_open, spy_high, spy_low, spy_close
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_date) DO UPDATE SET
                range_pct=excluded.range_pct,
                close_vs_open_pct=excluded.close_vs_open_pct,
                label=excluded.label,
                vix1d_open=excluded.vix1d_open,
                spy_open=excluded.spy_open,
                spy_high=excluded.spy_high,
                spy_low=excluded.spy_low,
                spy_close=excluded.spy_close
            """,
            (
                session,
                range_pct,
                close_vs_open,
                label,
                _float_or_none(vix1d_open),
                _float_or_none(open_) if label is not None or open_ is not None else None,
                _float_or_none(high),
                _float_or_none(low),
                _float_or_none(close),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _vix1d_open_for(day, db_path):
    session = day.isoformat() if isinstance(day, date) else str(day)
    conn = _connect_ro(db_path, timeout=1.0)
    if conn is None:
        return None
    try:
        if not _has_table(conn, "regime_marks"):
            return None
        row = conn.execute(
            """
            SELECT vix1d_open FROM regime_marks
            WHERE session_date=? AND vix1d_open IS NOT NULL
            ORDER BY scanned_at ASC, id ASC
            LIMIT 1
            """,
            (session,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return _float_or_none(row["vix1d_open"])


def _fetch_close(day: date) -> dict | None:
    end = day + timedelta(days=1)
    frame = _yahoo_history(
        "SPY",
        interval="1d",
        start=day.isoformat(),
        end=end.isoformat(),
    )
    match = [item for item in daily_bars_from_frame(frame) if item[0] == day]
    if not match:
        return None
    _session, open_, high, low, close = match[-1]
    if (
        open_ is None
        or high is None
        or low is None
        or close is None
        or high < low
    ):
        return None
    return {"open": open_, "high": high, "low": low, "close": close}


def finish_day(day, now=None, db_path=None, fetch=None, timeout_s: float = CLOSE_TIMEOUT_S) -> None:
    """One day_labels row. A missing daily bar is stored as NULL, not guessed."""
    try:
        moment = _moment(now) if now is not None else datetime.now(timezone.utc)
        session = day if isinstance(day, date) else date.fromisoformat(str(day))
        attempted = not (fetch is None and _under_test())
        ohlc = None
        error = None
        if attempted:
            ohlc, error = _run_bounded(
                lambda: (fetch or _fetch_close)(session),
                timeout_s,
            )
            if not isinstance(ohlc, dict):
                ohlc = None
        write_day_label(
            session,
            open_=None if ohlc is None else ohlc.get("open"),
            high=None if ohlc is None else ohlc.get("high"),
            low=None if ohlc is None else ohlc.get("low"),
            close=None if ohlc is None else ohlc.get("close"),
            vix1d_open=_vix1d_open_for(session, db_path),
            db_path=db_path,
        )
        if attempted:
            if error:
                status, detail = "fail", error
            elif ohlc is None:
                status, detail = "fail", "empty"
            else:
                status, detail = "ok", "bars=1"
            _write_one_health(
                db_path, moment, "spy_close", status, 1 if ohlc else 0, None, detail
            )
    except Exception as exc:
        print(f"[Shadow] regime day label failed: {type(exc).__name__}")


def _label_row(day, db_path):
    session = day.isoformat() if isinstance(day, date) else str(day)
    conn = _connect_ro(db_path, timeout=1.0)
    if conn is None:
        return None
    try:
        if not _has_table(conn, "day_labels"):
            return None
        return conn.execute(
            "SELECT label, range_pct, vix1d_open FROM day_labels WHERE session_date=?",
            (session,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def session_clause(day, db_path=None) -> str:
    """One SESSION_FINAL field. It contains no pipe."""
    row = _label_row(day, db_path)
    label = None if row is None else row["label"]
    if label not in ("trend", "chop", "normal"):
        label = "NULL"
    range_pct = None if row is None else _float_or_none(row["range_pct"])
    rendered = "NULL" if range_pct is None else f"{range_pct:.2f}%"
    text = f"regime: {label}, range {rendered}"
    if "|" in text:
        return "regime: NULL, range NULL"
    return text


def brief_prior_line(now=None, db_path=None) -> str:
    """Prior session's label and that day's VIX1D open. Missing is NULL."""
    try:
        moment = _moment(now)
        today = _as_ct(moment).date().isoformat()
        conn = _connect_ro(db_path, timeout=1.0)
        label = None
        vix1d = None
        if conn is not None:
            try:
                if _has_table(conn, "day_labels"):
                    row = conn.execute(
                        """
                        SELECT label, vix1d_open FROM day_labels
                        WHERE session_date < ?
                        ORDER BY session_date DESC
                        LIMIT 1
                        """,
                        (today,),
                    ).fetchone()
                    if row is not None:
                        label = row["label"]
                        vix1d = _float_or_none(row["vix1d_open"])
            finally:
                conn.close()
        if label not in ("trend", "chop", "normal"):
            label = "NULL"
        vix_text = "NULL" if vix1d is None else f"{vix1d:.2f}"
        return f"SHADOW prior day: {label}. VIX1D at the open: {vix_text}"
    except Exception as exc:
        print(f"[Shadow] regime brief failed: {type(exc).__name__}")
        return "SHADOW prior day: NULL. VIX1D at the open: NULL"


def _fmt_r(value) -> str:
    number = _float_or_none(value)
    if number is None:
        return "NULL"
    return f"{number:.2f}"


def weekly_lines(db_path=None, now=None) -> list:
    """Trend, chop, and normal counts, and mean R_fill by label. Read only."""
    try:
        return _weekly_lines(db_path, now)
    except Exception as exc:
        print(f"[Shadow] regime week failed: {type(exc).__name__}")
        return [_SHADOW_HEADER, "  SHADOW regime: unavailable"]


def _weekly_lines(db_path, now) -> list:
    import shadow_news
    moment = _moment(now)
    conn = _connect_ro(db_path, timeout=1.0)
    if conn is None:
        return [_SHADOW_HEADER, "  SHADOW regime: not on this disk yet."]
    try:
        if not _has_table(conn, "day_labels"):
            return [_SHADOW_HEADER, "  SHADOW regime: not on this disk yet."]
        start, end = shadow_news.chicago_week(moment)
        start_day = start.date().isoformat()
        end_day = end.date().isoformat()
        rows = conn.execute(
            """
            SELECT session_date, label FROM day_labels
            WHERE session_date>=? AND session_date<?
            """,
            (start_day, end_day),
        ).fetchall()
        labels = {}
        counts = {"trend": 0, "chop": 0, "normal": 0}
        for row in rows:
            label = row["label"]
            if label not in counts:
                continue
            counts[label] += 1
            labels[row["session_date"]] = label
        sums = {"trend": 0.0, "chop": 0.0, "normal": 0.0}
        seen = {"trend": 0, "chop": 0, "normal": 0}
        if _has_table(conn, "trade_history"):
            import ops.metrics as metrics
            for trade in metrics.load_closed_trades(conn):
                session = trade.get("session_date")
                if session is None:
                    continue
                key = session.isoformat() if hasattr(session, "isoformat") else str(session)
                if key < start_day or key >= end_day:
                    continue
                label = labels.get(key)
                r_fill = trade.get("r_fill")
                if label is None or r_fill is None:
                    continue
                sums[label] += float(r_fill)
                seen[label] += 1
    finally:
        conn.close()
    means = {
        label: (sums[label] / seen[label] if seen[label] else None)
        for label in ("trend", "chop", "normal")
    }
    return [
        _SHADOW_HEADER,
        f"  trend days: {counts['trend']}",
        f"  chop days: {counts['chop']}",
        f"  normal days: {counts['normal']}",
        f"  mean R trend: {_fmt_r(means['trend'])}",
        f"  mean R chop: {_fmt_r(means['chop'])}",
        f"  mean R normal: {_fmt_r(means['normal'])}",
    ]
