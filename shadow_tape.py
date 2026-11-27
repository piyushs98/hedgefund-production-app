"""Shadow tape: backup, pillars, post-exit quotes, blocked names, quote quality.

Nothing here changes a score, the gate, a size, or an exit. A missing
number stays NULL. A failure is logged and swallowed.

Yahoo budget for one regular session, before the extra calls:
  regime marks already use 13 full scans x 4, plus one daily ATR read
  and one closing daily bar: 54 calls.
  D reads the chain that scan already fetched: 0 extra calls.
  C asks for one single-contract quote at +15, +30, +60 minutes and at
  14:45 CT, and only when that pass does not already hold the row.
  That is at most 4 calls per close. Eight closes are 32 calls.
  54 + 32 = 86, under the 150/day cap.
  Thirty closes (the per-ticker entry cap across the book) would be
  120 extra calls and would pass 150. Those calls stop at the cap.
  D is already at zero, so the cap reduces C first only after that.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
DAILY_CAP = 150
REGIME_CALLS = 13 * 4 + 2
POST_EXIT_PER_CLOSE = 4
BLOCKED_CALLS = 0
PER_SCAN_CAP = 5
PER_DAY_CAP = 15
SCORE_FLOOR = 70.0
BACKUP_KEEP_DAYS = 30
BACKUP_TIMEOUT_S = 8.0
TAPE_TIMEOUT_S = 8.0
YAHOO_SHADOW = "yahoo_shadow"

_guard = threading.Lock()
_running = False


def _under_test() -> bool:
    for name in sys.modules:
        if name == "tests" or name.startswith("tests."):
            return True
        if "." not in name and name.startswith("test_"):
            return True
    return False


def _float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _positive(value):
    number = _float(value)
    if number is None or number <= 0:
        return None
    return number


def _present(row: dict, key: str):
    if not isinstance(row, dict) or key not in row:
        return None
    return _float(row.get(key))


def _moment(now) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _as_ct(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT)


def _utc_iso(moment: datetime) -> str:
    return _moment(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_date(moment: datetime) -> str:
    return _as_ct(_moment(moment)).date().isoformat()


def _db(db_path):
    if db_path:
        return db_path
    import config
    return config.NEWS_DB_PATH


def _connect(db_path, timeout=2.0):
    path = _db(db_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def _connect_ro(db_path, timeout=1.0):
    path = _db(db_path)
    if not path or not os.path.exists(path):
        return None
    uri = "file:" + os.path.abspath(path) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def _has(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def ensure_schema(db_path=None) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_entry_detail (
                id INTEGER PRIMARY KEY,
                session_date TEXT,
                trade_id TEXT,
                ticker TEXT,
                direction TEXT,
                strike REAL,
                expiration TEXT,
                entry_at TEXT,
                t_score REAL,
                s_score REAL,
                liq_mult REAL,
                pivot_sub REAL,
                mom_sub REAL,
                drift_sub REAL,
                vol_mult REAL,
                squawk_s REAL,
                bid REAL,
                ask REAL,
                spread_pct REAL,
                quote_age_s REAL,
                iv REAL,
                volume REAL,
                open_interest REAL
            );
            CREATE TABLE IF NOT EXISTS shadow_exit_detail (
                id INTEGER PRIMARY KEY,
                session_date TEXT,
                trade_id TEXT,
                ticker TEXT,
                direction TEXT,
                strike REAL,
                expiration TEXT,
                exit_at TEXT,
                bid REAL,
                ask REAL,
                spread_pct REAL,
                quote_age_s REAL,
                iv REAL,
                volume REAL,
                open_interest REAL
            );
            CREATE TABLE IF NOT EXISTS shadow_post_exit (
                id INTEGER PRIMARY KEY,
                session_date TEXT,
                trade_id TEXT,
                ticker TEXT,
                direction TEXT,
                strike REAL,
                expiration TEXT,
                exit_at TEXT,
                exit_mid REAL,
                bid_15 REAL,
                mid_15 REAL,
                bid_30 REAL,
                mid_30 REAL,
                bid_60 REAL,
                mid_60 REAL,
                bid_1445 REAL,
                mid_1445 REAL,
                max_mid REAL,
                min_mid REAL,
                UNIQUE (session_date, ticker, strike, expiration, exit_at)
            );
            CREATE TABLE IF NOT EXISTS shadow_blocked (
                id INTEGER PRIMARY KEY,
                session_date TEXT,
                scanned_at TEXT,
                ticker TEXT,
                score REAL,
                block_reason TEXT,
                direction TEXT,
                strike REAL,
                expiration TEXT,
                ask REAL,
                UNIQUE (session_date, scanned_at, ticker)
            );
            CREATE TABLE IF NOT EXISTS shadow_blocked_marks (
                id INTEGER PRIMARY KEY,
                blocked_id INTEGER,
                scanned_at TEXT,
                bid REAL,
                mid REAL,
                UNIQUE (blocked_id, scanned_at)
            );
            CREATE TABLE IF NOT EXISTS shadow_yahoo_extra (
                session_date TEXT PRIMARY KEY,
                calls INTEGER NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def components_from_card(card) -> dict:
    """Pillars the scorer already returned. Unknown stays None."""
    metrics = {}
    if card is None:
        sub = {}
    elif isinstance(card, dict):
        metrics = card.get("metrics") or {}
        sub = metrics.get("subscores") or {}
    else:
        metrics = getattr(card, "metrics", None) or {}
        sub = metrics.get("subscores") or {} if isinstance(metrics, dict) else {}
    if not isinstance(sub, dict):
        sub = {}

    def from_sub(key, attr=None):
        if key in sub:
            return _float(sub.get(key))
        if attr and not isinstance(card, dict) and card is not None:
            return _float(getattr(card, attr, None))
        if attr and isinstance(card, dict):
            return _float(card.get(attr))
        return None

    return {
        "t_score": from_sub("T", "technical_score"),
        "s_score": from_sub("S", "sentiment_score"),
        "liq_mult": from_sub("liq_mult"),
        "pivot_sub": from_sub("pivot_sub"),
        "mom_sub": from_sub("mom_sub"),
        "drift_sub": from_sub("drift_sub"),
        "vol_mult": from_sub("vol_mult"),
        "squawk_s": from_sub("squawk_s"),
    }


def _quote_age(row: dict, now) -> float | None:
    for key in ("quote_age_s", "quote_age", "quoteAge"):
        if key in row:
            number = _float(row.get(key))
            if number is not None and number >= 0:
                return number
    if now is None:
        return None
    moment = _moment(now)
    for key in ("lastTradeDate", "quoteTime", "quote_time"):
        if key not in row:
            continue
        raw = row.get(key)
        if raw in (None, "", 0):
            continue
        parsed = None
        if isinstance(raw, datetime):
            parsed = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        else:
            text = str(raw).strip()
            for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    parsed = datetime.strptime(text.replace("Z", "+0000") if fmt.endswith("%z") and text.endswith("Z") else text, fmt)
                    break
                except ValueError:
                    parsed = None
            if parsed is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed is None:
            continue
        age = (moment - parsed.astimezone(timezone.utc)).total_seconds()
        if age >= 0:
            return age
    return None


def quote_from_row(row, now=None) -> dict:
    """Bid, ask, spread, age, IV, volume, open interest. Missing is None."""
    empty = {
        "bid": None,
        "ask": None,
        "spread_pct": None,
        "quote_age_s": None,
        "iv": None,
        "volume": None,
        "open_interest": None,
        "mid": None,
    }
    if not isinstance(row, dict):
        return empty
    bid = _positive(row.get("bid"))
    ask = _positive(row.get("ask"))
    mid = None
    spread = None
    if bid is not None and ask is not None and ask >= bid:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread = (ask - bid) / mid * 100.0
    iv = _present(row, "impliedVolatility")
    if iv is None:
        iv = _present(row, "iv")
    oi = _present(row, "openInterest")
    if oi is None:
        oi = _present(row, "open_interest")
    return {
        "bid": bid,
        "ask": ask,
        "spread_pct": spread,
        "quote_age_s": _quote_age(row, now),
        "iv": iv,
        "volume": _present(row, "volume"),
        "open_interest": oi,
        "mid": mid,
    }


def _side_key(direction) -> str:
    text = str(direction or "").upper()
    return "puts" if text.startswith("P") else "calls"


def _norm_direction(direction) -> str | None:
    text = str(direction or "").upper()
    if text.startswith("P"):
        return "PUT"
    if text.startswith("C"):
        return "CALL"
    return None


def chain_row(options_dict, contract) -> dict | None:
    """The chain row already fetched for this contract. No new call."""
    if not isinstance(options_dict, dict) or not isinstance(contract, dict):
        return None
    if options_dict.get("error") and not options_dict.get("chains"):
        return None
    expiration = contract.get("expiration")
    strike = _float(contract.get("strike"))
    if not expiration or strike is None:
        return None
    expiration = str(expiration)[:10]
    chains = options_dict.get("chains") or {}
    if not isinstance(chains, dict):
        return None
    sides = chains.get(expiration) or chains.get(str(contract.get("expiration")))
    if not isinstance(sides, dict):
        return None
    contracts = sides.get(_side_key(contract.get("direction"))) or []
    best = None
    best_dist = 1e18
    for item in contracts:
        if not isinstance(item, dict):
            continue
        got = _float(item.get("strike"))
        if got is None:
            continue
        dist = abs(got - strike)
        if dist < best_dist and dist <= 0.051:
            best_dist = dist
            best = item
    return best


def _identity(trade: dict) -> dict:
    opt = trade.get("option_contract") if isinstance(trade.get("option_contract"), dict) else {}
    strike = trade.get("strike")
    if strike is None:
        strike = opt.get("strike")
    expiration = trade.get("expiration") or opt.get("expiration")
    direction = _norm_direction(trade.get("direction") or opt.get("direction"))
    trade_id = trade.get("trade_id")
    return {
        "trade_id": None if trade_id is None else str(trade_id),
        "ticker": str(trade.get("ticker") or "").upper(),
        "direction": direction,
        "strike": _float(strike),
        "expiration": None if not expiration else str(expiration)[:10],
    }


def note_entry(
    *,
    ticker,
    direction,
    entry_at,
    components=None,
    quote=None,
    contract=None,
    db_path=None,
    trade_id=None,
) -> None:
    """One shadow row for a paper entry. Pillars and the chain quote."""
    try:
        if db_path is None and _under_test():
            return
        moment = _moment(entry_at)
        parts = components if isinstance(components, dict) else {}
        if not parts and contract is None and quote is None:
            parts = {}
        quality = quote_from_row(quote, moment)
        ident = _identity({
            "ticker": ticker,
            "direction": direction,
            "trade_id": trade_id,
            **(contract or {}),
        })
        ensure_schema(db_path)
        conn = _connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO shadow_entry_detail (
                    session_date, trade_id, ticker, direction, strike, expiration,
                    entry_at, t_score, s_score, liq_mult, pivot_sub, mom_sub,
                    drift_sub, vol_mult, squawk_s, bid, ask, spread_pct,
                    quote_age_s, iv, volume, open_interest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _session_date(moment),
                    ident["trade_id"],
                    ident["ticker"] or str(ticker or "").upper(),
                    ident["direction"],
                    ident["strike"],
                    ident["expiration"],
                    _utc_iso(moment),
                    parts.get("t_score"),
                    parts.get("s_score"),
                    parts.get("liq_mult"),
                    parts.get("pivot_sub"),
                    parts.get("mom_sub"),
                    parts.get("drift_sub"),
                    parts.get("vol_mult"),
                    parts.get("squawk_s"),
                    quality["bid"],
                    quality["ask"],
                    quality["spread_pct"],
                    quality["quote_age_s"],
                    quality["iv"],
                    quality["volume"],
                    quality["open_interest"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[Shadow] entry tape failed: {type(exc).__name__}")


def _options_for(scored, ticker: str):
    if not isinstance(scored, dict):
        return None
    ctx = scored.get(ticker) or scored.get(str(ticker or "").upper())
    if not isinstance(ctx, dict):
        return None
    if "options_dict" in ctx:
        return ctx.get("options_dict")
    return ctx


def _merged_closes(closed, open_before) -> list[dict]:
    book = {}
    for trade in open_before or []:
        if isinstance(trade, dict) and trade.get("ticker"):
            book[str(trade["ticker"]).upper()] = trade
    rows = []
    for item in closed or []:
        if not isinstance(item, dict) or not item.get("ticker"):
            continue
        ticker = str(item["ticker"]).upper()
        base = dict(book.get(ticker) or {})
        for key, value in item.items():
            if value is not None:
                base[key] = value
        base["ticker"] = ticker
        rows.append(base)
    return rows


def _exit_mid_from_chain(trade, options_dict, now):
    row = chain_row(options_dict, _identity(trade))
    return quote_from_row(row, now)


def _record_exit_quote(conn, trade, quality, moment) -> None:
    ident = _identity(trade)
    conn.execute(
        """
        INSERT INTO shadow_exit_detail (
            session_date, trade_id, ticker, direction, strike, expiration,
            exit_at, bid, ask, spread_pct, quote_age_s, iv, volume, open_interest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _session_date(moment),
            ident["trade_id"],
            ident["ticker"],
            ident["direction"],
            ident["strike"],
            ident["expiration"],
            _utc_iso(moment),
            quality.get("bid"),
            quality.get("ask"),
            quality.get("spread_pct"),
            quality.get("quote_age_s"),
            quality.get("iv"),
            quality.get("volume"),
            quality.get("open_interest"),
        ),
    )


