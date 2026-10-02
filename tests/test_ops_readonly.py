"""Read-only dashboard and measurement report. No trading imports."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ops.dashboard import HOST, PORT, build_snapshot, render_page
from ops.report import render_report
from ops.store import Paths, connect_ro

CHI = ZoneInfo("America/Chicago")
SECRET = "discord-webhook-should-not-appear"


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE trade_history (
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
            notes TEXT,
            slippage REAL,
            pnl_mid REAL,
            pnl_fill REAL,
            entry_mid REAL,
            entry_ask REAL,
            exit_mid REAL,
            exit_bid REAL,
            fill_est INTEGER
        );
        CREATE TABLE portfolio_ledger (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            buying_power REAL NOT NULL,
            total_realized_pnl REAL NOT NULL,
            updated_at TEXT,
            total_realized_pnl_fill REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE position_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            scan_id TEXT,
            trade_id TEXT,
            ticker TEXT,
            bid REAL,
            ask REAL,
            mark REAL,
            spot REAL,
            pnl_pct REAL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL
        );
        """
    )


def _hedge(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE backtest_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            scan_id TEXT,
            ticker TEXT,
            reasons_json TEXT,
            raw_metrics_json TEXT,
            action_flag TEXT
        );
        CREATE TABLE circuit_breaker_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            consecutive_failures INTEGER,
            state TEXT,
            opened_at_epoch REAL,
            last_failure_source TEXT,
            updated_at TEXT
        );
        """
    )


class OpsReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.news = root / "news.db"
        self.hedge = root / "hedge.db"
        self.trades = root / "active_trades.json"
        self.logs = root / "logs"
        self.logs.mkdir()
        self.env = root / ".env"
        self.env.write_text(
            f"DISCORD_WEBHOOK={SECRET}\nRESET_LEDGER_ON_BOOT=false\n",
            encoding="utf-8",
        )
        self.paths = Paths(
            root=root,
            news_db=self.news,
            hedge_db=self.hedge,
            active_trades=self.trades,
            logs_dir=self.logs,
            env_file=self.env,
        )
        with sqlite3.connect(self.news) as conn:
            _schema(conn)
            conn.execute(
                "INSERT INTO portfolio_ledger VALUES (1, 9926, 226, ?, 160)",
                ("2026-10-01T20:00:00+00:00",),
            )
            win = {
                "trade_id": "t-win",
                "quantity": 2,
                "stop_loss": 2.10,
                "ticker": "GOOGL",
            }
            lose = {
                "trade_id": "t-lose",
                "quantity": 1,
                "stop_loss": 1.60,
                "ticker": "IWM",
            }
            conn.execute(
                """INSERT INTO trade_history
                   (closed_at, ticker, direction, strike, expiration,
                    entry_price, exit_price, pnl, contract_json, notes,
                    pnl_mid, pnl_fill, entry_mid, entry_ask, exit_mid, exit_bid,
                    fill_est)
                   VALUES (?, 'GOOGL', 'CALL', 200, '2026-10-16',
                           2.62, 4.05, 286, ?, 'EXIT:TAKE_PROFIT',
                           286, 238, 2.62, 2.74, 4.05, 3.93, 0)""",
                ("2026-10-01T15:00:00+00:00", json.dumps(win)),
            )
            conn.execute(
                """INSERT INTO trade_history
                   (closed_at, ticker, direction, strike, expiration,
                    entry_price, exit_price, pnl, contract_json, notes,
                    pnl_mid, pnl_fill, entry_mid, entry_ask, exit_mid, exit_bid,
                    fill_est)
                   VALUES (?, 'IWM', 'PUT', 220, '2026-10-16',
                           2.00, 1.40, -60, ?, 'EXIT:STOP_LOSS',
                           -60, -78, 2.00, 2.08, 1.40, 1.30, 0)""",
                ("2026-10-01T18:00:00+00:00", json.dumps(lose)),
            )
            conn.execute(
                """INSERT INTO trade_history
                   (closed_at, ticker, entry_price, exit_price, pnl, notes)
                   VALUES ('2026-10-01T14:00:00+00:00', 'GOOGL', 2.62, 2.62, 0,
                           'PAPER_BUY_OPEN')"""
            )
            conn.execute(
                """INSERT INTO position_marks
                   (ts, trade_id, ticker, mark, pnl_pct, entry_price, stop_loss, take_profit)
                   VALUES ('2026-10-01T16:00:00+00:00', 't-win', 'GOOGL', 3.2, 61.0, 2.62, 2.10, 4.0)"""
            )
            conn.execute(
                """INSERT INTO position_marks
                   (ts, trade_id, ticker, mark, pnl_pct)
                   VALUES ('2026-10-01T16:30:00+00:00', 't-win', 'GOOGL', 2.9, 10.0)"""
            )
        with sqlite3.connect(self.hedge) as conn:
            _hedge(conn)
            conn.execute(
                "INSERT INTO circuit_breaker_state VALUES (1, 0, 'CLOSED', NULL, NULL, '2026-10-01')"
            )
            conn.execute(
                """INSERT INTO backtest_telemetry
                   (timestamp, scan_id, ticker, reasons_json, action_flag)
                   VALUES ('2026-10-01 15:00:00', 'scan-a', 'GOOGL', '["dead_zone"]', 'PASS')"""
            )
            conn.execute(
                """INSERT INTO backtest_telemetry
                   (timestamp, scan_id, ticker, reasons_json, action_flag)
                   VALUES ('2026-10-01 15:00:00', 'scan-a', 'IWM', '["below"]', 'PASS')"""
            )
            conn.execute(
                """INSERT INTO backtest_telemetry
                   (timestamp, scan_id, ticker, reasons_json, action_flag)
                   VALUES ('2026-10-02 15:00:00', 'scan-b', 'GOOGL', '["data_unavailable"]', 'PASS')"""
            )
            conn.execute(
                """INSERT INTO backtest_telemetry
                   (timestamp, scan_id, ticker, reasons_json, action_flag)
                   VALUES ('2026-10-02 15:00:00', 'scan-b', 'IWM', '["data_unavailable"]', 'PASS')"""
            )
        self.trades.write_text(json.dumps([
            {
                "trade_id": "t-open",
                "ticker": "NVDA",
                "direction": "CALL",
                "strike": 180,
                "expiration": "2026-10-16",
                "entry_price": 3.00,
                "entry_premium": 3.00,
                "entry_mid": 3.00,
                "entry_timestamp": "2026-10-01T14:30:00+00:00",
                "stop_loss": 2.40,
                "take_profit": 4.50,
                "quantity": 1,
            }
        ]), encoding="utf-8")
        with sqlite3.connect(self.news) as conn:
            conn.execute(
                """INSERT INTO position_marks
                   (ts, trade_id, ticker, mark, pnl_pct, entry_price, stop_loss, take_profit)
                   VALUES ('2026-10-01T19:00:00+00:00', 't-open', 'NVDA', 3.30, 10.0,
                           3.00, 2.40, 4.50)"""
            )
        (self.logs / "bot-2026-10-01.log").write_text(
            "\n".join([
                "----- 2026-10-01 08:40:00 CDT starting one worker -----",
                "[Scoring] v7a25760 thr=70",
                "[Earnings] CRITICAL: blackout protection unavailable for NVDA.",
                "🚀 30-MIN SCAN scan-20261001-090000-abc123 | 09:00 CDT | tickers=10",
                "[scan] GATE [1/10 open] ADMIT none | BLOCK below_thr×10",
                "⏱️ EXIT-ONLY PASS exit-20261001-093000-def456 | 09:30 CDT | 1 open",
                "<script>alert(1)</script>",
            ]) + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_readonly_connection_refuses_writes_and_does_not_create(self):
        missing = Path(self.tmp.name) / "absent.db"
        self.assertIsNone(connect_ro(missing))
        self.assertFalse(missing.exists())
        conn = connect_ro(self.news)
        self.assertIsNotNone(conn)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO trade_history (closed_at, entry_price, exit_price, pnl) VALUES ('x', 1, 1, 0)")
        conn.close()

    def test_report_metrics_and_hides_secrets(self):
        text = render_report(self.paths)
        self.assertIn("Closed trades: 2", text)
        self.assertIn("value: +0.17R", text)
        self.assertIn("value: 3.05", text)
        self.assertIn("dollars: $78.00", text)
        self.assertIn("percent of seed: 0.78%", text)
        self.assertIn("value: 0.74", text)
        self.assertIn("mean entry spread: 4.29%", text)
        self.assertIn("mean stop slippage: 50.00%", text)
        self.assertIn("coin_flip count: unavailable", text)
        self.assertIn("TAKE_PROFIT:", text)
        self.assertIn("STOP_LOSS:", text)
        self.assertIn("fill $160.00", text)
        self.assertIn("mid $226.00", text)
        self.assertIn("spread cost $66.00", text)
        self.assertIn("counted: 1", text)
        self.assertIn("2026-10-01", text)
        self.assertIn("excluded for data outage: 1", text)
        self.assertIn("2026-10-02", text)
        self.assertIn("RESET_LEDGER_ON_BOOT=false", text)
        self.assertNotIn(SECRET, text)
        self.assertIn("A restart does not wipe the trade tables", text)

    def test_dashboard_page_and_staleness(self):
        self.assertEqual(HOST, "127.0.0.1")
        self.assertEqual(PORT, 8050)
        morning = datetime(2026, 10, 1, 11, 0, tzinfo=CHI)
        snap = build_snapshot(self.paths, now=morning)
        page = render_page(snap)
        self.assertIn("v7a25760", page)
        self.assertIn("GATE [1/10 open]", page)
        self.assertIn("below_thr×10", page)
        self.assertIn("NVDA", page)
        self.assertIn("CALL 180 2026-10-16", page)
        self.assertIn("3.30", page)
        self.assertIn("blackout protection unavailable", page)
        self.assertIn("Last full scan was", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>alert", page)
        self.assertIn("TAKE_PROFIT", page)
        self.assertIn("+2.29R", page)
        evening = datetime(2026, 10, 1, 19, 30, tzinfo=CHI)
        quiet = render_page(build_snapshot(self.paths, now=evening))
        self.assertNotIn("Last full scan was", quiet)
        fresh = datetime(2026, 10, 1, 9, 20, tzinfo=CHI)
        held = render_page(build_snapshot(self.paths, now=fresh))
        self.assertNotIn("may be stuck", held)


if __name__ == "__main__":
    unittest.main()
