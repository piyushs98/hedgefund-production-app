"""Executor records. This file is the only database the executor writes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from webull_exec.ids import make_id

ROOT = Path(__file__).resolve().parents[1]
OPEN_STATES = (
    "working",
    "filled_unprotected",
    "protected",
    "exiting",
)


def default_path() -> Path:
    import os
    return Path(os.environ.get("WEBULL_EXEC_DB") or (ROOT / "data" / "webull_exec.db"))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def close(self) -> None:
        self.conn.close()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS control (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intents (
                trade_id TEXT PRIMARY KEY,
                ticker TEXT,
                option_type TEXT,
                strike REAL,
                expiration TEXT,
                entry_ask REAL,
                stop_price REAL,
                entry_score REAL,
                state TEXT NOT NULL,
                entry_client_order_id TEXT,
                stop_client_order_id TEXT,
                exit_client_order_id TEXT,
                combo INTEGER NOT NULL DEFAULT 0,
                fill_price TEXT,
                paper_exit_bid REAL,
                exit_limit REAL,
                submitted_at TEXT,
                filled_at TEXT,
                exit_started_at TEXT,
                exit_order_at TEXT,
                repriced INTEGER NOT NULL DEFAULT 0,
                miss_reason TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS live_orders (
                client_order_id TEXT PRIMARY KEY,
                trade_id TEXT,
                intent TEXT,
                request_json TEXT,
                response_json TEXT,
                status TEXT,
                fill_price TEXT,
                submitted_at TEXT,
                ack_at TEXT,
                filled_at TEXT,
                cancelled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS slippage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT,
                paper_trade_id TEXT,
                side TEXT,
                paper_fill REAL,
                webull_fill REAL,
                slip_dollars REAL,
                recorded_at TEXT
            );
            CREATE TABLE IF NOT EXISTS realized (
                trade_id TEXT PRIMARY KEY,
                chicago_date TEXT,
                pnl_dollars REAL,
                r_multiple REAL,
                recorded_at TEXT
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT,
                message TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS quote_compares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT,
                side TEXT,
                yahoo_bid REAL,
                yahoo_ask REAL,
                webull_bid REAL,
                webull_ask REAL,
                quote_time TEXT,
                symbol TEXT,
                recorded_at TEXT
            );
            """
        )
        self.conn.commit()

    def get_control(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_control(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO control(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def disabled(self) -> bool:
        return self.get_control("disabled") == "1" or self.get_control("killed") == "1"

    def killed(self) -> bool:
        return self.get_control("killed") == "1"

    def next_id(self, day: str, kind: str) -> str:
        seq = int(self.get_control("order_seq") or "0") + 1
        self.set_control("order_seq", str(seq))
        return make_id(day, seq, kind)

    def remember_order(
        self,
        client_order_id: str,
        trade_id: str,
        intent: str,
        request: dict | list,
        response: dict,
        status: str,
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO live_orders (
                client_order_id, trade_id, intent, request_json, response_json,
                status, submitted_at, ack_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                response_json = excluded.response_json,
                status = excluded.status,
                ack_at = excluded.ack_at
            """,
            (
                client_order_id,
                trade_id,
                intent,
                json.dumps(request, default=str),
                json.dumps(response, default=str),
                status,
                now,
                now,
            ),
        )
        self.conn.commit()

    def mark_order(self, client_order_id: str, status: str, *, fill_price: str | None = None, cancelled: bool = False, filled: bool = False) -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE live_orders
            SET status = ?,
                fill_price = COALESCE(?, fill_price),
                cancelled_at = CASE WHEN ? THEN ? ELSE cancelled_at END,
                filled_at = CASE WHEN ? THEN ? ELSE filled_at END
            WHERE client_order_id = ?
            """,
            (status, fill_price, 1 if cancelled else 0, now, 1 if filled else 0, now, client_order_id),
        )
        self.conn.commit()

    def upsert_intent(self, row: dict) -> None:
        row = dict(row)
        row["updated_at"] = _now()
        cols = [
            "trade_id", "ticker", "option_type", "strike", "expiration",
            "entry_ask", "stop_price", "entry_score", "state",
            "entry_client_order_id", "stop_client_order_id", "exit_client_order_id",
            "combo", "fill_price", "paper_exit_bid", "exit_limit",
            "submitted_at", "filled_at", "exit_started_at", "exit_order_at",
            "repriced", "miss_reason", "updated_at",
        ]
        for col in cols:
            row.setdefault(col, None)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{col} = excluded.{col}" for col in cols if col != "trade_id")
        self.conn.execute(
            f"INSERT INTO intents ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(trade_id) DO UPDATE SET {updates}",
            [row[col] for col in cols],
        )
        self.conn.commit()

    def intent(self, trade_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM intents WHERE trade_id = ?", (trade_id,)).fetchone()
        return None if row is None else dict(row)

    def open_intents(self) -> list[dict]:
        marks = ",".join("?" for _ in OPEN_STATES)
        rows = self.conn.execute(
            f"SELECT * FROM intents WHERE state IN ({marks})",
            OPEN_STATES,
        ).fetchall()
        return [dict(row) for row in rows]

    def known_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT trade_id FROM intents").fetchall()
        return {str(row["trade_id"]) for row in rows}

    def add_quote(
        self,
        trade_id: str,
        side: str,
        yahoo_bid,
        yahoo_ask,
        webull_bid,
        webull_ask,
        quote_time,
        symbol,
    ) -> None:
        """Report-only. The row is not an order price."""
        self.conn.execute(
            """
            INSERT INTO quote_compares (
                trade_id, side, yahoo_bid, yahoo_ask, webull_bid, webull_ask,
                quote_time, symbol, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                side,
                yahoo_bid,
                yahoo_ask,
                webull_bid,
                webull_ask,
                None if quote_time is None else str(quote_time),
                symbol,
                _now(),
            ),
        )
        self.conn.commit()

    def add_slippage(self, client_order_id: str, trade_id: str, side: str, paper_fill: float, webull_fill: float) -> None:
        if side == "buy":
            slip = (webull_fill - paper_fill) * 100.0
        else:
            slip = (paper_fill - webull_fill) * 100.0
        self.conn.execute(
            """
            INSERT INTO slippage (
                client_order_id, paper_trade_id, side, paper_fill, webull_fill,
                slip_dollars, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (client_order_id, trade_id, side, paper_fill, webull_fill, slip, _now()),
        )
        self.conn.commit()

    def add_realized(self, trade_id: str, chicago_date: str, pnl: float, r_multiple: float | None) -> None:
        self.conn.execute(
            """
            INSERT INTO realized (trade_id, chicago_date, pnl_dollars, r_multiple, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO NOTHING
            """,
            (trade_id, chicago_date, pnl, r_multiple, _now()),
        )
        self.conn.commit()

    def daily_r(self, chicago_date: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(r_multiple), 0) AS r FROM realized WHERE chicago_date = ?",
            (chicago_date,),
        ).fetchone()
        return float(row["r"] or 0)

    def cumulative_pnl(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(pnl_dollars), 0) AS pnl FROM realized").fetchone()
        return float(row["pnl"] or 0)

    def alert_once(self, key: str, kind: str, message: str) -> bool:
        """Record and return True the first time ``key`` is raised."""
        if self.get_control("alert:" + key) == "1":
            return False
        self.conn.execute(
            "INSERT INTO alerts (kind, message, created_at) VALUES (?, ?, ?)",
            (kind, message, _now()),
        )
        self.conn.commit()
        self.set_control("alert:" + key, "1")
        return True

    def saw_sweep(self, day: str) -> bool:
        return self.get_control("sweep:" + day) == "1"

    def mark_sweep(self, day: str) -> None:
        self.set_control("sweep:" + day, "1")

    def saw_flatten(self, day: str) -> bool:
        return self.get_control("flatten:" + day) == "1"

    def mark_flatten(self, day: str) -> None:
        self.set_control("flatten:" + day, "1")
