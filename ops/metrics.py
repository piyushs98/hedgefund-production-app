"""Go/no-go math from the persistent tables. No network, no writes.

Fill P&L is (exit bid − entry ask) × 100 × qty, the same series the bot
stores on trade_history.pnl_fill. Planned risk stays on the mid premium,
because that is what the stop is measured from.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ops.store import connect_ro, table_columns, table_count, table_names

CHICAGO = ZoneInfo("America/Chicago")
MULTIPLIER = 100

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
    """Mean of fill P&L / peak mid excursion, for trades whose peak is > 0.

    Peak uses position_marks.pnl_pct (percent of entry) and the close mid
    print, matching the MFE on the TRADE line. Marks are mid. Fill P&L is
    the ask/bid series. Trades with no positive peak are left out of the mean.
    """
    by_id: dict[str, list[float]] = defaultdict(list)
    for mark in marks:
        tid = str(mark.get("trade_id") or "")
        pct = _f(mark.get("pnl_pct"))
        if tid and pct is not None:
            by_id[tid].append(pct)
    captures = []
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
        captures.append(pnl_fill / mfe_dollars)
    mean = sum(captures) / len(captures) if captures else None
    return {
        "mean": mean,
        "n": len(captures),
        "missing": missing,
        "nonpositive": nonpositive,
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


def summarize(news_path, hedge_path, open_debit: float = 0.0) -> dict[str, Any]:
    news = connect_ro(news_path)
    hedge = connect_ro(hedge_path)
    try:
        trades = load_closed_trades(news) if news is not None else []
        marks = load_marks(news) if news is not None else []
        markers = load_open_markers(news) if news is not None else []
        ledger = load_ledger(news) if news is not None else None
        sessions = load_sessions(hedge)
        joined_ids = {str(t.get("trade_id") or "") for t in trades if t.get("trade_id")}
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
        if seed is not None and seed > 0:
            dd_dollars, dd_pct = max_drawdown(
                [t["pnl_fill"] or 0.0 for t in trades], seed
            )
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
            "n_with_r": len(r_vals),
            "n_missing_risk": len(trades) - len(r_vals),
            "expectancy_r": expectancy,
            "profit_factor": pf,
            "gross_wins": gross_wins,
            "gross_losses": gross_losses,
            "no_losses": gross_losses <= 1e-9,
            "max_dd_dollars": dd_dollars,
            "max_dd_pct": dd_pct,
            "seed": seed,
            "mfe": mfe,
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
