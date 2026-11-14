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

from ops.dashboard import (
    HOST,
    PORT,
    build_snapshot,
    equity_curve,
    portfolio_now,
    render_page,
    unrealized_fill_at_bid,
)
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
                "version": "vec40b2a",
                "entry_timestamp": "2026-10-01T14:30:00+00:00",
            }
            lose = {
                "trade_id": "t-lose",
                "quantity": 1,
                "stop_loss": 1.60,
                "ticker": "IWM",
                "version": "vec40b2a",
                "entry_timestamp": "2026-10-01T16:30:00+00:00",
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
        self.assertIn("ratio: 0.74", text)
        self.assertIn("median: 0.74", text)
        self.assertIn("day-block 90% interval:", text)
        self.assertIn("trade sequence: dollars: $78.00", text)
        self.assertIn("daily closes:", text)
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
        self.assertIn("Excluded from measurement (unmonitored Oct 5–6): 0", text)

    def test_unmonitored_gap_lot_is_left_out_of_expectancy(self):
        gap_id = "9d590ab8-05cf-481e-8989-d3b7fe4bb179"
        gap = {
            "trade_id": gap_id,
            "quantity": 4,
            "stop_loss": 1.22,
            "excluded_from_measurement": "y",
        }
        with sqlite3.connect(self.news) as conn:
            conn.execute(
                """INSERT INTO trade_history
                   (closed_at, ticker, direction, strike, expiration,
                    entry_price, exit_price, pnl, contract_json, notes,
                    pnl_mid, pnl_fill, entry_mid, entry_ask, exit_mid, exit_bid,
                    fill_est)
                   VALUES (?, 'TSLA', 'CALL', 377.5, '2026-10-05',
                           1.53, 0.05, -592, ?, 'EXIT:EXPIRY_FLATTEN',
                           -592, 5000, 1.53, 1.54, 0.05, 0.01, 0)""",
                ("2026-10-07T14:00:00+00:00", json.dumps(gap)),
            )
            conn.execute(
                """INSERT INTO position_marks
                   (ts, trade_id, ticker, mark, pnl_pct)
                   VALUES ('2026-10-02T19:50:00+00:00', ?, 'TSLA', 2.0, 30.0)""",
                (gap_id,),
            )
        text = render_report(self.paths)
        self.assertIn("Closed trades: 2", text)
        self.assertIn("Excluded from measurement (unmonitored Oct 5–6): 1", text)
        self.assertIn("value: +0.17R", text)
        self.assertNotIn(gap_id, text)

    def test_mfe_ratio_is_not_the_mean_of_ratios(self):
        from ops.metrics import mfe_capture
        trades = [
            {"trade_id": "a", "entry_mid": 1.0, "exit_mid": 1.0, "qty": 1, "pnl_fill": 100.0},
            {"trade_id": "b", "entry_mid": 1.0, "exit_mid": 1.0, "qty": 1, "pnl_fill": -100.0},
        ]
        marks = [
            {"trade_id": "a", "pnl_pct": 50.0},
            {"trade_id": "b", "pnl_pct": 1.0},
        ]
        out = mfe_capture(trades, marks)
        self.assertAlmostEqual(out["ratio"], 0.0)
        self.assertAlmostEqual(out["median"], (2.0 + -100.0) / 2.0)
        self.assertNotAlmostEqual(out["ratio"], out["median"])

    def test_regime_edges_and_day_block_is_stable(self):
        from ops.metrics import classify_regime, day_block_expectancy
        self.assertEqual(classify_regime(100.0, 100.5), "trend_up")
        self.assertEqual(classify_regime(100.0, 99.5), "trend_down")
        self.assertEqual(classify_regime(100.0, 100.49), "chop")
        trades = [
            {"session_date": datetime(2026, 10, 8).date(), "r_fill": 1.0},
            {"session_date": datetime(2026, 10, 8).date(), "r_fill": -1.0},
            {"session_date": datetime(2026, 10, 9).date(), "r_fill": 0.5},
        ]
        first = day_block_expectancy(trades, draws=200, seed=7)
        second = day_block_expectancy(trades, draws=200, seed=7)
        self.assertEqual(first, second)
        self.assertLessEqual(first["low"], first["high"])
        self.assertEqual(first["sessions"], 2)

    def test_oct2_is_outside_the_measured_sample(self):
        oct2 = {
            "trade_id": "t-oct2",
            "quantity": 1,
            "stop_loss": 1.0,
            "version": "v7a25760",
            "entry_timestamp": "2026-10-02T15:00:00+00:00",
        }
        with sqlite3.connect(self.news) as conn:
            conn.execute(
                """INSERT INTO trade_history
                   (closed_at, ticker, direction, strike, expiration,
                    entry_price, exit_price, pnl, contract_json, notes,
                    pnl_mid, pnl_fill, entry_mid, entry_ask, exit_mid, exit_bid,
                    fill_est)
                   VALUES (?, 'SPY', 'CALL', 500, '2026-10-09',
                           2.00, 1.00, -100, ?, 'EXIT:STOP_LOSS',
                           -100, -120, 2.00, 2.10, 1.00, 0.90, 0)""",
                ("2026-10-02T16:00:00+00:00", json.dumps(oct2)),
            )
        text = render_report(self.paths)
        self.assertIn("Closed trades: 2", text)
        self.assertIn("Oct 2, version v7a25760, shown in its own section: 1", text)
        self.assertIn("OCT 2  (version v7a25760, not in the measured sample)", text)
        self.assertIn("expectancy: -1.20R", text)
        self.assertIn("SCORE SPLIT  (vec40b2a; entry_score 80+ and 70-79)", text)
        self.assertIn("70-79", text)
        self.assertIn("v9c3cc4d  (closed sample, not in the measured sample)", text)
        self.assertIn("80+ CARRY SHADOW", text)

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
        self.assertNotIn(SECRET, page)
        self.assertIn("id='equity'", page)
        self.assertIn("id='analysis'", page)
        self.assertIn("id='portfolio-chart'", page)
        self.assertIn('href="#analysis"', page)
        self.assertIn("Portfolio value now", page)
        self.assertIn("PAPER CASH (MID BASIS)", page)
        self.assertIn(
            "Counts trades at mid, so it's overstated by the spread paid. "
            "Portfolio value above is the real figure.",
            page,
        )
        self.assertIn("Today's change", page)
        self.assertIn("Since start", page)
        self.assertIn("+$160.00", page)
        self.assertIn("+1.60%", page)
        self.assertIn(
            "Measured: 2 trades on vec40b2a. "
            "Excluded: Oct 2 (older version, 0 trades), "
            "unmonitored Oct 5-6 (0 lots).",
            page,
        )
        self.assertIn("80+", page)
        self.assertIn("70-79", page)
        self.assertIn("Closed sample · v9c3cc4d", page)
        self.assertIn('id="book"', page)
        self.assertIn('id="sample"', page)
        self.assertIn("id='equity-chart'", page)
        self.assertIn("id='equity-fallback'", page)
        self.assertIn("chart.js@4.4.6/dist/chart.umd.min.js", page)
        self.assertIn("defer", page)
        self.assertIn("Measurement start is 2026-10-07.", page)
        self.assertIn("measurement start", page)
        self.assertIn("Fill equity", page)
        self.assertIn("Mid equity", page)
        self.assertIn("Day fill P&amp;L", page)
        self.assertIn("Unrealized at bid", page)
        self.assertIn("$10,160.00", page)
        self.assertIn("$10,226.00", page)
        self.assertIn("$160.00", page)
        self.assertNotIn("2026-10-01 open", page)
        blob = page.split('id="equity-data">', 1)[1].split("</script>", 1)[0]
        payload = json.loads(blob)
        self.assertEqual(payload["seed"], 10000)
        self.assertEqual(payload["measurement_start"], "2026-10-07")
        self.assertIsNone(payload["version_change"])
        self.assertEqual(len(payload["points"]), 1)
        point = payload["points"][0]
        self.assertEqual(point["date"], "2026-10-01")
        self.assertEqual(point["fill"], 10160)
        self.assertEqual(point["mid"], 10226)
        self.assertEqual(point["day_fill"], 160)
        self.assertEqual(point["n"], 2)
        self.assertEqual(point["peak"], 10160)
        self.assertFalse(point["open"])
        self.assertNotIn("9926", blob)
        simple = json.loads(page.split('id="portfolio-data">', 1)[1].split("</script>", 1)[0])
        self.assertEqual(len(simple["points"]), 1)
        self.assertEqual(simple["points"][0]["label"], "2026-10-01")
        self.assertEqual(simple["points"][0]["value"], 10160)
        self.assertNotIn("mid", page.split('id="portfolio-data">', 1)[1].split("</script>", 1)[0])
        evening = datetime(2026, 10, 1, 19, 30, tzinfo=CHI)
        quiet = render_page(build_snapshot(self.paths, now=evening))
        self.assertNotIn("Last full scan was", quiet)
        fresh = datetime(2026, 10, 1, 9, 20, tzinfo=CHI)
        held = render_page(build_snapshot(self.paths, now=fresh))
        self.assertNotIn("may be stuck", held)

    def test_equity_curve_is_fill_plus_seed_and_open_mark_is_bid(self):
        from datetime import date
        closed = [
            {
                "session_date": date(2026, 10, 2),
                "pnl_fill": -100.0,
                "pnl_mid": -40.0,
                "version": "v7a25760",
            },
            {
                "session_date": date(2026, 10, 7),
                "pnl_fill": 50.0,
                "pnl_mid": 80.0,
                "version": "v9c3cc4d",
            },
            {
                "session_date": date(2026, 10, 7),
                "pnl_fill": 25.0,
                "pnl_mid": 30.0,
                "version": "v9c3cc4d",
            },
        ]
        opens = [
            {"trade_id": "a", "entry_ask": 2.00, "quantity": 1, "ticker": "SPY"},
            {"trade_id": "b", "entry_ask": 1.00, "quantity": 2, "ticker": "QQQ"},
            {"trade_id": "c", "quantity": 1, "ticker": "TSLA"},
        ]
        marks = {
            "a": {"bid": 1.50},
            "b": {"bid": 1.25},
            "c": {"bid": 3.00},
        }
        curve = equity_curve(closed, opens, marks, date(2026, 10, 8))
        self.assertEqual(
            curve["version_change"],
            {"date": "2026-10-07", "from": "v7a25760", "to": "v9c3cc4d"},
        )
        self.assertEqual(curve["open_marked"], 2)
        self.assertEqual(curve["open_skipped"], 1)
        pts = curve["points"]
        self.assertEqual(pts[0]["fill"], 9900)
        self.assertEqual(pts[0]["mid"], 9960)
        self.assertEqual(pts[0]["peak"], 10000)
        self.assertEqual(pts[0]["day_fill"], -100)
        self.assertEqual(pts[0]["n"], 1)
        self.assertEqual(pts[1]["date"], "2026-10-07")
        self.assertEqual(pts[1]["fill"], 9975)
        self.assertEqual(pts[1]["mid"], 10070)
        self.assertEqual(pts[1]["day_fill"], 75)
        self.assertEqual(pts[1]["n"], 2)
        self.assertEqual(pts[1]["peak"], 10000)
        self.assertFalse(pts[1]["open"])
        self.assertTrue(pts[2]["open"])
        self.assertEqual(pts[2]["label"], "2026-10-08 open")
        self.assertEqual(pts[2]["fill"], 9975)
        self.assertEqual(pts[2]["mid"], 10070)
        self.assertEqual(pts[2]["day_fill"], 0)
        self.assertEqual(pts[2]["n"], 0)
        self.assertEqual(pts[2]["unrealized"], 0)
        self.assertIsNone(pts[2]["peak"])
        missing = unrealized_fill_at_bid(
            [{"trade_id": "a", "entry_ask": 1.0, "quantity": 1, "ticker": "SPY"}],
            {},
        )
        self.assertIsNone(missing["pnl"])
        only = equity_curve(
            [],
            [{"trade_id": "a", "entry_ask": 2.0, "quantity": 1, "ticker": "SPY"}],
            {"a": {"bid": 2.5}},
            date(2026, 11, 3),
        )
        self.assertEqual(only["points"][0]["label"], "start")
        self.assertEqual(only["points"][0]["fill"], 10000)
        self.assertTrue(only["points"][1]["open"])
        self.assertEqual(only["points"][1]["fill"], 10050)
        self.assertEqual(only["points"][1]["unrealized"], 50)
        stats = portfolio_now(curve, date(2026, 10, 8))
        self.assertEqual(
            [point["label"] for point in stats["points"]],
            ["2026-10-02", "2026-10-07", "2026-10-08"],
        )
        self.assertEqual([point["value"] for point in stats["points"]], [9900, 9975, 9975])
        self.assertEqual(stats["value"], 9975)
        self.assertEqual(stats["today_change"], 0)
        self.assertEqual(stats["since_change"], -25)
        flat = portfolio_now(only, date(2026, 11, 3))
        self.assertEqual([point["label"] for point in flat["points"]], ["2026-11-03"])
        self.assertEqual(flat["value"], 10050)
        self.assertEqual(flat["today_change"], 50)
        self.assertEqual(flat["since_change"], 50)
        page = render_page({
            "equity": curve,
            "status": _quiet_status(),
            "now": datetime(2026, 10, 8, 12, tzinfo=CHI),
            "today": _empty_today(),
            "positions": [],
            "summary": {},
            "tail": [],
            "log_path": None,
            "breaker": "CLOSED",
            "reset_ledger": "false",
            "news_db": "news.db",
            "hedge_db": "hedge.db",
        })
        self.assertIn("Version change on 2026-10-07: v7a25760 → v9c3cc4d.", page)
        self.assertIn("2026-10-08 open", page)
        self.assertIn("The dotted segment marks 2 open lots", page)
        self.assertIn("$9,975.00", page)
        self.assertIn("-$25.00", page)
        self.assertIn("-0.25%", page)
        self.assertIn("id='analysis'", page)
        self.assertIn('id="book"', page)
        self.assertIn('id="tape"', page)


def _quiet_status():
    return {
        "market_open": False,
        "holiday": False,
        "stale": False,
        "stale_text": "",
        "criticals": [],
        "gate": None,
        "version": "v9c3cc4d",
        "last_scan": None,
        "last_exit": None,
        "crumb_hot": False,
        "crumb": "quiet",
    }


def _empty_today():
    return {
        "entries": 0,
        "closes": 0,
        "realized_fill": 0,
        "spread_cost": 0,
        "closed": [],
    }


if __name__ == "__main__":
    unittest.main()
