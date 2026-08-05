"""Stage 4 B1–B4: deterministic scan-path exits (no tracker daemon)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

# datetime used in B5 / C-D exit tests

import config
import position_exits


class TestGateDefaults(unittest.TestCase):
    def test_stage4_gate_defaults_env_tunable(self):
        self.assertEqual(config.GATE_MAX_CONCURRENT, 10)
        self.assertEqual(config.GATE_MAX_ENTRIES_PER_TICKER, 6)
        # Unchanged churn guards
        self.assertEqual(config.GATE_PERSIST_CYCLES, 2)
        self.assertEqual(config.GATE_FLIP_LOCK_MINUTES, 60)
        self.assertEqual(config.GATE_FLIP_OVERRIDE_SCORE, 85.0)
        self.assertEqual(config.GATE_REENTRY_COOLDOWN_MINUTES, 45)


class TestMarkLookup(unittest.TestCase):
    def test_mid_mark_from_chain(self):
        trade = {
            "ticker": "IWM",
            "direction": "CALL",
            "strike": 302.0,
            "expiration": "2026-08-05",
            "entry_price": 1.42,
            "stop_loss": 1.14,
            "take_profit": 2.13,
        }
        options = {
            "current_price": 302.10,
            "chains": {
                "2026-08-05": {
                    "calls": [
                        {
                            "strike": 302.0,
                            "bid": 0.90,
                            "ask": 1.10,
                            "lastPrice": 1.00,
                        }
                    ],
                    "puts": [],
                }
            },
        }
        info = position_exits.lookup_option_mark(trade, options)
        self.assertTrue(info["found"])
        self.assertAlmostEqual(info["mark"], 1.0)
        self.assertAlmostEqual(info["spot"], 302.10)
        self.assertAlmostEqual(info["bid"], 0.90)


class TestExitRules(unittest.TestCase):
    def setUp(self):
        position_exits.reset_eod_flags_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.trades_path = Path(self.tmp.name) / "active_trades.json"
        self.trades_path.write_text("[]", encoding="utf-8")

    def tearDown(self):
        position_exits.reset_eod_flags_for_tests()
        self.tmp.cleanup()

    def _trade(self, **over):
        base = {
            "trade_id": "t-iwm-1",
            "ticker": "IWM",
            "direction": "CALL",
            "strike": 302.0,
            "expiration": "2026-08-05",
            "entry_price": 1.42,
            "entry_premium": 1.42,
            "stop_loss": 1.14,
            "take_profit": 2.13,
            "entry_timestamp": "2026-08-05T14:32:00Z",
        }
        base.update(over)
        return base

    def _options(self, bid, ask, spot=302.10, exp="2026-08-05"):
        return {
            "current_price": spot,
            "chains": {
                exp: {
                    "calls": [
                        {"strike": 302.0, "bid": bid, "ask": ask, "lastPrice": (bid + ask) / 2}
                    ],
                    "puts": [],
                }
            },
        }

    def test_stop_loss_closes(self):
        trade = self._trade()
        scored = {"IWM": {"options_dict": self._options(0.01, 0.03), "card": None}}

        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "STOP_LOSS", "ok": True}
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[]
                    ):
                        summary = position_exits.run_scan_exits(
                            [trade],
                            scored,
                            scan_id="test",
                            session_date=date(2026, 8, 4),  # not expired yet
                            force_eod=False,
                        )
        self.assertEqual(len(summary["closed"]), 1)
        args, _kwargs = close.call_args
        self.assertEqual(args[2], "STOP_LOSS")
        self.assertAlmostEqual(args[1], 0.02, places=3)

    def test_take_profit_closes(self):
        trade = self._trade()
        scored = {"IWM": {"options_dict": self._options(2.20, 2.40), "card": None}}

        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "TAKE_PROFIT", "ok": True}
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[]
                    ):
                        summary = position_exits.run_scan_exits(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 4),
                            force_eod=False,
                        )
        self.assertEqual(summary["closed"][0]["reason"], "TAKE_PROFIT")

    def test_expiry_flatten_past_date_only(self):
        """Same-day (0DTE) is NOT expiry-flattened at the open — only past dates."""
        trade = self._trade(expiration="2026-08-04")
        scored = {"IWM": {"options_dict": self._options(0.50, 0.60), "card": None}}

        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "EXPIRY_FLATTEN", "ok": True}
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[]
                    ):
                        summary = position_exits.run_scan_exits(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 5),
                            force_eod=False,
                            now_cdt=datetime(2026, 8, 5, 10, 0, 0),
                        )
        self.assertEqual(close.call_args[0][2], "EXPIRY_FLATTEN")

    def test_zero_dte_flatten_at_1300_cdt(self):
        trade = self._trade(expiration="2026-08-05")
        scored = {"IWM": {"options_dict": self._options(0.50, 0.60), "card": None}}
        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "ZERO_DTE_FLATTEN", "ok": True}
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[]
                    ):
                        position_exits.run_scan_exits(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 5),
                            force_eod=False,
                            now_cdt=datetime(2026, 8, 5, 13, 5, 0),
                        )
        self.assertEqual(close.call_args[0][2], "ZERO_DTE_FLATTEN")

    def test_breakeven_lock_b5(self):
        trade = self._trade()
        trade["peak_pnl_pct"] = 30.0  # was +30%
        # mark back at entry 1.42
        scored = {"IWM": {"options_dict": self._options(1.40, 1.44), "card": None}}
        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "BREAKEVEN_LOCK", "ok": True}
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[]
                    ):
                        position_exits.run_scan_exits(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 4),
                            force_eod=False,
                            now_cdt=datetime(2026, 8, 5, 11, 0, 0),
                        )
        self.assertEqual(close.call_args[0][2], "BREAKEVEN_LOCK")

    def test_eod_flatten_all(self):
        trade = self._trade(expiration="2026-08-12")  # not expired
        scored = {"IWM": {"options_dict": self._options(1.00, 1.20), "card": None}}

        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "EOD_FLATTEN", "ok": True}
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[]
                    ):
                        summary = position_exits.run_scan_exits(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 5),
                            force_eod=True,
                        )
        self.assertTrue(summary["eod_triggered"])
        self.assertEqual(close.call_args[0][2], "EOD_FLATTEN")

    def test_record_mark_persists(self):
        trade = self._trade()
        mark_info = {
            "bid": 1.0,
            "ask": 1.2,
            "mark": 1.1,
            "spot": 302.0,
            "found": True,
        }
        with mock.patch.object(config, "NEWS_DB_PATH", self.db):
            ok = position_exits.record_position_mark(
                trade, mark_info, live_score=81.0, scan_id="s1", db_path=self.db
            )
        self.assertTrue(ok)
        import sqlite3

        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT ticker, mark, live_score, pnl_pct FROM position_marks"
            ).fetchone()
        self.assertEqual(row[0], "IWM")
        self.assertAlmostEqual(row[1], 1.1)
        self.assertAlmostEqual(row[2], 81.0)
        # (1.1 - 1.42) / 1.42 * 100
        self.assertAlmostEqual(row[3], round((1.1 - 1.42) / 1.42 * 100, 2))

    def test_eod_window_boundary(self):
        # Naive datetimes are interpreted as America/Chicago wall time.
        dt_before = datetime(2026, 8, 5, 14, 44, 0)
        dt_at = datetime(2026, 8, 5, 14, 45, 0)
        with mock.patch.object(config, "EXIT_EOD_FLATTEN_HOUR", 14):
            with mock.patch.object(config, "EXIT_EOD_FLATTEN_MINUTE", 45):
                self.assertFalse(position_exits.is_eod_flatten_window(dt_before))
                self.assertTrue(position_exits.is_eod_flatten_window(dt_at))


class TestCloseWiresGate(unittest.TestCase):
    def test_close_calls_paper_sell_and_remove(self):
        trade = {
            "trade_id": "x1",
            "ticker": "NVDA",
            "direction": "CALL",
            "entry_price": 2.0,
            "strike": 100,
            "expiration": "2026-08-12",
        }
        with mock.patch("virtual_broker.paper_sell") as sell:
            sell.return_value = {"ok": True, "pnl": -20.0}
            with mock.patch("tracker_agent.remove_active_trade") as rem:
                rem.return_value = True
                out = position_exits.close_open_position(trade, 1.6, "STOP_LOSS")
        self.assertTrue(out["ok"])
        sell.assert_called_once()
        self.assertEqual(sell.call_args.kwargs.get("notes"), "EXIT:STOP_LOSS")
        rem.assert_called_once()


if __name__ == "__main__":
    unittest.main()