def _ensure_post_row(conn, trade, quality, moment) -> None:
    ident = _identity(trade)
    exit_at = _utc_iso(moment)
    conn.execute(
        """
        INSERT OR IGNORE INTO shadow_post_exit (
            session_date, trade_id, ticker, direction, strike, expiration,
            exit_at, exit_mid, max_mid, min_mid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _session_date(moment),
            ident["trade_id"],
            ident["ticker"],
            ident["direction"],
            ident["strike"],
            ident["expiration"],
            exit_at,
            quality.get("mid"),
            quality.get("mid"),
            quality.get("mid"),
        ),
    )


def _recompute_range(conn, row_id: int) -> None:
    row = conn.execute("SELECT * FROM shadow_post_exit WHERE id=?", (row_id,)).fetchone()
    if row is None:
        return
    mids = [
        _float(row[name])
        for name in ("exit_mid", "mid_15", "mid_30", "mid_60", "mid_1445")
    ]
    mids = [item for item in mids if item is not None]
    if not mids:
        return
    conn.execute(
        "UPDATE shadow_post_exit SET max_mid=?, min_mid=? WHERE id=?",
        (max(mids), min(mids), row_id),
    )


def _due(exit_at: datetime, now: datetime, minutes: int) -> bool:
    target = exit_at + timedelta(minutes=minutes)
    return target - timedelta(seconds=60) <= now < target + timedelta(seconds=60)


def _clock_due_1445(exit_at: datetime, now: datetime) -> bool:
    local = _as_ct(now)
    clock = local.time()
    start = datetime.combine(local.date(), datetime.min.time(), CT).replace(
        hour=14, minute=44, tzinfo=CT
    )
    end = start + timedelta(minutes=2)
    if not (start <= local < end):
        return False
    exit_local = _as_ct(exit_at)
    limit = datetime.combine(local.date(), datetime.min.time(), CT).replace(
        hour=14, minute=45, second=59, tzinfo=CT
    )
    return exit_local <= limit


def _stopped(now: datetime) -> bool:
    local = _as_ct(now)
    return (local.hour, local.minute) >= (14, 55)


def _after_1445(now: datetime) -> bool:
    local = _as_ct(now)
    return (local.hour, local.minute) > (14, 45)


def _parse_iso(text: str) -> datetime | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _room(conn, session: str) -> int:
    """Regime health rows plus C's own counter. A C health row is not counted twice."""
    try:
        used = 0
        if _has(conn, "source_health"):
            start = datetime.fromisoformat(session).replace(tzinfo=CT)
            end = start + timedelta(days=1)
            row = conn.execute(
                """
                SELECT COUNT(*) FROM source_health
                WHERE source=? AND fetched_at>=? AND fetched_at<?
                  AND IFNULL(slot, '') != 'post_exit'
                """,
                (YAHOO_SHADOW, _utc_iso(start), _utc_iso(end.astimezone(timezone.utc))),
            ).fetchone()
            used += int(row[0] or 0)
        if _has(conn, "shadow_yahoo_extra"):
            row = conn.execute(
                "SELECT calls FROM shadow_yahoo_extra WHERE session_date=?",
                (session,),
            ).fetchone()
            used += int(row[0] or 0) if row else 0
        return DAILY_CAP - used
    except sqlite3.Error:
        return 0


def _spend(conn, session: str) -> None:
    conn.execute(
        """
        INSERT INTO shadow_yahoo_extra (session_date, calls) VALUES (?, 1)
        ON CONFLICT(session_date) DO UPDATE SET calls=calls+1
        """,
        (session,),
    )


def _ensure_health(conn) -> None:
    """Same connection as the tape. Does not open the live news file."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_health (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            http_status INTEGER,
            rows_new INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER,
            posts_returned INTEGER,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            cached_tokens INTEGER,
            posts_fetched INTEGER,
            users_fetched INTEGER,
            estimated_cost_usd REAL,
            fetched_at TEXT NOT NULL,
            detail TEXT,
            event_id TEXT,
            slot TEXT
        )
        """
    )


