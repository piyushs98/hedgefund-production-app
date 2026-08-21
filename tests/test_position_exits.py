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
import virtual_broker


class TestGateDefaults(unittest.TestCase):
    def test_stage4_gate_defaults_env_tunable(self):
        self.assertEqual(config.GATE_MAX_CONCURRENT, 10)
        self.assertEqual(config.GATE_MAX_ENTRIES_PER_TICKER, 6)
        # Unchanged churn guards
        self.assertEqual(config.GATE_PERSIST_CYCLES, 2)
        self.assertEqual(config.GATE_FLIP_LOCK_MINUTES, 60)
        self.assertEqual(config.GATE_FLIP_OVERRIDE_SCORE, 85.0)
        self.assertEqual(config.GATE_REENTRY_COOLDOWN_MINUTES, 45)
        self.assertEqual(config.CARRY_MIN_DTE, 2)
        self.assertEqual(config.EXIT_INTERVAL_SECONDS, 300)
        self.assertEqual(config.FULL_SCAN_INTERVAL_SECONDS, 1800)
        self.assertEqual(config.TIME_STOP_SCORE_EXEMPT, 80.0)
        self.assertEqual(config.MIN_EXTRINSIC_PCT, 10.0)
        self.assertEqual(config.MAX_CONTRACT_SPREAD_PCT, 8.0)
        self.assertEqual(config.GATE_POST_EXIT_COOLDOWN_MINUTES, 45)
        self.assertEqual(config.THESIS_EXIT_SCORE, 55.0)
        self.assertEqual(config.ACCOUNT_SIZE, 10000.0)
        self.assertEqual(config.STARTING_BUYING_POWER, config.ACCOUNT_SIZE)
        self.assertEqual(config.RISK_PER_TRADE_PCT, 1.5)
        self.assertEqual(config.MAX_CONTRACTS_PER_TRADE, 10)
        self.assertEqual(config.FIRST_FULL_SCAN_HOUR, 8)
        self.assertEqual(config.FIRST_FULL_SCAN_MINUTE, 45)


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

    def test_eod_flattens_short_dated_only(self):
        """CARRY_MIN_DTE=2: cal_dte 1 flattens; cal_dte>=2 may carry."""
        short = self._trade(expiration="2026-08-06", trade_id="short")  # cal_dte=1 on Aug 5
        long = self._trade(
            expiration="2026-08-12",
            trade_id="long",
            ticker="SPY",
            strike=500.0,
        )
        # Fresh entry so TIME_STOP does not fire on the carry-eligible name
        from datetime import timezone as _tz
        long["entry_timestamp"] = datetime.now(_tz.utc).isoformat()
        scored = {
            "IWM": {"options_dict": self._options(1.00, 1.20), "card": None},
            "SPY": {
                "options_dict": {
                    "current_price": 500.0,
                    "chains": {
                        "2026-08-12": {
                            "calls": [
                                {
                                    "strike": 500.0,
                                    # Above SL (1.14) so only short-dated EOD closes
                                    "bid": 1.40,
                                    "ask": 1.50,
                                    "lastPrice": 1.45,
                                }
                            ],
                            "puts": [],
                        }
                    },
                },
                "card": None,
            },
        }

        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "EOD_FLATTEN", "ok": True}
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[]
                    ):
                        with mock.patch.object(config, "CARRY_MIN_DTE", 2):
                            summary = position_exits.run_scan_exits(
                                [short, long],
                                scored,
                                session_date=date(2026, 8, 5),
                                force_eod=True,
                                now_cdt=datetime(2026, 8, 5, 14, 50, 0),
                            )
        self.assertTrue(summary["eod_triggered"])
        # Only short-dated closed
        self.assertEqual(close.call_count, 1)
        self.assertEqual(close.call_args[0][2], "EOD_FLATTEN")
        self.assertEqual(close.call_args[0][0].get("trade_id"), "short")

    def test_time_stop_skipped_logged_when_score_exempt(self):
        from datetime import timezone as _tz, timedelta

        trade = self._trade(expiration="2026-08-14")  # still live; not expiry flatten
        trade["entry_timestamp"] = (
            datetime(2026, 8, 7, 10, 0, 0, tzinfo=_tz.utc) - timedelta(hours=3)
        ).isoformat()
        trade["peak_pnl_pct"] = 5.0
        trade["last_live_score"] = 84.0
        # Mark near entry → |pnl| < 10%; options on future exp
        scored = {
            "IWM": {
                "options_dict": self._options(1.40, 1.44, exp="2026-08-14"),
                "card": mock.Mock(total_score=84.0),
            }
        }
        with mock.patch("position_exits.close_open_position") as close:
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[trade]
                    ):
                        with mock.patch.object(config, "TIME_STOP_SCORE_EXEMPT", 80.0):
                            summary = position_exits.run_scan_exits(
                                [trade],
                                scored,
                                session_date=date(2026, 8, 7),
                                force_eod=False,
                                now_cdt=datetime(2026, 8, 7, 14, 0, 0),
                            )
        close.assert_not_called()
        self.assertTrue(
            any("TIME_STOP SKIPPED IWM" in s for s in summary.get("time_stop_skipped", [])),
            summary.get("time_stop_skipped"),
        )

    def test_mark_fail_streak_alerts_at_two(self):
        trade = self._trade()
        trade["mark_fail_streak"] = 1  # already failed once
        # No options → mark fails
        scored = {"IWM": {"options_dict": {"current_price": 302.0, "chains": {}}, "card": None}}
        alerts = []

        def _capture(msg):
            alerts.append(msg)
            return True

        with mock.patch("position_exits.close_open_position") as close:
            with mock.patch("tracker_agent.save_active_trade", return_value=True):
                with mock.patch("broadcaster.send_discord_alert", side_effect=_capture):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[trade]
                    ):
                        with mock.patch.object(config, "MARK_FAIL_ALERT_STREAK", 2):
                            summary = position_exits.run_scan_exits(
                                [trade],
                                scored,
                                session_date=date(2026, 8, 5),
                                force_eod=False,
                                now_cdt=datetime(2026, 8, 5, 11, 0, 0),
                            )
        close.assert_not_called()
        self.assertEqual(summary["marks_failed"], 1)
        self.assertEqual(summary["marks_ok"], 0)
        self.assertEqual(summary["positions_checked"], 1)
        self.assertTrue(summary.get("all_marks_failed"))
        self.assertTrue(any("MARK FAILED" in a for a in alerts))
        self.assertIn("marks: checked=1", summary.get("mark_summary_line", ""))

    def test_thesis_void_closes_on_score_collapse(self):
        """Live score < 55 with entry_score >= 70 closes regardless of P&L."""
        trade = self._trade(expiration="2026-08-12")
        trade["entry_score"] = 86.0
        trade["entry_price"] = 5.90
        trade["entry_premium"] = 5.90
        trade["stop_loss"] = 4.72
        trade["take_profit"] = 8.85
        # Mark still well above SL (open +47%) — thesis is gone
        scored = {
            "IWM": {
                "options_dict": self._options(8.60, 8.80),
                "card": mock.Mock(total_score=12.0),
            }
        }
        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "THESIS_VOID", "ok": True}
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
                            now_cdt=datetime(2026, 8, 5, 8, 35, 0),
                        )
        self.assertEqual(close.call_args[0][2], "THESIS_VOID")
        self.assertAlmostEqual(close.call_args[0][1], 8.70, places=2)

    def test_thesis_void_skips_marginal_entry(self):
        """Hysteresis: entry_score 65 must not thesis-void even at live 12."""
        trade = self._trade(expiration="2026-08-12")
        trade["entry_score"] = 65.0
        scored = {
            "IWM": {
                "options_dict": self._options(1.50, 1.70),
                "card": mock.Mock(total_score=12.0),
            }
        }
        with mock.patch("position_exits.close_open_position") as close:
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[trade]
                    ):
                        position_exits.run_scan_exits(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 5),
                            force_eod=False,
                            now_cdt=datetime(2026, 8, 5, 11, 0, 0),
                        )
        close.assert_not_called()

    def test_thesis_void_skips_when_live_still_above_floor(self):
        trade = self._trade(expiration="2026-08-12")
        trade["entry_score"] = 88.0
        scored = {
            "IWM": {
                "options_dict": self._options(1.50, 1.70),
                "card": mock.Mock(total_score=78.0),
            }
        }
        with mock.patch("position_exits.close_open_position") as close:
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[trade]
                    ):
                        position_exits.run_scan_exits(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 5),
                            force_eod=False,
                            now_cdt=datetime(2026, 8, 5, 11, 0, 0),
                        )
        close.assert_not_called()

    def test_take_profit_beats_thesis_void(self):
        trade = self._trade(expiration="2026-08-12")
        trade["entry_score"] = 88.0
        trade["take_profit"] = 1.40
        scored = {
            "IWM": {
                "options_dict": self._options(1.50, 1.70),
                "card": mock.Mock(total_score=12.0),
            }
        }
        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ticker": "IWM", "reason": "TAKE_PROFIT", "ok": True}
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
                            now_cdt=datetime(2026, 8, 5, 11, 0, 0),
                        )
        self.assertEqual(close.call_args[0][2], "TAKE_PROFIT")

    def test_morning_carry_review_thesis_void(self):
        trade = self._trade(expiration="2026-08-12")
        trade["entry_score"] = 84.0
        trade["entry_price"] = 2.37
        trade["entry_premium"] = 2.37
        trade["stop_loss"] = 1.90
        trade["take_profit"] = 3.56
        scored = {
            "IWM": {
                "options_dict": self._options(3.00, 3.20),
                "card": mock.Mock(total_score=49.0),
                "pivot_data": {"pivot": 301.5, "close": 302.0},
            }
        }
        position_exits.reset_eod_flags_for_tests()
        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {
                "ticker": "IWM",
                "reason": "CARRY_THESIS_VOID",
                "ok": True,
            }
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch("broadcaster.send_discord_alert", return_value=True):
                        summary = position_exits.run_morning_carry_review(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 5),
                            now_cdt=datetime(2026, 8, 5, 8, 35, 0),
                            force=True,
                        )
        self.assertTrue(summary["ran"])
        self.assertEqual(close.call_args[0][2], "CARRY_THESIS_VOID")
        self.assertIn("CLOSE THESIS_VOID", summary["lines"][0])
        self.assertIn("score 84->49", summary["lines"][0])

    def test_morning_carry_review_hold_line(self):
        trade = self._trade(expiration="2026-08-12")
        trade["entry_score"] = 78.0
        trade["entry_pivot"] = 300.0
        trade["entry_dte"] = 7
        scored = {
            "IWM": {
                "options_dict": self._options(1.50, 1.70),
                "card": mock.Mock(total_score=72.0),
                "pivot_data": {"pivot": 301.5, "close": 302.0},
            }
        }
        position_exits.reset_eod_flags_for_tests()
        with mock.patch("position_exits.close_open_position") as close:
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch("broadcaster.send_discord_alert", return_value=True):
                        summary = position_exits.run_morning_carry_review(
                            [trade],
                            scored,
                            session_date=date(2026, 8, 5),
                            now_cdt=datetime(2026, 8, 5, 8, 35, 0),
                            force=True,
                        )
        self.assertTrue(summary["ran"])
        self.assertEqual(len(summary["lines"]), 1)
        self.assertIn("HOLD", summary["lines"][0])
        self.assertIn("score 78->72", summary["lines"][0])
        self.assertIn("pivot 300.00->301.50", summary["lines"][0])
        close.assert_not_called()

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

    def test_full_scan_window_starts_0845(self):
        from midday_delta import is_full_scan_window

        self.assertFalse(is_full_scan_window(datetime(2026, 8, 20, 8, 30, 0)))
        self.assertFalse(is_full_scan_window(datetime(2026, 8, 20, 8, 44, 0)))
        self.assertTrue(is_full_scan_window(datetime(2026, 8, 20, 8, 45, 0)))
        self.assertTrue(is_full_scan_window(datetime(2026, 8, 20, 9, 15, 0)))

    def test_eod_book_line_at_1445(self):
        alerts = []
        position_exits.reset_eod_flags_for_tests()
        with mock.patch.object(virtual_broker, "DB_PATH", self.db):
            with mock.patch.object(config, "NEWS_DB_PATH", self.db):
                with mock.patch(
                    "broadcaster.send_discord_alert",
                    side_effect=lambda m: alerts.append(m) or True,
                ):
                    with mock.patch(
                        "tracker_agent.load_active_trades", return_value=[]
                    ):
                        virtual_broker.ensure_ledger()
                        line = position_exits.maybe_emit_eod_book(
                            datetime(2026, 8, 20, 14, 45, 0)
                        )
                        again = position_exits.maybe_emit_eod_book(
                            datetime(2026, 8, 20, 14, 50, 0)
                        )
        self.assertIsNotNone(line)
        self.assertTrue(line.startswith("BOOK: start "))
        self.assertIn("peak deployed", line)
        self.assertIn("realized", line)
        self.assertIn("end ", line)
        self.assertIsNone(again)  # once per day
        self.assertEqual(len(alerts), 1)

    def test_eod_window_boundary(self):
        # Naive datetimes are interpreted as America/Chicago wall time.
        dt_before = datetime(2026, 8, 5, 14, 44, 0)
        dt_at = datetime(2026, 8, 5, 14, 45, 0)
        with mock.patch.object(config, "EXIT_EOD_FLATTEN_HOUR", 14):
            with mock.patch.object(config, "EXIT_EOD_FLATTEN_MINUTE", 45):
                self.assertFalse(position_exits.is_eod_flatten_window(dt_before))
                self.assertTrue(position_exits.is_eod_flatten_window(dt_at))


