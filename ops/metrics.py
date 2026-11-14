"""Go/no-go math from the persistent tables. No network, no writes.

Fill P&L is (exit bid − entry ask) × 100 × qty, the same series the bot
stores on trade_history.pnl_fill. Planned risk stays on the mid premium,
because that is what the stop is measured from.
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import measurement_exclusions
from ops.store import connect_ro, table_columns, table_count, table_names

CHICAGO = ZoneInfo("America/Chicago")
MULTIPLIER = 100

# Headline figures use this stamp and drop excluded_from_measurement=y.
# Oct 2 posted v7a25760. v9c3cc4d closed on 2026-11-13 and is its own section.
# MEASURED_VERSION matches fill_accounting.TRADING_STAMP. Set with the pin.
MEASURED_VERSION = "v91c6693"
PRIOR_MEASURED_VERSION = "v9c3cc4d"
OCT2_VERSION = "v7a25760"
REGIME_MOVE = 0.005
BOOTSTRAP_DRAWS = 10000
BOOTSTRAP_SEED = 0x9C3CC4D

# Pre-registered bars. Printed beside the sample. Not a live knob.
BARS = {
    "expectancy_r": 0.15,
    "min_trades": 300,
    "profit_factor": 1.3,
    "max_drawdown_pct": 20.0,
    "mfe_capture": 0.35,
    "stop_slippage_pct": 15.0,
}


def parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def chicago_date(raw: Any):
    dt = parse_dt(raw)
    if dt is None:
        return None
    return dt.astimezone(CHICAGO).date()


def _f(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        out = float(val)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _load_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def quantity_of(meta: dict[str, Any]) -> int:
    for key in ("quantity", "qty", "contracts"):
        raw = meta.get(key)
        if raw is None and isinstance(meta.get("option_contract"), dict):
            raw = meta["option_contract"].get(key)
        if raw is None:
            continue
        try:
            qty = int(raw)
        except (TypeError, ValueError):
            continue
        if qty >= 0:
            return qty
    return 1


def exit_reason(notes: Any) -> str:
    text = str(notes or "").strip()
    if text.startswith("EXIT:"):
        return text[5:].strip() or "UNKNOWN"
    if not text:
        return "UNLABELED"
    return text


def is_open_marker(notes: Any) -> bool:
    return str(notes or "").strip() == "PAPER_BUY_OPEN"


def planned_risk(entry_mid: float | None, stop: float | None, qty: int) -> float | None:
    if entry_mid is None or stop is None or qty < 1:
        return None
    if entry_mid <= stop:
        return None
    risk = (entry_mid - stop) * MULTIPLIER * qty
    if risk <= 1e-9:
        return None
    return risk


def _select(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[dict[str, Any]]:
    present = set(table_columns(conn, table))
    if not present:
        return []
    wanted = [c for c in columns if c in present]
    if not wanted:
        return []
    sql = "SELECT " + ", ".join(wanted) + f" FROM {table}"
    rows = []
    for rec in conn.execute(sql):
        item = {c: rec[c] for c in wanted}
        rows.append(item)
    return rows


def load_closed_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if "trade_history" not in table_names(conn):
        return []
    raw_rows = _select(
        conn,
        "trade_history",
        [
            "id", "closed_at", "ticker", "direction", "strike", "expiration",
            "entry_price", "exit_price", "pnl", "contract_json", "notes",
            "slippage", "pnl_mid", "pnl_fill", "entry_mid", "entry_ask",
            "exit_mid", "exit_bid", "fill_est",
        ],
    )
    closed = []
    for row in raw_rows:
        if is_open_marker(row.get("notes")):
            continue
        meta = _load_meta(row.get("contract_json"))
        qty = quantity_of(meta)
        entry_mid = _f(row.get("entry_mid"))
        if entry_mid is None:
            entry_mid = _f(row.get("entry_price"))
        entry_ask = _f(row.get("entry_ask"))
        exit_mid = _f(row.get("exit_mid"))
        if exit_mid is None:
            exit_mid = _f(row.get("exit_price"))
        exit_bid = _f(row.get("exit_bid"))
        pnl_mid = _f(row.get("pnl_mid"))
        if pnl_mid is None:
            pnl_mid = _f(row.get("pnl"))
        pnl_fill = _f(row.get("pnl_fill"))
        if pnl_fill is None:
            pnl_fill = pnl_mid
        stop = _f(meta.get("stop_loss"))
        risk = planned_risk(entry_mid, stop, qty)
        r_fill = None
        if pnl_fill is not None and risk is not None:
            r_fill = pnl_fill / risk
        spread_pct = None
        if entry_mid and entry_ask and entry_mid > 0:
            spread_pct = (entry_ask - entry_mid) / entry_mid * 100.0
        stop_slip = None
        reason = exit_reason(row.get("notes"))
        if "STOP_LOSS" in reason and pnl_mid is not None and risk is not None:
            actual_loss = -pnl_mid
            stop_slip = (actual_loss - risk) / risk * 100.0
        closed.append({
            "id": row.get("id"),
            "closed_at": row.get("closed_at"),
            "session_date": chicago_date(row.get("closed_at")),
            "ticker": row.get("ticker"),
            "direction": row.get("direction"),
            "strike": row.get("strike"),
            "expiration": row.get("expiration"),
            "qty": qty,
            "entry_mid": entry_mid,
            "entry_ask": entry_ask,
            "exit_mid": exit_mid,
            "exit_bid": exit_bid,
            "pnl_mid": pnl_mid,
            "pnl_fill": pnl_fill,
            "planned_risk": risk,
            "r_fill": r_fill,
            "entry_spread_pct": spread_pct,
            "stop_slippage_pct": stop_slip,
            "reason": reason,
            "fill_est": bool(row.get("fill_est")),
            "trade_id": str(meta.get("trade_id") or "") or None,
            "meta": meta,
            "entry_score": _f(meta.get("entry_score")),
            "version": str(meta.get("version") or "") or None,
            "carried": _carried_flag(
                meta.get("entry_timestamp") or meta.get("entry_time"),
                chicago_date(row.get("closed_at")),
            ),
        })
    closed.sort(key=lambda t: (str(t.get("closed_at") or ""), t.get("id") or 0))
    return closed


def load_marks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if "position_marks" not in table_names(conn):
        return []
    return _select(
        conn,
        "position_marks",
        ["ts", "trade_id", "ticker", "mark", "pnl_pct", "entry_price",
         "stop_loss", "take_profit", "bid", "ask"],
    )


def load_open_markers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if "trade_history" not in table_names(conn):
        return []
    rows = _select(
        conn,
        "trade_history",
        ["closed_at", "ticker", "notes", "contract_json", "entry_price"],
    )
    out = []
    for row in rows:
        if not is_open_marker(row.get("notes")):
            continue
        meta = _load_meta(row.get("contract_json"))
        out.append({
            "closed_at": row.get("closed_at"),
            "session_date": chicago_date(row.get("closed_at")),
            "ticker": row.get("ticker"),
            "qty": quantity_of(meta),
            "entry_price": _f(row.get("entry_price")),
        })
    return out


def load_ledger(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if "portfolio_ledger" not in table_names(conn):
        return None
    cols = table_columns(conn, "portfolio_ledger")
    if "id" not in cols:
        return None
    wanted = [
        c for c in (
            "buying_power", "total_realized_pnl", "total_realized_pnl_fill", "updated_at",
        ) if c in cols
    ]
    sql = "SELECT " + ", ".join(wanted) + " FROM portfolio_ledger WHERE id = 1"
    row = conn.execute(sql).fetchone()
    if row is None:
        return None
    return {c: row[c] for c in wanted}


def _outage_row(row: dict[str, Any]) -> bool:
    blob = " ".join(
        str(row.get(key) or "")
        for key in ("reasons_json", "raw_metrics_json", "action_flag")
    )
    return "data_unavailable" in blob


def load_sessions(conn: sqlite3.Connection) -> dict[str, Any]:
    """Count Chicago days in backtest_telemetry.

    A day is excluded when every stored scan that day is entirely
    data_unavailable. A day with no telemetry is neither counted nor
    excluded: a scan that died before writing a card is not in this table.
    """
    empty = {
        "counted": [],
        "excluded": [],
        "scans": 0,
        "note": "backtest_telemetry is absent",
    }
    if conn is None or "backtest_telemetry" not in table_names(conn):
        return empty
    rows = _select(
        conn,
        "backtest_telemetry",
        ["timestamp", "scan_id", "ticker", "reasons_json", "raw_metrics_json", "action_flag"],
    )
    by_day: dict[Any, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        day = chicago_date(row.get("timestamp"))
        scan = str(row.get("scan_id") or "")
        if day is None or not scan:
            continue
        by_day[day][scan].append(_outage_row(row))
    counted = []
    excluded = []
    n_scans = 0
    for day in sorted(by_day):
        scans = by_day[day]
        n_scans += len(scans)
        if scans and all(flags and all(flags) for flags in scans.values()):
            excluded.append(day.isoformat())
        else:
            counted.append(day.isoformat())
    note = (
        "A session day is excluded only when every backtest_telemetry scan "
        "that day is data_unavailable on every ticker row. A scan that "
        "never wrote a card is not in this table, so a total outage with "
        "zero rows is not counted and not excluded."
    )
    return {
        "counted": counted,
        "excluded": excluded,
        "scans": n_scans,
        "note": note,
    }


def mfe_capture(trades: list[dict[str, Any]], marks: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum of fill P&L over sum of peak dollars, plus the median trade ratio.

    Peak uses position_marks.pnl_pct (percent of entry) and the close mid
    print, matching the MFE on the TRADE line. Marks are mid. Fill P&L is
    the ask/bid series. Trades with no positive peak stay out of both figures.
    The ratio is one number for the whole set. The median is the middle
    per-trade ratio (the average of the two middle ratios when the count is even).
    """
    by_id: dict[str, list[float]] = defaultdict(list)
    for mark in marks:
        tid = str(mark.get("trade_id") or "")
        pct = _f(mark.get("pnl_pct"))
        if tid and pct is not None:
            by_id[tid].append(pct)
    ratios = []
    pnl_sum = 0.0
    peak_sum = 0.0
    missing = 0
    nonpositive = 0
    for trade in trades:
        pcts = list(by_id.get(trade.get("trade_id") or "", []))
        entry = trade.get("entry_mid")
        exit_mid = trade.get("exit_mid")
        if entry and exit_mid and entry > 0:
            pcts.append((exit_mid - entry) / entry * 100.0)
        if not pcts:
            missing += 1
            continue
        mfe_pct = max(pcts)
        if mfe_pct <= 0 or not entry or entry <= 0:
            nonpositive += 1
            continue
        mfe_dollars = (mfe_pct / 100.0) * entry * MULTIPLIER * int(trade["qty"])
        pnl_fill = trade.get("pnl_fill")
        if mfe_dollars <= 1e-9 or pnl_fill is None:
            missing += 1
            continue
        ratios.append(pnl_fill / mfe_dollars)
        pnl_sum += float(pnl_fill)
        peak_sum += mfe_dollars
    ratio = (pnl_sum / peak_sum) if peak_sum > 1e-9 else None
    median = statistics.median(ratios) if ratios else None
    return {
        "ratio": ratio,
        "median": median,
        "n": len(ratios),
        "missing": missing,
        "nonpositive": nonpositive,
        "pnl_fill": pnl_sum if ratios else None,
        "peak_dollars": peak_sum if ratios else None,
    }