def _health(conn, moment, status: str, detail: str) -> None:
    try:
        import shadow_news
        if not _has(conn, "source_health"):
            _ensure_health(conn)
        shadow_news._record_health(
            conn,
            source=YAHOO_SHADOW,
            status=status,
            rows_new=1 if status == "ok" else 0,
            latency_ms=None,
            fetched_at=_utc_iso(moment),
            detail=(detail or "")[:240],
            slot="post_exit",
        )
    except Exception as exc:
        print(f"[Shadow] post-exit health failed: {type(exc).__name__}")


def _fetch_quote(fetch, trade, now):
    ident = _identity(trade)
    if fetch is None:
        if _under_test():
            return None
        from data_engineer import fetch_contract_quote
        import json
        raw = fetch_contract_quote(
            ident["ticker"],
            ident["expiration"],
            ident["strike"],
            ident["direction"] or "CALL",
        )
        body = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(body, dict) or body.get("error"):
            return None
        return quote_from_row(chain_row(body, ident), now)
    raw = fetch(ident["ticker"], ident["expiration"], ident["strike"], ident["direction"])
    if isinstance(raw, dict) and "bid" in raw and "mid" in raw:
        return raw
    return quote_from_row(raw, now)


def _apply_checkpoint(conn, row, slot: str, quality: dict) -> None:
    bid_col = f"bid_{slot}"
    mid_col = f"mid_{slot}"
    conn.execute(
        f"UPDATE shadow_post_exit SET {bid_col}=?, {mid_col}=? WHERE id=?",
        (quality.get("bid"), quality.get("mid"), row["id"]),
    )
    _recompute_range(conn, row["id"])


