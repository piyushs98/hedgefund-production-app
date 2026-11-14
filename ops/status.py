"""Parse the bot log and the open-book files. Read-only."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ops.metrics import CHICAGO, MULTIPLIER, _f, planned_risk, quantity_of
from ops.store import connect_ro, newest_bot_log, read_text, table_names

SCAN_RE = re.compile(
    r"(?:30-MIN SCAN|PORTFOLIO SCAN)\s+"
    r"((?:scan|exit)-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-[0-9a-f]+)"
    r"(?:\s+\|\s+(\d{2}):(\d{2})\s+(?:CDT|CT))?"
)
EXIT_RE = re.compile(
    r"(?:EXIT-ONLY PASS|EXIT-ONLY)\s+"
    r"(exit-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-[0-9a-f]+)"
    r"(?:\s+\|\s+(\d{2}):(\d{2})\s+(?:CDT|CT))?"
)
EXIT_IDLE_RE = re.compile(r"\[exit-pass\]\s+(\d{2}):(\d{2})\s+(?:CDT|CT)")
VERSION_RE = re.compile(r"\bv([0-9a-f]{7})\b")
GATE_RE = re.compile(r"^.*\bGATE \[.*$", re.MULTILINE)
BANNER_RE = re.compile(r"----- (\d{4}-\d{2}-\d{2}) ")
CRUMB_RE = re.compile(
    r"Crumb fetch rate-limited|crumb fetch suppressed|crumb backoff",
    re.IGNORECASE,
)
STALE_AFTER = timedelta(minutes=35)
SCAN_OPEN = time(8, 45)
SCAN_CLOSE = time(15, 0)


def _chicago_dt(year, month, day, hour, minute, second=0) -> datetime:
    return datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second),
        tzinfo=CHICAGO,
    )


def _from_id(match: re.Match, clock_h: int | None, clock_m: int | None) -> datetime:
    year, month, day = match.group(2), match.group(3), match.group(4)
    hour, minute, second = match.group(5), match.group(6), match.group(7)
    if clock_h is not None:
        hour, minute, second = clock_h, clock_m, 0
    return _chicago_dt(year, month, day, hour, minute, second)


def parse_log(text: str, *, file_date: str | None, now: datetime) -> dict[str, Any]:
    today = now.astimezone(CHICAGO).date().isoformat()
    version = None
    for hit in VERSION_RE.finditer(text):
        version = "v" + hit.group(1)

    last_scan = None
    for match in SCAN_RE.finditer(text):
        if not match.group(1).startswith("scan-"):
            continue
        clock_h = int(match.group(8)) if match.group(8) else None
        clock_m = int(match.group(9)) if match.group(9) else None
        last_scan = _from_id(match, clock_h, clock_m)

    last_exit = None
    for match in EXIT_RE.finditer(text):
        clock_h = int(match.group(8)) if match.group(8) else None
        clock_m = int(match.group(9)) if match.group(9) else None
        last_exit = _from_id(match, clock_h, clock_m)
    # Idle exit lines carry a clock and no date. Attach the file date or today.
    idle_day = file_date or today
    for match in EXIT_IDLE_RE.finditer(text):
        try:
            y, m, d = (int(p) for p in idle_day.split("-"))
        except ValueError:
            continue
        stamp = _chicago_dt(y, m, d, match.group(1), match.group(2))
        if last_exit is None or stamp > last_exit:
            last_exit = stamp

    gates = GATE_RE.findall(text)
    gate = gates[-1].strip() if gates else None

    crumb_lines = [ln.strip() for ln in text.splitlines() if CRUMB_RE.search(ln)]
    if not crumb_lines:
        crumb = "clear — no crumb 429 in this log"
        crumb_hot = False
    else:
        crumb = crumb_lines[-1]
        crumb_hot = True

    criticals = _criticals_today(text, file_date=file_date, today=today)
    holiday = "Market closed today for NYSE holiday." in text
    market_open = _market_open(now) and not holiday
    stale = False
    stale_text = ""
    if market_open:
        if last_scan is None:
            stale = True
            stale_text = (
                "No full scan in the current log. The full-scan window is open "
                "(weekdays 08:45–15:00 Chicago)."
            )
        else:
            age = now.astimezone(CHICAGO) - last_scan
            if age > STALE_AFTER:
                minutes = int(age.total_seconds() // 60)
                stale = True
                stale_text = (
                    f"Last full scan was {minutes} minutes ago "
                    f"({last_scan.strftime('%Y-%m-%d %H:%M')} Chicago). "
                    "A scan older than 35 minutes during market hours means "
                    "the bot may be stuck."
                )
    return {
        "version": version or "not in the log",
        "last_scan": last_scan,
        "last_exit": last_exit,
        "gate": gate,
        "crumb": crumb,
        "crumb_hot": crumb_hot,
        "criticals": criticals,
        "stale": stale,
        "stale_text": stale_text,
        "market_open": market_open,
        "holiday": holiday,
    }


def _market_open(now: datetime) -> bool:
    local = now.astimezone(CHICAGO)
    if local.weekday() >= 5:
        return False
    return SCAN_OPEN <= local.time() < SCAN_CLOSE


def _criticals_today(text: str, *, file_date: str | None, today: str) -> list[str]:
    """CRITICAL lines that belong to today's Chicago process or today's scans."""
    lines = text.splitlines()
    banner_today = False
    scan_today = False
    out = []
    if file_date == today:
        banner_today = True
    for line in lines:
        banner = BANNER_RE.search(line)
        if banner:
            banner_today = banner.group(1) == today
            scan_today = False
        if f"scan-{today.replace('-', '')}-" in line or f"exit-{today.replace('-', '')}-" in line:
            scan_today = True
        if "CRITICAL" not in line:
            continue
        if banner_today or scan_today or file_date == today:
            out.append(line.strip())
    return out


def load_active_trades(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, dict) and t.get("ticker")]
    if isinstance(raw, dict):
        if isinstance(raw.get("trades"), list):
            return [t for t in raw["trades"] if isinstance(t, dict) and t.get("ticker")]
        if raw.get("ticker"):
            return [raw]
    return []


def open_debit(trades: list[dict[str, Any]]) -> float:
    total = 0.0
    for trade in trades:
        entry = _f(trade.get("entry_price"))
        if entry is None:
            entry = _f(trade.get("entry_premium"))
        if entry is None:
            continue
        total += entry * MULTIPLIER * quantity_of(trade)
    return total


def latest_marks(news_db: Path, trade_ids: list[str]) -> dict[str, dict[str, Any]]:
    conn = connect_ro(news_db)
    if conn is None:
        return {}
    try:
        if "position_marks" not in table_names(conn):
            return {}
        found = {}
        for trade_id in trade_ids:
            if not trade_id:
                continue
            row = conn.execute(
                "SELECT ts, mark, bid, ask, pnl_pct, stop_loss, take_profit "
                "FROM position_marks WHERE trade_id = ? ORDER BY ts DESC LIMIT 1",
                (trade_id,),
            ).fetchone()
            if row is not None:
                found[trade_id] = {k: row[k] for k in row.keys()}
        return found
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def position_rows(
    trades: list[dict[str, Any]],
    marks: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    rows = []
    for trade in trades:
        trade_id = str(trade.get("trade_id") or "")
        mark = marks.get(trade_id) or {}
        entry = _f(trade.get("entry_premium"))
        if entry is None:
            entry = _f(trade.get("entry_price"))
        entry_mid = _f(trade.get("entry_mid")) or entry
        sl = _f(trade.get("stop_loss"))
        if sl is None:
            sl = _f(mark.get("stop_loss"))
        tp = _f(trade.get("take_profit"))
        if tp is None:
            tp = _f(trade.get("target_price"))
        if tp is None:
            tp = _f(mark.get("take_profit"))
        qty = quantity_of(trade)
        px = _f(mark.get("mark"))
        pnl = None
        r_mult = None
        if px is not None and entry is not None:
            pnl = (px - entry) * MULTIPLIER * qty
        risk = planned_risk(entry_mid, sl, qty)
        if pnl is not None and risk is not None:
            r_mult = pnl / risk
        dist_sl = (px - sl) if px is not None and sl is not None else None
        dist_tp = (tp - px) if px is not None and tp is not None else None
        held = None
        started = None
        raw_ts = trade.get("entry_timestamp") or trade.get("entry_time")
        if raw_ts:
            text = str(raw_ts).strip().replace("Z", "+00:00")
            try:
                started = datetime.fromisoformat(text)
            except ValueError:
                started = None
            if started is not None:
                if started.tzinfo is None:
                    started = started.replace(tzinfo=CHICAGO)
                held = max(
                    0,
                    int(round((now - started.astimezone(CHICAGO)).total_seconds() / 60.0)),
                )
        direction = str(trade.get("direction") or "").upper() or "—"
        strike = trade.get("strike")
        exp = str(trade.get("expiration") or "")[:10]
        strike_s = "—"
        if strike is not None:
            try:
                n = float(strike)
                strike_s = str(int(n)) if abs(n - round(n)) < 1e-9 else f"{n:g}"
            except (TypeError, ValueError):
                strike_s = str(strike)
        rows.append({
            "ticker": str(trade.get("ticker") or "—"),
            "contract": f"{direction} {strike_s} {exp}".strip(),
            "qty": qty,
            "entry": entry,
            "mark": px,
            "pnl": pnl,
            "r": r_mult,
            "dist_sl": dist_sl,
            "dist_tp": dist_tp,
            "held": held,
        })
    return rows


def breaker_status(hedge_db: Path) -> str:
    conn = connect_ro(hedge_db)
    if conn is None:
        return "hedge_fund.db missing"
    try:
        if "circuit_breaker_state" not in table_names(conn):
            return "no circuit_breaker_state table"
        row = conn.execute(
            "SELECT consecutive_failures, state, opened_at_epoch, last_failure_source, "
            "updated_at FROM circuit_breaker_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return "no breaker row"
        state = row["state"] or "CLOSED"
        fails = row["consecutive_failures"] or 0
        source = row["last_failure_source"] or "—"
        opened = row["opened_at_epoch"]
        opened_s = "—"
        if opened:
            try:
                opened_s = datetime.fromtimestamp(float(opened), CHICAGO).strftime(
                    "%Y-%m-%d %H:%M Chicago"
                )
            except (TypeError, ValueError, OSError):
                opened_s = str(opened)
        return (
            f"{state}  failures={fails}  last={source}  "
            f"opened={opened_s}  updated={row['updated_at'] or '—'}"
        )
    except sqlite3.Error as exc:
        return f"unreadable ({exc})"
    finally:
        conn.close()


def file_date_of(path: Path | None) -> str | None:
    if path is None:
        return None
    match = re.search(r"bot-(\d{4}-\d{2}-\d{2})\.log$", path.name)
    return match.group(1) if match else None


def read_bot_log(logs_dir: Path) -> tuple[Path | None, str]:
    path = newest_bot_log(logs_dir)
    if path is None:
        return None, ""
    return path, read_text(path)