def profit_factor(trades: list[dict[str, Any]]) -> float | None:
    wins = 0.0
    losses = 0.0
    for trade in trades:
        pnl = trade.get("pnl_fill")
        if pnl is None or pnl == 0:
            continue
        if pnl > 0:
            wins += pnl
        else:
            losses += -pnl
    if losses <= 1e-9:
        return None
    return wins / losses


def max_drawdown(pnls: list[float], seed: float) -> tuple[float, float | None]:
    equity = float(seed)
    peak = equity
    worst = 0.0
    for pnl in pnls:
        equity += float(pnl)
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > worst:
            worst = drop
    pct = (worst / seed * 100.0) if seed else None
    return worst, pct


def _gap_excluded(trade: dict[str, Any]) -> bool:
    """The four unmonitored lots, by trade id or the close flag."""
    if measurement_exclusions.is_excluded(trade.get("trade_id")):
        return True
    meta = trade.get("meta")
    if not isinstance(meta, dict):
        return False
    return str(meta.get("excluded_from_measurement") or "").lower() == "y"


def _carried_flag(entry_raw: Any, close_day) -> str | None:
    entry_day = chicago_date(entry_raw)
    if entry_day is None or close_day is None:
        return None
    return "y" if entry_day != close_day else "n"


def _cp(direction: Any) -> str:
    text = str(direction or "").upper()
    if "PUT" in text or text == "P":
        return "P"
    if "CALL" in text or text == "C":
        return "C"
    return ""