def _checkpoints(conn, now, scored, fetch, session: str) -> None:
    if not _has(conn, "shadow_post_exit"):
        return
    if _stopped(now):
        return
    moment = _moment(now)
    rows = conn.execute(
        "SELECT * FROM shadow_post_exit WHERE session_date=?",
        (session,),
    ).fetchall()
    for row in rows:
        exit_at = _parse_iso(row["exit_at"])
        if exit_at is None:
            continue
        pending = []
        if row["mid_15"] is None and _due(exit_at, moment, 15):
            pending.append("15")
        if row["mid_30"] is None and _due(exit_at, moment, 30):
            pending.append("30")
        if row["mid_60"] is None and _due(exit_at, moment, 60):
            pending.append("60")
        if row["mid_1445"] is None and _clock_due_1445(exit_at, moment):
            pending.append("1445")
        if not pending:
            continue
        trade = {
            "ticker": row["ticker"],
            "direction": row["direction"],
            "strike": row["strike"],
            "expiration": row["expiration"],
            "trade_id": row["trade_id"],
        }
        options = _options_for(scored, row["ticker"])
        quality = _exit_mid_from_chain(trade, options, moment) if options else quote_from_row(None)
        spent = False
        if quality.get("mid") is None:
            if _room(conn, session) <= 0:
                print("[Shadow] post-exit skipped: daily Yahoo cap")
                continue
            if not trade["expiration"] or trade["strike"] is None:
                continue
            _spend(conn, session)
            spent = True
            try:
                fetched = _fetch_quote(fetch, trade, moment)
                quality = fetched or quote_from_row(None)
                _health(conn, moment, "ok" if quality.get("bid") is not None or quality.get("ask") is not None else "fail", "post_exit")
            except Exception as exc:
                print(f"[Shadow] post-exit quote failed: {type(exc).__name__}")
                _health(conn, moment, "fail", type(exc).__name__)
                quality = quote_from_row(None)
        for slot in pending:
            _apply_checkpoint(conn, row, slot, quality)
        if spent or quality.get("mid") is not None:
            print(
                f"[Shadow] post-exit {row['ticker']} "
                + " ".join(f"+{slot}={quality.get('mid')}" for slot in pending)
            )


