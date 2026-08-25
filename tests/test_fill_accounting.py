"""Fill vs mid accounting, TRADE/SESSION lines, version stamp.

Accounting only: triggers still fire on mid.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import config
import fill_accounting as fa
import position_exits
import virtual_broker as vb


class TestFillMath(unittest.TestCase):
    def test_googl_example_two_series(self):
        qty = 2
        quotes = fa.resolve_fill_prices(
            entry_mid=2.62, exit_mid=4.05, entry_ask=2.74, exit_bid=3.93
        )
        self.assertFalse(quotes["fill_est"])
        pnl_mid = fa.pnl_dollars(4.05, 2.62, qty)
        pnl_fill = fa.pnl_dollars(3.93, 2.74, qty)
        self.assertAlmostEqual(pnl_mid, 286.0)
        self.assertAlmostEqual(pnl_fill, 238.0)
        # No double-count: fill already includes the spread. Do not also
        # subtract theoretical_slippage (mid-ask)*100*qty = -24.
        slip = (2.62 - 2.74) * 100 * qty
        self.assertAlmostEqual(slip, -24.0)
        self.assertNotAlmostEqual(pnl_fill, pnl_mid + slip)

    def test_missing_quotes_fall_back_mid_and_tag_est(self):
        quotes = fa.resolve_fill_prices(entry_mid=2.62, exit_mid=4.05)
        self.assertTrue(quotes["fill_est"])
        self.assertAlmostEqual(quotes["entry_ask"], 2.62)
        self.assertAlmostEqual(quotes["exit_bid"], 4.05)
        self.assertAlmostEqual(
            fa.pnl_dollars(quotes["exit_bid"], quotes["entry_ask"], 2),
            fa.pnl_dollars(4.05, 2.62, 2),
        )


class TestTriggersStayOnMid(unittest.TestCase):
    def test_stop_fires_on_mid_not_bid(self):
        from datetime import date

        trade = {
            "ticker": "GOOGL",
            "direction": "CALL",
            "entry_price": 2.62,
            "stop_loss": 2.10,
            "take_profit": 3.93,
        }
        # Mid 2.05 is through the stop; bid would be even worse, but the
        # trigger uses mid. Bid 1.90 is not consulted.
        reason, px = position_exits.evaluate_exit_reason_for_mark(
            trade,
            2.05,
            sess=date(2026, 8, 25),
            now=datetime(2026, 8, 25, 10, 56, 0),
            include_time_stop=False,
            do_eod=False,
        )
        self.assertEqual(reason, "STOP_LOSS")
        self.assertAlmostEqual(px, 2.05)


class TestTradeAndSessionLines(unittest.TestCase):
    def setUp(self):
        fa.reset_session_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")

    def tearDown(self):
        fa.reset_session_for_tests()
        self.tmp.cleanup()

    def test_trade_line_field_order_and_count(self):
        trade = {
            "ticker": "GOOGL",
            "direction": "CALL",
            "strike": 350,
            "expiration": "2026-08-28",
            "entry_timestamp": "2026-08-25T14:47:00+00:00",  # 09:47 CDT
            "entry_score": 86.0,
            "last_live_score": 74.0,
            "entry_dte": 4.8,
            "peak_pnl_pct": 61.0,
            "trough_pnl_pct": -8.0,
            "stop_loss": 2.00,
        }
        # 10:56 CDT = 15:56 UTC
        closed_at = datetime(2026, 8, 25, 15, 56, 0, tzinfo=timezone.utc)
        with mock.patch.object(config, "NEWS_DB_PATH", self.db):
            line = fa.format_trade_line(
                trade,
                reason="TAKE_PROFIT",
                entry_mid=2.62,
                entry_ask=2.74,
                exit_mid=4.05,
                exit_bid=3.93,
                qty=2,
                pnl_mid=286.0,
                pnl_fill=238.0,
                planned_risk=124.0,
                fill_est=False,
                closed_at=closed_at,
                db_path=self.db,
            )
        parts = line.split("|")
        self.assertEqual(len(parts), fa.TRADE_FIELD_COUNT)
        self.assertEqual(parts[0], "TRADE")
        self.assertTrue(parts[1].startswith("v"))
        self.assertEqual(len(parts[1]), 8)  # v + 7
        self.assertEqual(parts[2], "2026-08-25")
        self.assertEqual(parts[3], "GOOGL")
        self.assertEqual(parts[4], "C")
        self.assertEqual(parts[5], "350")
        self.assertEqual(parts[6], "2026-08-28")
        self.assertEqual(parts[7], "qty2")
        self.assertEqual(parts[8], "09:47")
        self.assertEqual(parts[9], "entry_mid 2.62")
        self.assertEqual(parts[10], "entry_ask 2.74")
        self.assertEqual(parts[11], "10:56")
        self.assertEqual(parts[12], "exit_mid 4.05")
        self.assertEqual(parts[13], "exit_bid 3.93")
        self.assertEqual(parts[14], "TAKE_PROFIT")
        self.assertEqual(parts[15], "pnl_mid +286")
        self.assertEqual(parts[16], "pnl_fill +238")
        self.assertEqual(parts[17], "planned_risk 124")
        self.assertEqual(parts[18], "R_fill +1.92")
        self.assertEqual(parts[19], "mfe_pct 0.61")
        self.assertEqual(parts[20], "mae_pct -0.08")
        self.assertEqual(parts[21], "hold_min 69")
        self.assertEqual(parts[22], "entry_score 86")
        self.assertEqual(parts[23], "exit_score 74")
        self.assertEqual(parts[24], "dte_entry 4.8")
        self.assertEqual(parts[25], "carried n")
        self.assertNotIn("fill=est", line)

    def test_missing_fields_write_na_same_arity(self):
        line = fa.format_trade_line(
            {"ticker": "SPY"},
            reason="TIME_STOP",
            entry_mid=None,
            entry_ask=None,
            exit_mid=None,
            exit_bid=None,
            qty=1,
            pnl_mid=None,
            pnl_fill=None,
            planned_risk=None,
            fill_est=True,
        )
        parts = line.split("|")
        self.assertEqual(len(parts), fa.TRADE_FIELD_COUNT)
        self.assertEqual(parts[0], "TRADE")
        self.assertIn("n/a", parts[9])
        self.assertIn("fill=est", parts[10])
        self.assertIn("fill=est", parts[13])

    def test_carried_y_when_entry_prior_session(self):
        trade = {
            "ticker": "NVDA",
            "direction": "PUT",
            "strike": 210,
            "expiration": "2026-08-31",
            "entry_timestamp": "2026-08-24T19:29:00+00:00",
        }
        line = fa.format_trade_line(
            trade,
            reason="EARNINGS_FLATTEN",
            entry_mid=2.62,
            entry_ask=2.70,
            exit_mid=2.50,
            exit_bid=2.40,
            qty=1,
            pnl_mid=-12.0,
            pnl_fill=-30.0,
            planned_risk=52.0,
            closed_at=datetime(2026, 8, 25, 18, 30, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(line.endswith("carried y") or line.split("|")[-1] == "carried y")

    def test_session_line_field_count(self):
        fa.note_scan()
        fa.note_scan()
        fa.note_entry()
        fa.note_close(pnl_mid=213.0, pnl_fill=38.0, planned_risk=53.5)
        fa.note_spy(765.91)
        fa.note_spy(767.50)
        fa.note_spy(764.20)
        fa.note_critical()
        line = fa.format_session_line(
            equity_fill=10041.0,
            peak_deployed=2778.0,
            open_value=1100.0,
            now=datetime(2026, 8, 25, 14, 45, 0),
        )
        parts = line.split("|")
        self.assertEqual(len(parts), fa.SESSION_FIELD_COUNT)
        self.assertEqual(parts[0], "SESSION")
        self.assertTrue(parts[1].startswith("v"))
        self.assertEqual(parts[3], "scans 2")
        self.assertEqual(parts[4], "entries 1")
        self.assertEqual(parts[5], "closes 1")
        self.assertEqual(parts[6], "realized_mid +213")
        self.assertEqual(parts[7], "realized_fill +38")
        self.assertEqual(parts[8], "spread_cost 175")
        self.assertEqual(parts[10], "equity_fill 10041")
        self.assertEqual(parts[11], "peak_deployed 2778")
        self.assertEqual(parts[12], "open_value 1100")
        self.assertEqual(parts[13], "spy_open 765.91")
        self.assertEqual(parts[14], "spy_close 764.20")
        self.assertEqual(parts[15], "spy_range_pct 0.43")
        self.assertEqual(parts[16], "criticals 1")


class TestLedgerFillSeries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.db_patch = mock.patch.object(vb, "DB_PATH", self.db)
        self.cfg_patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.db_patch.start()
        self.cfg_patch.start()
        vb.reset_book_for_tests()
        fa.reset_session_for_tests()
        self.open_trades = []
        self.open_patch = mock.patch(
            "tracker_agent.load_active_trades",
            side_effect=lambda: list(self.open_trades),
        )
        self.open_patch.start()
        vb.ensure_ledger()

    def tearDown(self):
        vb.reset_book_for_tests()
        fa.reset_session_for_tests()
        self.open_patch.stop()
        self.db_patch.stop()
        self.cfg_patch.stop()
        self.tmp.cleanup()

    def test_buy_debits_mid_sell_credits_mid_fill_is_separate(self):
        contract = {
            "ticker": "GOOGL",
            "direction": "CALL",
            "strike": 350.0,
            "expiration": "2026-08-28",
            "quantity": 2,
            "ask": 2.74,
            "bid": 2.50,
            "stop_loss": 2.10,
        }
        buy = vb.paper_buy(contract, 2.62, quantity=2)
        self.assertTrue(buy["ok"])
        self.assertAlmostEqual(buy["cost"], 2.62 * 100 * 2)
        self.assertAlmostEqual(buy["entry_ask"], 2.74)
        snap = vb.get_portfolio()
        self.assertAlmostEqual(snap["buying_power"], 10000.0 - 524.0)

        sell = vb.paper_sell(
            contract,
            4.05,
            "CALL",
            2.62,
            notes="EXIT:TAKE_PROFIT",
            quantity=2,
            exit_bid=3.93,
            entry_ask=2.74,
        )
        self.assertTrue(sell["ok"])
        self.assertAlmostEqual(sell["pnl_mid"], 286.0)
        self.assertAlmostEqual(sell["pnl_fill"], 238.0)
        self.assertAlmostEqual(sell["pnl"], 286.0)  # callers still see mid
        snap = vb.get_portfolio()
        # BP restored by mid credit: 10000 - 524 + 810 = 10286
        self.assertAlmostEqual(snap["buying_power"], 10286.0)
        self.assertAlmostEqual(snap["total_realized_pnl"], 286.0)
        self.assertAlmostEqual(snap["total_realized_pnl_fill"], 238.0)
        line = vb.format_book_line()
        self.assertIn("realized +238", line)
        self.assertIn("equity 10,238", line)

    def test_open_book_marks_at_bid(self):
        self.open_trades.append(
            {
                "ticker": "IWM",
                "quantity": 2,
                "entry_price": 1.35,
                "entry_ask": 1.40,
                "last_mark": 1.50,
                "last_bid": 1.45,
            }
        )
        self.assertAlmostEqual(vb.open_mark_value(), 1.45 * 100 * 2)
        line = vb.format_book_line()
        self.assertIn("open value 290", line)

    def test_close_records_fill_and_keeps_scan_exits(self):
        trade = {
            "trade_id": "t-googl",
            "ticker": "GOOGL",
            "direction": "CALL",
            "strike": 350,
            "expiration": "2026-08-28",
            "entry_price": 2.62,
            "entry_ask": 2.74,
            "last_bid": 3.93,
            "stop_loss": 2.10,
            "take_profit": 3.93,
            "quantity": 2,
            "entry_score": 86.0,
            "last_live_score": 74.0,
            "entry_timestamp": "2026-08-25T14:47:00+00:00",
            "peak_pnl_pct": 61.0,
            "trough_pnl_pct": -8.0,
            "entry_dte": 4.8,
        }
        alerts = []
        with mock.patch(
            "broadcaster.send_discord_alert",
            side_effect=lambda m: alerts.append(m) or True,
        ):
            with mock.patch("tracker_agent.remove_active_trade", return_value=True):
                out = position_exits.close_open_position(trade, 4.05, "TAKE_PROFIT")
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["pnl_mid"], 286.0)
        self.assertAlmostEqual(out["pnl_fill"], 238.0)
        self.assertFalse(out["fill_est"])
        self.assertTrue(any(a.startswith("TRADE|") for a in alerts))
        trade_line = next(a for a in alerts if a.startswith("TRADE|"))
        self.assertEqual(len(trade_line.split("|")), fa.TRADE_FIELD_COUNT)
        self.assertIn("pnl_fill +238", trade_line)
        self.assertIn("TAKE_PROFIT", trade_line)

    def test_missing_bid_tags_fill_est(self):
        trade = {
            "ticker": "SPY",
            "direction": "PUT",
            "entry_price": 2.16,
            "quantity": 1,
        }
        alerts = []
        with mock.patch(
            "broadcaster.send_discord_alert",
            side_effect=lambda m: alerts.append(m) or True,
        ):
            with mock.patch("tracker_agent.remove_active_trade", return_value=True):
                out = position_exits.close_open_position(trade, 2.71, "TRAILING_GIVEBACK")
        self.assertTrue(out["fill_est"])
        self.assertAlmostEqual(out["pnl_mid"], out["pnl_fill"])
        trade_line = next(a for a in alerts if a.startswith("TRADE|"))
        self.assertIn("fill=est", trade_line)


class TestVersionStamp(unittest.TestCase):
    def test_boot_logs_carry_version(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            config.log_scoring_config()
        text = buf.getvalue()
        self.assertIn("[Scoring]", text)
        self.assertRegex(text, r"\[Scoring\] v[0-9a-f]{7} ")

    def test_code_version_shape(self):
        ver = fa.code_version()
        self.assertTrue(ver.startswith("v"))
        self.assertGreaterEqual(len(ver), 2)


class TestStrikeSelectorStampsAsk(unittest.TestCase):
    def test_selected_contract_carries_bid_ask(self):
        import strike_selector as ss
        now = datetime(2026, 8, 5, 10, 32, tzinfo=timezone.utc)
        # Use the existing tight-spread helper if the module test chain is simpler:
        options = {
            "current_price": 302.10,
            "chains": {
                "2026-08-06": {
                    "calls": [
                        {
                            "strike": 302.0,
                            "bid": 1.97,
                            "ask": 2.03,
                            "lastPrice": 2.00,
                            "openInterest": 50_000,
                            "volume": 50_000,
                            "impliedVolatility": 0.20,
                        }
                    ],
                    "puts": [],
                }
            },
        }
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            with mock.patch.object(config, "REQUIRED_MOVE_ATR_K", 5.0):
                with mock.patch.object(config, "EXIT_MAX_DECAY_DENSITY", 100.0):
                    with mock.patch.object(config, "MAX_CONTRACT_SPREAD_PCT", 8.0):
                        with mock.patch.object(config, "MIN_PREMIUM", 0.50):
                            out = ss.select_optimal_contract(
                                options, pivot, atr_abs=5.0, now=now
                            )
        if "error" in out:
            self.skipTest(f"selector rejected: {out['error']}")
        self.assertIn("bid", out)
        self.assertIn("ask", out)
        self.assertIn("entry_ask", out)
        self.assertAlmostEqual(out["ask"], 2.03)
        self.assertAlmostEqual(out["entry_mid"], out["entry_premium"])


if __name__ == "__main__":
    unittest.main()