def _strike_key(val: Any) -> str:
    number = _f(val)
    if number is None:
        return str(val or "")
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:g}"


def _hhmm_chicago(raw: Any) -> str | None:
    dt = parse_dt(raw)
    if dt is None:
        return None
    return dt.astimezone(CHICAGO).strftime("%H:%M")


def _minute_distance(left: str | None, right: str | None) -> int:
    def mins(text: str | None) -> int | None:
        if not text or ":" not in text:
            return None
        hour, minute = text.split(":", 1)
        try:
            return int(hour) * 60 + int(minute)
        except ValueError:
            return None
    a = mins(left)
    b = mins(right)
    if a is None or b is None:
        return 10_000
    return abs(a - b)


def _trade_match_key(trade: dict[str, Any]) -> tuple:
    day = trade.get("session_date")
    return (
        day.isoformat() if day is not None else "",
        str(trade.get("ticker") or ""),
        _cp(trade.get("direction")),
        _strike_key(trade.get("strike")),
        str(trade.get("expiration") or "")[:10],
        str(int(trade.get("qty") or 0)),
        str(trade.get("reason") or ""),
    )


def _read_trade_stamps(logs_dir) -> list[dict[str, Any]]:
    """Posted TRADE lines. Read-only. Version lives on the line, not in SQLite."""
    path = Path(logs_dir) if logs_dir else None
    if path is None or not path.is_dir():
        return []
    stamps = []
    seen = set()
    for file in sorted(path.glob("bot-*.log")):
        try:
            handle = file.open(errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                idx = line.find("TRADE|")
                if idx < 0:
                    continue
                parts = line[idx:].strip().split("|")
                if len(parts) < 15 or parts[0] != "TRADE":
                    continue
                qty_raw = parts[7]
                if qty_raw.startswith("qty"):
                    qty_raw = qty_raw[3:]
                qty_num = _f(qty_raw)
                qty_key = str(int(qty_num)) if qty_num is not None else qty_raw
                stamp = {
                    "version": parts[1],
                    "exit_hhmm": parts[11],
                    "excluded": any(
                        piece.strip() == "excluded_from_measurement=y" for piece in parts
                    ),
                    "key": (
                        parts[2],
                        parts[3],
                        parts[4],
                        _strike_key(parts[5]),
                        parts[6][:10],
                        qty_key,
                        parts[14],
                    ),
                }
                identity = (stamp["version"], stamp["key"], stamp["exit_hhmm"], stamp["excluded"])
                if identity in seen:
                    continue
                seen.add(identity)
                stamps.append(stamp)
    return stamps


def attach_trade_stamps(trades: list[dict[str, Any]], logs_dir) -> dict[str, int]:
    """Copy version from the posted TRADE line onto the matching closed row."""
    stamps = _read_trade_stamps(logs_dir)
    buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for stamp in stamps:
        buckets[stamp["key"]].append(stamp)
    used: set[int] = set()
    matched = 0
    for trade in trades:
        choices = [
            stamp for stamp in buckets.get(_trade_match_key(trade), [])
            if id(stamp) not in used
        ]
        if not choices:
            continue
        target = _hhmm_chicago(trade.get("closed_at"))
        choices.sort(key=lambda stamp: _minute_distance(target, stamp["exit_hhmm"]))
        best = choices[0]
        used.add(id(best))
        trade["version"] = best["version"]
        if best["excluded"]:
            meta = trade.get("meta")
            if isinstance(meta, dict):
                meta["excluded_from_measurement"] = "y"
        matched += 1
    return {
        "matched": matched,
        "stamp_rows": len(stamps),
        "unmatched_stamps": len(stamps) - matched,
    }


def _session_row(parts: list[str]) -> dict[str, Any] | None:
    if len(parts) < 15 or parts[0] != "SESSION":
        return None
    return {
        "version": parts[1],
        "date": parts[2],
        "spy_open": _field_number(parts[13]),
        "spy_close": _field_number(parts[14]),
    }


def load_session_lines(logs_dir, news: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """SESSION lines from the log, plus any database row the log does not have.

    The database row is written whether or not Discord accepts the line.
    A date already parsed from the log is left as the log recorded it.
    """
    path = Path(logs_dir) if logs_dir else None
    by_date: dict[str, dict[str, Any]] = {}
    if path is not None and path.is_dir():
        for file in sorted(path.glob("bot-*.log")):
            try:
                handle = file.open(errors="replace")
            except OSError:
                continue
            with handle:
                for line in handle:
                    idx = line.find("SESSION|")
                    if idx < 0:
                        continue
                    parts = line[idx:].strip().split("|")
                    row = _session_row(parts)
                    if row is None:
                        continue
                    by_date[row["date"]] = row
    if news is not None and "session_lines" in table_names(news):
        for stored in _select(news, "session_lines", ["session_date", "line"]):
            text = str(stored.get("line") or "")
            row = _session_row(text.split("|"))
            if row is None or row["date"] in by_date:
                continue
            by_date[row["date"]] = row
    return [by_date[day] for day in sorted(by_date)]


def _field_number(raw: str) -> float | None:
    bits = str(raw).split()
    if len(bits) < 2:
        return None
    return _f(bits[-1])


def classify_regime(spy_open: float | None, spy_close: float | None) -> str | None:
    """|change| >= 0.5% is trend up or trend down. Otherwise chop."""
    if spy_open is None or spy_close is None or spy_open <= 0:
        return None
    change = (spy_close - spy_open) / spy_open
    if change >= REGIME_MOVE:
        return "trend_up"
    if change <= -REGIME_MOVE:
        return "trend_down"
    return "chop"


def _hold_bucket(trades: list[dict[str, Any]]) -> dict[str, Any]:
    r_vals = [t["r_fill"] for t in trades if t.get("r_fill") is not None]
    return {
        "n": len(trades),
        "n_r": len(r_vals),
        "pnl_fill": sum((t.get("pnl_fill") or 0.0) for t in trades),
        "mean_r": (sum(r_vals) / len(r_vals)) if r_vals else None,
    }


def hold_split(trades: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {
        "same_day": [],
        "carried": [],
        "unknown": [],
    }
    for trade in trades:
        flag = trade.get("carried")
        if flag == "y":
            groups["carried"].append(trade)
        elif flag == "n":
            groups["same_day"].append(trade)
        else:
            groups["unknown"].append(trade)
    return {name: _hold_bucket(rows) for name, rows in groups.items()}


def regime_table(
    sessions: list[dict[str, Any]], trades: list[dict[str, Any]], version: str
) -> dict[str, Any]:
    """Class measured SESSION lines and attach that day's closed-trade P&L."""
    posted = {}
    for row in sessions:
        if row.get("version") != version:
            continue
        posted[row["date"]] = row
    classes = {"trend_up": [], "trend_down": [], "chop": []}
    changes = {}
    for day in sorted(posted):
        row = posted[day]
        label = classify_regime(row.get("spy_open"), row.get("spy_close"))
        if label is None:
            continue
        classes[label].append(day)
        op = row.get("spy_open")
        cl = row.get("spy_close")
        changes[day] = (cl - op) / op if op else None
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        day = trade.get("session_date")
        if day is not None:
            by_day[day.isoformat()].append(trade)

    def pack(dates: list[str]) -> dict[str, Any]:
        rows = [trade for day in dates for trade in by_day.get(day, [])]
        bucket = _hold_bucket(rows)
        bucket["sessions"] = len(dates)
        bucket["dates"] = [
            {"date": day, "change": changes.get(day)} for day in dates
        ]
        return bucket

    classed = set(posted)
    missing = sorted(day for day in by_day if day not in classed)
    table = {name: pack(dates) for name, dates in classes.items()}
    table["no_session_line"] = pack(missing)
    return table


def daily_pnls(trades: list[dict[str, Any]]) -> list[float]:
    by_day: dict[Any, float] = defaultdict(float)
    for trade in trades:
        day = trade.get("session_date")
        if day is None:
            continue
        by_day[day] += trade.get("pnl_fill") or 0.0
    return [by_day[day] for day in sorted(by_day)]


def day_block_expectancy(
    trades: list[dict[str, Any]],
    *,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any] | None:
    """90% interval for mean R. Resample close-dates, then pool the trades.

    The point expectancy is the mean R of every trade. A resample draws the
    session count with replacement and takes the mean R of the trades on
    the drawn dates, so a day with more trades keeps more weight.
    """
    blocks: dict[Any, list[float]] = defaultdict(list)
    for trade in trades:
        day = trade.get("session_date")
        r_fill = trade.get("r_fill")
        if day is None or r_fill is None:
            continue
        blocks[day].append(r_fill)
    days = [blocks[day] for day in sorted(blocks)]
    if not days:
        return None
    rng = random.Random(seed)
    n_days = len(days)
    stats = []
    for _ in range(draws):
        picked = [days[rng.randrange(n_days)] for _ in range(n_days)]
        values = [r for block in picked for r in block]
        if values:
            stats.append(sum(values) / len(values))
    if not stats:
        return None
    cuts = statistics.quantiles(stats, n=20, method="inclusive")
    return {
        "low": cuts[0],
        "high": cuts[-1],
        "draws": draws,
        "seed": seed,
        "sessions": n_days,
        "trades": sum(len(block) for block in days),
    }


def score_groups(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """all, 80+, and 70–79. A missing score is counted and left out of both buckets."""
    hi: list[dict[str, Any]] = []
    mid: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for trade in trades:
        score = trade.get("entry_score")
        if score is None:
            other.append(trade)
        elif score >= 80:
            hi.append(trade)
        elif score >= 70:
            mid.append(trade)
        else:
            other.append(trade)
    return {"all": list(trades), "80+": hi, "70-79": mid, "other": other}


def headline_metrics(
    trades: list[dict[str, Any]],
    session_lines: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    seed: float | None,
    version: str,
) -> dict[str, Any]:
    """The report headlines for one set of closes. Seed is the account seed."""
    r_vals = [t["r_fill"] for t in trades if t.get("r_fill") is not None]
    pf = profit_factor(trades)
    gross_wins = sum(t["pnl_fill"] for t in trades if (t.get("pnl_fill") or 0) > 0)
    gross_losses = sum(-(t["pnl_fill"] or 0) for t in trades if (t.get("pnl_fill") or 0) < 0)
    fill_sum = sum(t["pnl_fill"] or 0.0 for t in trades)
    mid_sum = sum(t["pnl_mid"] or 0.0 for t in trades)
    dd_dollars, dd_pct = (None, None)
    dd_daily_dollars, dd_daily_pct = (None, None)
    if trades and seed is not None and seed > 0:
        dd_dollars, dd_pct = max_drawdown(
            [t["pnl_fill"] or 0.0 for t in trades], seed
        )
        dd_daily_dollars, dd_daily_pct = max_drawdown(daily_pnls(trades), seed)
    spreads = [t["entry_spread_pct"] for t in trades if t.get("entry_spread_pct") is not None]
    stops = [t["stop_slippage_pct"] for t in trades if t.get("stop_slippage_pct") is not None]
    by_reason: dict[str, dict[str, float]] = {}
    for trade in trades:
        bucket = by_reason.setdefault(
            trade["reason"], {"n": 0, "pnl_fill": 0.0, "r_sum": 0.0, "r_n": 0}
        )
        bucket["n"] += 1
        bucket["pnl_fill"] += trade["pnl_fill"] or 0.0
        if trade.get("r_fill") is not None:
            bucket["r_sum"] += trade["r_fill"]
            bucket["r_n"] += 1
    return {
        "n": len(trades),
        "n_with_r": len(r_vals),
        "n_missing_risk": len(trades) - len(r_vals),
        "expectancy_r": (sum(r_vals) / len(r_vals)) if r_vals else None,
        "expectancy_ci": day_block_expectancy(trades),
        "profit_factor": pf,
        "gross_wins": gross_wins,
        "gross_losses": gross_losses,
        "no_losses": gross_losses <= 1e-9,
        "max_dd_dollars": dd_dollars,
        "max_dd_pct": dd_pct,
        "max_dd_daily_dollars": dd_daily_dollars,
        "max_dd_daily_pct": dd_daily_pct,
        "mfe": mfe_capture(trades, marks),
        "entry_spread_pct": (sum(spreads) / len(spreads)) if spreads else None,
        "n_entry_spread": len(spreads),
        "stop_slippage_pct": (sum(stops) / len(stops)) if stops else None,
        "n_stop_slippage": len(stops),
        "fill_est": sum(1 for t in trades if t.get("fill_est")),
        "realized_fill": fill_sum,
        "realized_mid": mid_sum,
        "spread_cost": mid_sum - fill_sum,
        "by_reason": by_reason,
        "regimes": regime_table(session_lines, trades, version),
        "hold": hold_split(trades),
    }


def _buckets_for(
    trades: list[dict[str, Any]],
    session_lines: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    seed: float | None,
    version: str,
) -> dict[str, Any]:
    groups = score_groups(trades)
    out = {
        key: headline_metrics(groups[key], session_lines, marks, seed, version)
        for key in ("all", "80+", "70-79")
    }
    out["unscored"] = len(groups["other"])
    return out


def load_carry_shadow(conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    if conn is None or "carry_shadow" not in table_names(conn):
        return []
    return _select(
        conn,
        "carry_shadow",
        [
            "ticker", "entry_score", "flatten_on", "next_session", "status",
            "flatten_pnl", "flatten_r", "shadow_reason", "shadow_pnl", "shadow_r",
        ],
    )


def _sample_side(trades: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Oct 2 headline. The session line supplies spy_open and spy_close."""
    bucket = _hold_bucket(trades)
    row = next((s for s in sessions if s.get("version") == OCT2_VERSION), None)
    change = None
    label = None
    if row is not None:
        label = classify_regime(row.get("spy_open"), row.get("spy_close"))
        opened = row.get("spy_open")
        closed = row.get("spy_close")
        if opened:
            change = (closed - opened) / opened
    bucket["spy_open"] = None if row is None else row.get("spy_open")
    bucket["spy_close"] = None if row is None else row.get("spy_close")
    bucket["change"] = change
    bucket["regime"] = label
    bucket["date"] = None if row is None else row.get("date")
    return bucket


def summarize(news_path, hedge_path, open_debit: float = 0.0, logs_dir=None) -> dict[str, Any]:
    news = connect_ro(news_path)
    hedge = connect_ro(hedge_path)
    try:
        loaded = load_closed_trades(news) if news is not None else []
        stamps = attach_trade_stamps(loaded, logs_dir)
        session_lines = load_session_lines(logs_dir, news)
        excluded = [t for t in loaded if _gap_excluded(t)]
        kept = [t for t in loaded if not _gap_excluded(t)]
        trades = [t for t in kept if t.get("version") == MEASURED_VERSION]
        if PRIOR_MEASURED_VERSION == MEASURED_VERSION:
            prior = []
        else:
            prior = [t for t in kept if t.get("version") == PRIOR_MEASURED_VERSION]
        oct2 = [t for t in kept if t.get("version") == OCT2_VERSION]
        other = [
            t for t in kept
            if t.get("version") not in (None, "", MEASURED_VERSION, OCT2_VERSION)
        ]
        unstamped = [t for t in kept if not t.get("version")]
        marks = load_marks(news) if news is not None else []
        markers = load_open_markers(news) if news is not None else []
        ledger = load_ledger(news) if news is not None else None
        sessions = load_sessions(hedge)
        joined_ids = {str(t.get("trade_id") or "") for t in loaded if t.get("trade_id")}
        joined_ids.update(measurement_exclusions.EXCLUDED_TRADE_IDS)
        orphan_ids = sorted({
            str(m.get("trade_id") or "")
            for m in marks
            if str(m.get("trade_id") or "") not in joined_ids
        })
        orphan_marks = sum(
            1 for m in marks if str(m.get("trade_id") or "") not in joined_ids
        )
        r_vals = [t["r_fill"] for t in trades if t.get("r_fill") is not None]
        expectancy = sum(r_vals) / len(r_vals) if r_vals else None
        pf = profit_factor(trades)
        gross_wins = sum(t["pnl_fill"] for t in trades if (t.get("pnl_fill") or 0) > 0)
        gross_losses = sum(-(t["pnl_fill"] or 0) for t in trades if (t.get("pnl_fill") or 0) < 0)
        fill_sum = sum(t["pnl_fill"] or 0.0 for t in trades)
        mid_sum = sum(t["pnl_mid"] or 0.0 for t in trades)
        realized_mid_ledger = _f((ledger or {}).get("total_realized_pnl")) or 0.0
        bp = _f((ledger or {}).get("buying_power"))
        seed = None
        if bp is not None:
            seed = bp - realized_mid_ledger + float(open_debit)
        dd_dollars, dd_pct = (None, None)
        dd_daily_dollars, dd_daily_pct = (None, None)
        if seed is not None and seed > 0:
            dd_dollars, dd_pct = max_drawdown(
                [t["pnl_fill"] or 0.0 for t in trades], seed
            )
            dd_daily_dollars, dd_daily_pct = max_drawdown(daily_pnls(trades), seed)
        spreads = [t["entry_spread_pct"] for t in trades if t.get("entry_spread_pct") is not None]
        stops = [t["stop_slippage_pct"] for t in trades if t.get("stop_slippage_pct") is not None]
        by_reason: dict[str, dict[str, float]] = {}
        for trade in trades:
            bucket = by_reason.setdefault(
                trade["reason"], {"n": 0, "pnl_fill": 0.0, "r_sum": 0.0, "r_n": 0}
            )
            bucket["n"] += 1
            bucket["pnl_fill"] += trade["pnl_fill"] or 0.0
            if trade.get("r_fill") is not None:
                bucket["r_sum"] += trade["r_fill"]
                bucket["r_n"] += 1
        mfe = mfe_capture(trades, marks)
        return {
            "trades": trades,
            "markers": markers,
            "ledger": ledger,
            "sessions": sessions,
            "n_closed": len(trades),
            "n_excluded": len(excluded),
            "n_with_r": len(r_vals),
            "n_missing_risk": len(trades) - len(r_vals),
            "expectancy_r": expectancy,
            "profit_factor": pf,
            "gross_wins": gross_wins,
            "gross_losses": gross_losses,
            "no_losses": gross_losses <= 1e-9,
            "max_dd_dollars": dd_dollars,
            "max_dd_pct": dd_pct,
            "max_dd_daily_dollars": dd_daily_dollars,
            "max_dd_daily_pct": dd_daily_pct,
            "seed": seed,
            "mfe": mfe,
            "expectancy_ci": day_block_expectancy(trades),
            "regimes": regime_table(session_lines, trades, MEASURED_VERSION),
            "hold": hold_split(trades),
            "buckets": _buckets_for(trades, session_lines, marks, seed, MEASURED_VERSION),
            "prior_version": PRIOR_MEASURED_VERSION,
            "n_prior": len(prior),
            "prior_buckets": _buckets_for(
                prior, session_lines, marks, seed, PRIOR_MEASURED_VERSION
            ),
            "carry_shadow": load_carry_shadow(news),
            "n_oct2": len(oct2),
            "oct2": _sample_side(oct2, session_lines),
            "n_unstamped": len(unstamped),
            "n_other": len(other),
            "other_versions": sorted({str(t.get("version")) for t in other}),
            "stamps": stamps,
            "entry_spread_pct": (sum(spreads) / len(spreads)) if spreads else None,
            "n_entry_spread": len(spreads),
            "stop_slippage_pct": (sum(stops) / len(stops)) if stops else None,
            "n_stop_slippage": len(stops),
            "fill_est": sum(1 for t in trades if t.get("fill_est")),
            "realized_fill": fill_sum,
            "realized_mid": mid_sum,
            "spread_cost": mid_sum - fill_sum,
            "by_reason": by_reason,
            "mark_rows": len(marks),
            "orphan_marks": orphan_marks,
            "orphan_ids": orphan_ids,
            "news_tables": _inventory(news),
            "hedge_tables": _inventory(hedge),
        }
    finally:
        if news is not None:
            news.close()
        if hedge is not None:
            hedge.close()


def _inventory(conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    if conn is None:
        return []
    out = []
    for name in table_names(conn):
        out.append({
            "name": name,
            "rows": table_count(conn, name),
            "columns": table_columns(conn, name),
        })
    return out


def today_activity(summary: dict[str, Any], day) -> dict[str, Any]:
    trades = [t for t in summary["trades"] if t.get("session_date") == day]
    markers = [m for m in summary["markers"] if m.get("session_date") == day]
    fill = sum(t["pnl_fill"] or 0.0 for t in trades)
    mid = sum(t["pnl_mid"] or 0.0 for t in trades)
    return {
        "entries": len(markers),
        "closes": len(trades),
        "realized_fill": fill,
        "spread_cost": mid - fill,
        "closed": trades,
    }