def _note_closes(conn, closed, open_before, scored, moment) -> None:
    for trade in _merged_closes(closed, open_before):
        options = _options_for(scored, trade.get("ticker"))
        quality = _exit_mid_from_chain(trade, options, moment)
        _record_exit_quote(conn, trade, quality, moment)
        _ensure_post_row(conn, trade, quality, moment)


def _decision_fields(item) -> dict | None:
    if isinstance(item, dict):
        ticker = item.get("ticker")
        score = _float(item.get("score"))
        admit = bool(item.get("admit"))
        reason = item.get("reason")
    else:
        ticker = getattr(item, "ticker", None)
        score = _float(getattr(item, "score", None))
        admit = bool(getattr(item, "admit", False))
        reason = getattr(item, "reason", None)
    if not ticker or score is None or score < SCORE_FLOOR or admit:
        return None
    text = None if reason in (None, "") else str(reason)[:120]
    return {"ticker": str(ticker).upper(), "score": score, "reason": text}


def _select_contract(select, ctx, now):
    options = ctx.get("options_dict") if isinstance(ctx, dict) else None
    pivot = ctx.get("pivot_data") if isinstance(ctx, dict) else None
    atr = ctx.get("atr_abs") if isinstance(ctx, dict) else None
    ticker = ctx.get("ticker") if isinstance(ctx, dict) else None
    if select is None:
        import strike_selector
        return strike_selector.select_optimal_contract(
            options or {},
            pivot or {},
            atr_abs=atr,
            now=now,
            ticker=ticker,
        )
    return select(options or {}, pivot or {}, atr, ticker)