class TestAug20ThesisReplay(unittest.TestCase):
    """What Aug 20 carry would have done with THESIS_VOID armed."""

    def _reason(self, trade, mark, live_score):
        return position_exits.evaluate_exit_reason_for_mark(
            trade,
            mark,
            sess=date(2026, 8, 20),
            now=datetime(2026, 8, 20, 8, 30, 0),
            include_time_stop=False,
            do_eod=False,
            live_score=live_score,
        )

    def test_tsla_would_void_at_carry_mark(self):
        trade = {
            "ticker": "TSLA",
            "direction": "CALL",
            "strike": 345.0,
            "expiration": "2026-08-28",
            "entry_price": 5.90,
            "stop_loss": 4.72,
            "take_profit": 8.85,
            "entry_score": 86.0,
        }
        reason, px = self._reason(trade, 8.70, 12.0)
        self.assertEqual(reason, "THESIS_VOID")
        self.assertAlmostEqual(px, 8.70)
        # vs later BREAKEVEN_LOCK at 5.50 (−$40)
        void_pnl = (8.70 - 5.90) * 100
        actual_pnl = (5.50 - 5.90) * 100
        self.assertAlmostEqual(void_pnl, 280.0)
        self.assertAlmostEqual(actual_pnl, -40.0)

    def test_amzn_would_void_at_carry_mark(self):
        trade = {
            "ticker": "AMZN",
            "direction": "CALL",
            "strike": 265.0,
            "expiration": "2026-08-28",
            "entry_price": 2.37,
            "stop_loss": 1.90,
            "take_profit": 3.56,
            "entry_score": 84.0,
        }
        reason, px = self._reason(trade, 3.10, 49.0)
        self.assertEqual(reason, "THESIS_VOID")
        self.assertAlmostEqual(px, 3.10)
        void_pnl = (3.10 - 2.37) * 100
        self.assertAlmostEqual(void_pnl, 73.0, places=0)

    def test_aapl_would_not_void_score_survived(self):
        trade = {
            "ticker": "AAPL",
            "direction": "CALL",
            "strike": 320.0,
            "expiration": "2026-08-28",
            "entry_price": 0.95,
            "stop_loss": 0.76,
            "take_profit": 1.43,
            "entry_score": 88.0,
        }
        # Score 78 is above 55 — TP still the correct close
        reason, px = self._reason(trade, 1.45, 78.0)
        self.assertEqual(reason, "TAKE_PROFIT")
        self.assertAlmostEqual(px, 1.45)
        self.assertNotEqual(reason, "THESIS_VOID")


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
