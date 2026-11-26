"""Read-only view of the paper book. This module never writes it."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class PaperOpen:
    trade_id: str
    ticker: str
    option_type: str
    strike: float
    expiration: str
    entry_ask: float | None
    stop_loss: float | None
    entry_score: float | None
    quantity: int
    entry_mid: float | None


@dataclass
class PaperClose:
    trade_id: str
    exit_bid: float | None
    reason: str


def _f(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _option_type(direction: str | None) -> str | None:
    text = str(direction or "").upper()
    if "PUT" in text:
        return "PUT"
    if "CALL" in text:
        return "CALL"
    return None


def opens(path: Path | None = None) -> list[PaperOpen]:
    store = path or Path(os_env("ACTIVE_TRADES_PATH", str(ROOT / "active_trades.json")))
    if not store.exists():
        return []
    try:
        raw = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows: list = []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        if isinstance(raw.get("trades"), list):
            rows = raw["trades"]
        elif raw.get("ticker"):
            rows = [raw]
    out: list[PaperOpen] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("trade_id"):
            continue
        option_type = _option_type(row.get("direction"))
        strike = _f(row.get("strike"))
        expiration = str(row.get("expiration") or "")[:10]
        if option_type is None or strike is None or len(expiration) != 10:
            continue
        qty = row.get("quantity") or 1
        try:
            qty_i = int(qty)
        except (TypeError, ValueError):
            qty_i = 1
        entry_ask = _f(row.get("entry_ask"))
        if entry_ask is None:
            entry_ask = _f(row.get("ask"))
        out.append(
            PaperOpen(
                trade_id=str(row["trade_id"]),
                ticker=str(row.get("ticker") or "").upper(),
                option_type=option_type,
                strike=strike,
                expiration=expiration,
                entry_ask=entry_ask,
                stop_loss=_f(row.get("stop_loss")),
                entry_score=_f(row.get("entry_score")),
                quantity=qty_i,
                entry_mid=_f(row.get("entry_mid")),
            )
        )
    return out


def close_for(trade_id: str, db_path: Path | None = None) -> PaperClose | None:
    path = db_path or Path(os_env("NEWS_DB_PATH", str(ROOT / "data" / "news_room.db")))
    if not path.exists():
        return None
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_history)")}
        if "notes" not in cols or "contract_json" not in cols:
            return None
        bid_sql = "exit_bid" if "exit_bid" in cols else "NULL AS exit_bid"
        rows = conn.execute(
            f"""
            SELECT notes, contract_json, {bid_sql}
            FROM trade_history
            WHERE notes LIKE 'EXIT:%'
            ORDER BY id DESC
            LIMIT 80
            """
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    for row in rows:
        try:
            payload = json.loads(row["contract_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or str(payload.get("trade_id") or "") != trade_id:
            continue
        bid = _f(row["exit_bid"])
        if bid is None:
            bid = _f(payload.get("exit_bid"))
        if bid is None:
            bid = _f(payload.get("last_bid"))
        reason = str(row["notes"] or "")
        if reason.startswith("EXIT:"):
            reason = reason[5:]
        return PaperClose(trade_id=trade_id, exit_bid=bid, reason=reason)
    return None


def os_env(name: str, default: str) -> str:
    import os
    return os.environ.get(name) or default