def _note_blocked(conn, now, scored, decisions, select) -> None:
    if _after_1445(now):
        return
    session = _session_date(now)
    scanned = _utc_iso(now)
    chosen = []
    for item in decisions or []:
        fields = _decision_fields(item)
        if fields is None:
            continue
        chosen.append(fields)
    chosen.sort(key=lambda row: (-row["score"], row["ticker"]))
    already = conn.execute(
        "SELECT COUNT(*) FROM shadow_blocked WHERE session_date=?",
        (session,),
    ).fetchone()
    room = PER_DAY_CAP - int(already[0] or 0)
    take = chosen[: max(0, min(PER_SCAN_CAP, room))]
    for fields in take:
        ctx = {}
        if isinstance(scored, dict):
            ctx = dict(scored.get(fields["ticker"]) or {})
        ctx["ticker"] = fields["ticker"]
        contract = None
        try:
            contract = _select_contract(select, ctx, now)
        except Exception as exc:
            print(f"[Shadow] blocked select failed: {type(exc).__name__}")
            contract = None
        direction = None
        strike = None
        expiration = None
        ask = None
        if isinstance(contract, dict) and "error" not in contract:
            direction = _norm_direction(contract.get("direction"))
            strike = _float(contract.get("strike"))
            expiration = contract.get("expiration")
            expiration = None if not expiration else str(expiration)[:10]
            ask = _positive(contract.get("ask"))
        conn.execute(
            """
            INSERT OR IGNORE INTO shadow_blocked (
                session_date, scanned_at, ticker, score, block_reason,
                direction, strike, expiration, ask
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session,
                scanned,
                fields["ticker"],
                fields["score"],
                fields["reason"],
                direction,
                strike,
                expiration,
                ask,
            ),
        )
    if not _after_1445(now):
        _mark_blocked(conn, now, scored, session, scanned)


def _mark_blocked(conn, now, scored, session: str, scanned: str) -> None:
    rows = conn.execute(
        """
        SELECT * FROM shadow_blocked
        WHERE session_date=? AND scanned_at<?
        """,
        (session, scanned),
    ).fetchall()
    for row in rows:
        if row["strike"] is None or not row["expiration"]:
            quality = quote_from_row(None)
        else:
            options = _options_for(scored, row["ticker"])
            found = chain_row(options, {
                "expiration": row["expiration"],
                "strike": row["strike"],
                "direction": row["direction"],
            })
            quality = quote_from_row(found, now)
        conn.execute(
            """
            INSERT OR REPLACE INTO shadow_blocked_marks (
                blocked_id, scanned_at, bid, mid
            ) VALUES (?, ?, ?, ?)
            """,
            (row["id"], scanned, quality.get("bid"), quality.get("mid")),
        )


def _tape_work(now, scored, closed, open_before, decisions, db_path, fetch, select, blocked: bool) -> None:
    moment = _moment(now)
    session = _session_date(moment)
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        _note_closes(conn, closed, open_before, scored, moment)
        _checkpoints(conn, moment, scored, fetch, session)
        if blocked:
            _note_blocked(conn, moment, scored, decisions, select)
        conn.commit()
    finally:
        conn.close()


def _run(work, timeout_s: float) -> None:
    global _running
    with _guard:
        if _running:
            print("[Shadow] tape skipped: previous pass still running")
            return
        _running = True

    def _work():
        global _running
        try:
            work()
        finally:
            with _guard:
                _running = False

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_work)
    try:
        future.result(timeout=timeout_s)
    except FuturesTimeoutError:
        print("[Shadow] tape timed out")
    except Exception as exc:
        print(f"[Shadow] tape failed: {type(exc).__name__}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if future.cancelled():
            with _guard:
                _running = False


def on_exit_pass(now, *, scored=None, closed=None, open_before=None, db_path=None, fetch=None, timeout_s: float = TAPE_TIMEOUT_S) -> None:
    """Register closes and fill checkpoints that land on this exit pass."""
    if db_path is None and fetch is None and _under_test():
        return
    _run(
        lambda: _tape_work(now, scored, closed, open_before, None, db_path, fetch, None, False),
        timeout_s,
    )


def on_full_scan(
    now,
    *,
    scored=None,
    decisions=None,
    closed=None,
    open_before=None,
    db_path=None,
    fetch=None,
    select=None,
    timeout_s: float = TAPE_TIMEOUT_S,
) -> None:
    """Blocked names from this scan's chain, plus the same exit tape."""
    if db_path is None and fetch is None and select is None and _under_test():
        return
    _run(
        lambda: _tape_work(
            now, scored, closed, open_before, decisions, db_path, fetch, select, True
        ),
        timeout_s,
    )


def _backup_dir_value(news_path: str) -> str | None:
    if "rehearsal-" in str(news_path or ""):
        return None
    raw = os.environ.get("BACKUP_DIR", "").strip().strip('"').strip("'")
    if raw:
        return raw
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return None
    try:
        lines = open(env_path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return None
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, val = text.split("=", 1)
        if key.strip() != "BACKUP_DIR":
            continue
        got = val.strip().strip('"').strip("'")
        return got or None
    return None


def _refuse_path(path: str) -> None:
    name = os.path.basename(path).lower()
    if name in {".env", ".env.local"} or name.endswith((".pem", ".key")):
        raise RuntimeError("refused")
    if "secret" in name or "credential" in name:
        raise RuntimeError("refused")


def _sqlite_backup(src: str, dest: str) -> None:
    _refuse_path(src)
    _refuse_path(dest)
    if not os.path.isfile(src):
        raise FileNotFoundError(os.path.basename(src))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    source = sqlite3.connect(src, timeout=2.0)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def restore_db(src: str, dest: str) -> None:
    """Copy a backup file into dest with the SQLite backup API."""
    _sqlite_backup(src, dest)


def _bot_log(day: date) -> str:
    root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "logs", f"bot-{day.isoformat()}.log")


def _sources(day: date) -> list[tuple[str, str]]:
    import config
    news = config.NEWS_DB_PATH
    hedge = getattr(config, "HEDGE_DB_PATH", None) or os.path.join("data", "hedge_fund.db")
    webull = os.environ.get("WEBULL_EXEC_DB", "").strip()
    if not webull:
        webull = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "webull_exec.db")
    return [
        ("hedge_fund.db", hedge),
        ("news_room.db", news),
        ("webull_exec.db", webull),
        (f"bot-{day.isoformat()}.log", _bot_log(day)),
    ]


