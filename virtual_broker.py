"""
virtual_broker.py — Virtual SQLite paper ledger for the hedge fund dashboard.

Replaces an external paper broker: buying power and realized PnL live in
`news_room.db` (config.NEWS_DB_PATH). Options use the standard 100x multiplier.

Does not touch scoring, AI prompts, or DeepSeek logic.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

DB_PATH = config.NEWS_DB_PATH
CONTRACT_MULTIPLIER = 100
STARTING_BUYING_POWER = 100_000.0


def _connect() -> sqlite3.Connection:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_ledger() -> None:
    """
    Create portfolio_ledger + trade_history if missing.
    Seed portfolio_ledger with $100,000 buying_power / $0 PnL when empty.
    """
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_ledger (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                buying_power REAL NOT NULL,
                total_realized_pnl REAL NOT NULL,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                closed_at TEXT NOT NULL,
                ticker TEXT,
                direction TEXT,
                strike REAL,
                expiration TEXT,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                pnl REAL NOT NULL,
                contract_json TEXT,
                notes TEXT
            )
            """
        )
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM portfolio_ledger"
        ).fetchone()
        if row is None or int(row["n"]) == 0:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO portfolio_ledger
                    (id, buying_power, total_realized_pnl, updated_at)
                VALUES (1, ?, 0.0, ?)
                """,
                (STARTING_BUYING_POWER, now),
            )
        conn.commit()


def get_portfolio() -> dict[str, float]:
    """Return current buying_power and total_realized_pnl."""
    ensure_ledger()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT buying_power, total_realized_pnl
            FROM portfolio_ledger WHERE id = 1
            """
        ).fetchone()
    if not row:
        return {
            "buying_power": STARTING_BUYING_POWER,
            "total_realized_pnl": 0.0,
        }
    return {
        "buying_power": float(row["buying_power"]),
        "total_realized_pnl": float(row["total_realized_pnl"]),
    }


def _contract_meta(contract: Any) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    return contract


def paper_buy(contract: Any, entry_price: float | int | None) -> dict[str, Any]:
    """
    Open a virtual long option: debit entry_price * 100 from buying_power.

    Returns a result dict with ok/error and the updated ledger snapshot.
    """
    ensure_ledger()
    try:
        premium = float(entry_price)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"invalid entry_price: {entry_price!r}"}

    if premium <= 0:
        return {"ok": False, "error": f"entry_price must be > 0, got {premium}"}

    cost = premium * CONTRACT_MULTIPLIER
    now = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        row = conn.execute(
            "SELECT buying_power, total_realized_pnl FROM portfolio_ledger WHERE id = 1"
        ).fetchone()
        if not row:
            return {"ok": False, "error": "portfolio_ledger missing after ensure"}

        buying_power = float(row["buying_power"])
        if buying_power < cost:
            print(
                f"[VirtualBroker] paper_buy blocked: need ${cost:.2f}, "
                f"have ${buying_power:.2f}"
            )
            return {
                "ok": False,
                "error": "insufficient buying_power",
                "buying_power": buying_power,
                "cost": cost,
            }

        new_bp = buying_power - cost
        conn.execute(
            """
            UPDATE portfolio_ledger
            SET buying_power = ?, updated_at = ?
            WHERE id = 1
            """,
            (new_bp, now),
        )
        conn.commit()
        realized = float(row["total_realized_pnl"])

    meta = _contract_meta(contract)
    ticker = meta.get("ticker") or meta.get("symbol")
    print(
        f"[VirtualBroker] paper_buy "
        f"{ticker or meta.get('direction', '?')} "
        f"@ ${premium:.2f} → debit ${cost:.2f}; "
        f"buying_power ${buying_power:.2f} → ${new_bp:.2f}"
    )
    return {
        "ok": True,
        "cost": cost,
        "entry_price": premium,
        "buying_power": new_bp,
        "total_realized_pnl": realized,
    }


def paper_sell(
    contract: Any,
    exit_price: float | int | None,
    direction: str | None,
    entry_price: float | int | None,
) -> dict[str, Any]:
    """
    Close a virtual long option:
      * credit exit_price * 100 back to buying_power
      * realized PnL = (exit_price - entry_price) * 100
      * append a row to trade_history
    """
    ensure_ledger()
    try:
        entry = float(entry_price)
        exit_ = float(exit_price)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": f"invalid prices entry={entry_price!r} exit={exit_price!r}",
        }

    pnl = (exit_ - entry) * CONTRACT_MULTIPLIER
    capital_back = exit_ * CONTRACT_MULTIPLIER
    now = datetime.now(timezone.utc).isoformat()
    meta = _contract_meta(contract)

    # Prefer explicit direction arg; fall back to contract fields
    dir_str = direction or meta.get("direction") or ""
    ticker = (
        meta.get("ticker")
        or meta.get("symbol")
        or (meta.get("option_contract") or {}).get("ticker")
    )
    strike = meta.get("strike")
    expiration = meta.get("expiration")
    if isinstance(meta.get("option_contract"), dict):
        oc = meta["option_contract"]
        strike = strike if strike is not None else oc.get("strike")
        expiration = expiration or oc.get("expiration")
        dir_str = dir_str or oc.get("direction") or ""

    with _connect() as conn:
        row = conn.execute(
            "SELECT buying_power, total_realized_pnl FROM portfolio_ledger WHERE id = 1"
        ).fetchone()
        if not row:
            return {"ok": False, "error": "portfolio_ledger missing after ensure"}

        new_bp = float(row["buying_power"]) + capital_back
        new_pnl = float(row["total_realized_pnl"]) + pnl
        conn.execute(
            """
            UPDATE portfolio_ledger
            SET buying_power = ?, total_realized_pnl = ?, updated_at = ?
            WHERE id = 1
            """,
            (new_bp, new_pnl, now),
        )
        conn.execute(
            """
            INSERT INTO trade_history
                (closed_at, ticker, direction, strike, expiration,
                 entry_price, exit_price, pnl, contract_json, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                ticker,
                str(dir_str) if dir_str else None,
                float(strike) if strike is not None else None,
                str(expiration) if expiration else None,
                entry,
                exit_,
                pnl,
                json.dumps(meta, default=str) if meta else None,
                None,
            ),
        )
        conn.commit()

    print(
        f"[VirtualBroker] paper_sell {ticker or '?'} {dir_str} "
        f"entry=${entry:.2f} exit=${exit_:.2f} PnL=${pnl:.2f}; "
        f"buying_power → ${new_bp:.2f}, realized → ${new_pnl:.2f}"
    )
    return {
        "ok": True,
        "pnl": pnl,
        "capital_back": capital_back,
        "entry_price": entry,
        "exit_price": exit_,
        "buying_power": new_bp,
        "total_realized_pnl": new_pnl,
    }


# Seed tables on import so first API hit never races an empty DB.
try:
    ensure_ledger()
except Exception as _init_err:
    print(f"[VirtualBroker] WARNING: ensure_ledger on import failed: {_init_err}")