def _prune(folder: str, day: date) -> None:
    cutoff = day - timedelta(days=BACKUP_KEEP_DAYS - 1)
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        if len(name) != 10 or name[4] != "-" or name[7] != "-":
            continue
        try:
            stamp = date.fromisoformat(name)
        except ValueError:
            continue
        if stamp < cutoff:
            target = os.path.join(folder, name)
            if os.path.isdir(target):
                shutil.rmtree(target)


def run_backup(day: date, *, backup_dir: str, sources=None, log_path: str | None = None) -> bool:
    """One dated folder. Databases use the SQLite backup API. The log is a file."""
    _refuse_path(backup_dir)
    dest_root = os.path.join(backup_dir, day.isoformat())
    os.makedirs(dest_root, exist_ok=True)
    _prune(backup_dir, day)
    items = sources
    if items is None:
        items = _sources(day)
        if log_path:
            items = [(name, path) if not name.startswith("bot-") else (name, log_path) for name, path in items]
    for name, src in items:
        _refuse_path(src)
        dest = os.path.join(dest_root, name)
        if name.endswith(".db"):
            _sqlite_backup(src, dest)
        else:
            if not os.path.isfile(src):
                raise FileNotFoundError(name)
            if os.path.basename(src) != name:
                raise RuntimeError("refused")
            shutil.copyfile(src, dest)
    leaked = [
        name for name in os.listdir(dest_root)
        if name.lower().startswith(".env") or name.lower().endswith((".pem", ".key"))
    ]
    if leaked:
        raise RuntimeError("refused")
    return True


def _notify_backup_failure(day: date) -> None:
    if _under_test():
        return
    try:
        import broadcaster
        broadcaster.send_discord_alert(
            f"SHADOW backup FAILED for {day.isoformat()}. "
            "The session line is stored. No .env or keys were copied.",
            critical=False,
        )
    except Exception as exc:
        print(f"[Shadow] backup note failed: {type(exc).__name__}")


def backup_clause(day, *, backup_dir: str | None = None, sources=None, log_path: str | None = None, notify=True) -> str:
    """SESSION_FINAL field. Exactly one of the two tokens."""
    try:
        session = day if isinstance(day, date) else date.fromisoformat(str(day))
    except ValueError:
        session = date.today()
    try:
        if backup_dir is None and sources is None and _under_test():
            return "backup FAILED"
        import config
        news = config.NEWS_DB_PATH
        # Rehearsal copies live under a rehearsal- folder. Do not read .env
        # and do not post a failure note for that deliberate skip.
        rehearsal = "rehearsal-" in str(news or "")
        folder = backup_dir if backup_dir is not None else _backup_dir_value(news)
        if not folder:
            if notify and not rehearsal:
                _notify_backup_failure(session)
            return "backup FAILED"
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            pool.submit(
                run_backup,
                session,
                backup_dir=folder,
                sources=sources,
                log_path=log_path,
            ).result(timeout=BACKUP_TIMEOUT_S)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return "backup ok"
    except Exception as exc:
        print(f"[Shadow] backup failed: {type(exc).__name__}")
        if notify:
            _notify_backup_failure(session)
        return "backup FAILED"


def _fmt(value) -> str:
    number = _float(value)
    if number is None:
        return "NULL"
    return f"{number:.2f}"


def weekly_lines(db_path=None, now=None) -> list:
    """C, D, and E for the Chicago week. Read only. Marked SHADOW."""
    try:
        return _weekly(db_path, now)
    except Exception as exc:
        print(f"[Shadow] tape week failed: {type(exc).__name__}")
        return [
            "SHADOW POST-EXIT  (logged only; not a live rule)",
            f"  SHADOW tape: unavailable ({type(exc).__name__})",
        ]


def _weekly(db_path, now) -> list:
    import shadow_news
    moment = _moment(now)
    conn = _connect_ro(db_path)
    header_c = "SHADOW POST-EXIT  (logged only; not a live rule)"
    header_d = "SHADOW BLOCKED  (logged only; not a live rule)"
    header_e = "SHADOW QUOTE QUALITY  (logged only; not a live rule)"
    if conn is None:
        missing = "  not on this disk yet."
        return [header_c, missing, header_d, missing, header_e, missing]
    try:
        if not (_has(conn, "shadow_post_exit") or _has(conn, "shadow_blocked") or _has(conn, "shadow_entry_detail")):
            missing = "  not on this disk yet."
            return [header_c, missing, header_d, missing, header_e, missing]
        start, end = shadow_news.chicago_week(moment)
        start_day = start.date().isoformat()
        end_day = end.date().isoformat()

        def _rows(table: str):
            if not _has(conn, table):
                return []
            return conn.execute(
                f"SELECT * FROM {table} WHERE session_date>=? AND session_date<?",
                (start_day, end_day),
            ).fetchall()

        post = _rows("shadow_post_exit")
        ranges = []
        filled = {"15": 0, "30": 0, "60": 0, "1445": 0}
        for row in post:
            for slot in filled:
                if _float(row[f"mid_{slot}"]) is not None:
                    filled[slot] += 1
            hi = _float(row["max_mid"])
            lo = _float(row["min_mid"])
            if hi is not None and lo is not None:
                ranges.append(hi - lo)
        blocked = _rows("shadow_blocked")
        later = 0
        asks = []
        if blocked and _has(conn, "shadow_blocked_marks"):
            ids = [row["id"] for row in blocked]
            marks = {
                row["blocked_id"]
                for row in conn.execute(
                    f"""
                    SELECT blocked_id, mid FROM shadow_blocked_marks
                    WHERE blocked_id IN ({",".join("?" for _ in ids)})
                      AND mid IS NOT NULL
                    """,
                    ids,
                )
            } if ids else set()
            later = sum(1 for row in blocked if row["id"] in marks)
        for row in blocked:
            ask = _float(row["ask"])
            if ask is not None:
                asks.append(ask)
        entries = _rows("shadow_entry_detail")
        exits = _rows("shadow_exit_detail")
        entry_spreads = [_float(row["spread_pct"]) for row in entries]
        exit_spreads = [_float(row["spread_pct"]) for row in exits]
        entry_spreads = [item for item in entry_spreads if item is not None]
        exit_spreads = [item for item in exit_spreads if item is not None]

        def _mean(values):
            if not values:
                return None
            return sum(values) / len(values)

        return [
            header_c,
            f"  closes tracked: {len(post)}",
            f"  +15 filled: {filled['15']}  +30 filled: {filled['30']}  "
            f"+60 filled: {filled['60']}  14:45 filled: {filled['1445']}",
            f"  mean mid range: {_fmt(_mean(ranges))}",
            header_d,
            f"  candidates: {len(blocked)}",
            f"  with a later quote: {later}",
            f"  mean ask at the block: {_fmt(_mean(asks))}",
            header_e,
            f"  entry quotes: {len(entries)}  exit quotes: {len(exits)}",
            f"  mean entry spread %: {_fmt(_mean(entry_spreads))}",
            f"  mean exit spread %: {_fmt(_mean(exit_spreads))}",
        ]
    finally:
        conn.close()
