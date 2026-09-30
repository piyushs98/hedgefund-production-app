"""Position and session record integrity. No sizing or score changes."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import config
import fill_accounting
import llm_chain
import master_bot
import position_exits
import virtual_broker as vb


class TestPaperCloseIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.db_patch = mock.patch.object(vb, "DB_PATH", self.db)
        self.cfg_patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.db_patch.start()
        self.cfg_patch.start()
        vb.reset_book_for_tests()
        fill_accounting.reset_session_for_tests()
        self.open_patch = mock.patch(
            "tracker_agent.load_active_trades", return_value=[]
        )
        self.open_patch.start()
        vb.ensure_ledger()

    def tearDown(self):
        vb.reset_book_for_tests()
        fill_accounting.reset_session_for_tests()
        self.open_patch.stop()
        self.db_patch.stop()
        self.cfg_patch.stop()
        self.tmp.cleanup()

    def _lot(self, trade_id="lot-1", ticker="IWM"):
        return {
            "trade_id": trade_id,
            "ticker": ticker,
            "direction": "CALL",
            "strike": 302.0,
            "expiration": "2026-08-12",
            "quantity": 2,
            "entry_premium": 1.50,
        }

    def test_second_sell_of_same_trade_does_not_credit_again(self):
        contract = self._lot()
        buy = vb.paper_buy(contract, 1.50, quantity=2)
        self.assertTrue(buy["ok"])
        first = vb.paper_sell(
            contract, 1.20, "CALL", 1.50, notes="EXIT:STOP_LOSS", quantity=2
        )
        self.assertTrue(first["ok"])
        self.assertFalse(first.get("duplicate"))
        bp = vb.get_portfolio()["buying_power"]
        realized = vb.get_portfolio()["total_realized_pnl"]
        second = vb.paper_sell(
            contract, 1.20, "CALL", 1.50, notes="EXIT:STOP_LOSS", quantity=2
        )
        self.assertTrue(second["ok"])
        self.assertTrue(second.get("duplicate"))
        snap = vb.get_portfolio()
        self.assertAlmostEqual(snap["buying_power"], bp)
        self.assertAlmostEqual(snap["total_realized_pnl"], realized)
        with vb._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM trade_history WHERE notes LIKE 'EXIT:%'"
            ).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_distinct_trade_ids_both_credit(self):
        a = self._lot("lot-a")
        b = self._lot("lot-b")
        vb.paper_buy(a, 1.50, quantity=1)
        vb.paper_buy(b, 1.50, quantity=1)
        sa = vb.paper_sell(a, 1.00, "CALL", 1.50, notes="EXIT:STOP_LOSS", quantity=1)
        sb = vb.paper_sell(b, 1.00, "CALL", 1.50, notes="EXIT:STOP_LOSS", quantity=1)
        self.assertFalse(sa.get("duplicate"))
        self.assertFalse(sb.get("duplicate"))
        with vb._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM trade_history WHERE notes LIKE 'EXIT:%'"
            ).fetchone()["n"]
        self.assertEqual(n, 2)

    def test_void_restores_debit_without_an_exit_row(self):
        contract = self._lot("void-1")
        buy = vb.paper_buy(contract, 1.50, quantity=2)
        self.assertTrue(buy["ok"])
        self.assertAlmostEqual(buy["buying_power"], 10000.0 - 300.0)
        undone = vb.void_unpersisted_buy(contract, 1.50, quantity=2)
        self.assertTrue(undone["ok"])
        snap = vb.get_portfolio()
        self.assertAlmostEqual(snap["buying_power"], 10000.0)
        self.assertAlmostEqual(snap["total_realized_pnl"], 0.0)
        self.assertAlmostEqual(float(vb._book.get("open_cost") or 0.0), 0.0)
        with vb._connect() as conn:
            exits = conn.execute(
                "SELECT COUNT(*) AS n FROM trade_history WHERE notes LIKE 'EXIT:%'"
            ).fetchone()["n"]
            opens = conn.execute(
                "SELECT COUNT(*) AS n FROM trade_history WHERE notes = 'PAPER_BUY_OPEN'"
            ).fetchone()["n"]
        self.assertEqual(exits, 0)
        self.assertEqual(opens, 0)
        self.assertEqual(fill_accounting._session.get("entries"), 0)


class TestFailedCloseRetainsPosition(unittest.TestCase):
    def _trade(self):
        return {
            "trade_id": "keep-1",
            "ticker": "NVDA",
            "direction": "CALL",
            "entry_price": 2.0,
            "strike": 100,
            "expiration": "2026-08-12",
        }

    def test_failed_sell_does_not_remove_or_emit(self):
        with mock.patch("virtual_broker.paper_sell", return_value={"ok": False, "error": "db"}):
            with mock.patch("tracker_agent.remove_active_trade") as rem:
                with mock.patch("broadcaster.send_discord_alert") as disc:
                    out = position_exits.close_open_position(
                        self._trade(), 1.6, "STOP_LOSS"
                    )
        self.assertFalse(out["ok"])
        rem.assert_not_called()
        disc.assert_not_called()
        self.assertNotIn("trade_line", out)

    def test_sell_exception_retains_position(self):
        with mock.patch("virtual_broker.paper_sell", side_effect=RuntimeError("locked")):
            with mock.patch("tracker_agent.remove_active_trade") as rem:
                out = position_exits.close_open_position(
                    self._trade(), 1.6, "STOP_LOSS"
                )
        self.assertFalse(out["ok"])
        self.assertIn("locked", out["sell_error"])
        rem.assert_not_called()

    def test_remove_failure_does_not_emit_trade(self):
        with mock.patch(
            "virtual_broker.paper_sell",
            return_value={"ok": True, "pnl": -40.0, "pnl_mid": -40.0, "pnl_fill": -40.0},
        ):
            with mock.patch(
                "tracker_agent.remove_active_trade", side_effect=RuntimeError("disk")
            ):
                with mock.patch("broadcaster.send_discord_alert") as disc:
                    out = position_exits.close_open_position(
                        self._trade(), 1.6, "STOP_LOSS"
                    )
        self.assertTrue(out["ok"])
        self.assertFalse(out["removed"])
        disc.assert_not_called()
        self.assertNotIn("trade_line", out)


class TestExactContractAndUnresolved(unittest.TestCase):
    def test_other_expiry_same_strike_is_not_the_mark(self):
        trade = {
            "ticker": "IWM",
            "direction": "CALL",
            "strike": 302.0,
            "expiration": "2026-08-05",
        }
        options = {
            "current_price": 302.1,
            "chains": {
                "2026-08-12": {
                    "calls": [{"strike": 302.0, "bid": 0.90, "ask": 1.10, "lastPrice": 1.0}],
                    "puts": [],
                }
            },
        }
        info = position_exits.lookup_option_mark(trade, options)
        self.assertFalse(info["found"])
        self.assertIsNone(info["mark"])
        self.assertAlmostEqual(info["spot"], 302.1)

    def test_forced_flatten_without_mark_is_unpriced(self):
        trade = {
            "ticker": "IWM",
            "direction": "CALL",
            "strike": 302.0,
            "expiration": "2026-08-06",
            "entry_price": 1.42,
            "stop_loss": 1.14,
            "take_profit": 2.13,
        }
        reason, px = position_exits.evaluate_exit_reason_for_mark(
            trade,
            None,
            sess=date(2026, 8, 5),
            now=datetime(2026, 8, 5, 14, 50, 0),
            include_time_stop=False,
            do_eod=True,
        )
        self.assertEqual(reason, "EOD_FLATTEN")
        self.assertIsNone(px)

    def test_scan_does_not_close_unpriced_flatten(self):
        position_exits.reset_eod_flags_for_tests()
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "news.db")
        trade = {
            "trade_id": "short",
            "ticker": "IWM",
            "direction": "CALL",
            "strike": 302.0,
            "expiration": "2026-08-06",
            "entry_price": 1.42,
            "entry_premium": 1.42,
            "stop_loss": 1.14,
            "take_profit": 2.13,
            "entry_timestamp": "2026-08-05T15:00:00Z",
        }
        scored = {
            "IWM": {
                "options_dict": {
                    "current_price": 302.1,
                    "chains": {
                        "2026-08-12": {
                            "calls": [
                                {"strike": 302.0, "bid": 0.4, "ask": 0.6, "lastPrice": 0.5}
                            ]
                        }
                    },
                },
                "card": None,
            }
        }
        with mock.patch.object(vb, "DB_PATH", db):
            with mock.patch.object(config, "NEWS_DB_PATH", db):
                vb.reset_book_for_tests()
                vb.ensure_ledger()
                with mock.patch("position_exits.close_open_position") as close:
                    with mock.patch("tracker_agent.save_active_trade", return_value=True):
                        with mock.patch("tracker_agent.load_active_trades", return_value=[trade]):
                            with mock.patch("broadcaster.send_discord_alert", return_value=True):
                                with mock.patch.object(config, "CARRY_MIN_DTE", 2):
                                    summary = position_exits.run_scan_exits(
                                        [trade],
                                        scored,
                                        session_date=date(2026, 8, 5),
                                        force_eod=True,
                                        now_cdt=datetime(2026, 8, 5, 14, 50, 0),
                                    )
        tmp.cleanup()
        close.assert_not_called()
        self.assertEqual(summary["unresolved"][0]["reason"], "EOD_FLATTEN")
        self.assertFalse(summary.get("eod_triggered"))
        self.assertFalse(position_exits.eod_already_done(date(2026, 8, 5)))
        position_exits.reset_eod_flags_for_tests()


class TestEodDeliveryAndFlatBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        position_exits.reset_eod_flags_for_tests()
        self.db_patch = mock.patch.object(vb, "DB_PATH", self.db)
        self.cfg_patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.db_patch.start()
        self.cfg_patch.start()
        vb.reset_book_for_tests()
        vb.ensure_ledger()

    def tearDown(self):
        position_exits.reset_eod_flags_for_tests()
        vb.reset_book_for_tests()
        self.db_patch.stop()
        self.cfg_patch.stop()
        self.tmp.cleanup()

    def test_failed_discord_does_not_latch_the_day(self):
        when = datetime(2026, 8, 20, 14, 45, 0)
        with mock.patch("tracker_agent.load_active_trades", return_value=[]):
            with mock.patch("broadcaster.send_discord_alert", return_value=False):
                missed = position_exits.maybe_emit_eod_book(when)
            self.assertIsNone(missed)
            self.assertFalse(position_exits.eod_book_already_done(date(2026, 8, 20)))
            with mock.patch("broadcaster.send_discord_alert", return_value=True):
                sent = position_exits.maybe_emit_eod_book(when)
                again = position_exits.maybe_emit_eod_book(
                    datetime(2026, 8, 20, 14, 50, 0)
                )
        self.assertIsNotNone(sent)
        self.assertIsNone(again)

    def test_flat_exit_pass_asks_for_the_book_line(self):
        from midday_delta import run_exit_only_pass

        breaker = mock.Mock()
        breaker.is_open.return_value = False
        with mock.patch("tracker_agent.load_active_trades", return_value=[]):
            with mock.patch("position_exits.maybe_emit_eod_book", return_value=None) as emit:
                run_exit_only_pass(breaker)
        emit.assert_called()

    def test_breaker_open_flat_book_asks_for_the_book_line(self):
        from midday_delta import run_exit_only_pass

        breaker = mock.Mock()
        breaker.is_open.return_value = True
        with mock.patch("tracker_agent.load_active_trades", return_value=[]):
            with mock.patch("position_exits.maybe_emit_eod_book", return_value=None) as emit:
                run_exit_only_pass(breaker)
        emit.assert_called()


class TestDailyCapRolls(unittest.TestCase):
    def test_next_chicago_session_clears_entries_and_keeps_the_open_lot(self):
        from signal_gate import GateConfig, Observation, SignalGate

        gate = SignalGate(
            GateConfig(
                persist_cycles=1,
                max_entries_per_ticker=1,
                reentry_cooldown_minutes=0,
                post_exit_cooldown_minutes=0,
                max_concurrent=5,
            )
        )
        t0 = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)
        first = gate.process_scan([Observation("IWM", 80, "C", "EXECUTE")], t0)
        self.assertTrue(first[0].admit)
        self.assertEqual(gate._st("IWM").entries_today, 1)
        self.assertTrue(gate._st("IWM").position_open)
        blocked = gate.process_scan([Observation("IWM", 90, "C", "EXECUTE")], t0)
        self.assertFalse(blocked[0].admit)

        nxt = t0 + timedelta(days=1)
        rolled = gate.process_scan([Observation("QQQ", 88, "C", "EXECUTE")], nxt)
        self.assertEqual(gate._st("IWM").entries_today, 0)
        self.assertTrue(gate._st("IWM").position_open)
        self.assertIn("IWM", gate._open)
        self.assertTrue(rolled[0].admit, rolled[0].reason)


class TestCommitRollsBackDebit(unittest.TestCase):
    def test_failed_persist_voids_and_rolls_back_admit(self):
        gate = mock.Mock()
        with mock.patch("master_bot.record_executed_trade", return_value=False):
            with mock.patch(
                "master_bot.virtual_broker.void_unpersisted_buy",
                return_value={"ok": True},
            ) as void:
                with mock.patch("master_bot.signal_gate.get_gate", return_value=gate):
                    ok = master_bot.commit_open_position(
                        "AAPL",
                        {"ticker": "AAPL", "entry_premium": 1.5, "quantity": 1},
                        entry_price=1.5,
                        quantity=1,
                    )
        self.assertFalse(ok)
        void.assert_called_once()
        gate.rollback_admit.assert_called_once_with("AAPL")


class TestLlmDeadlineDoesNotJoin(unittest.TestCase):
    def test_timeout_returns_before_the_worker_finishes(self):
        def _slow():
            time.sleep(0.4)
            return "late"

        started = time.monotonic()
        with self.assertRaises(llm_chain.LLMChainError) as ctx:
            llm_chain._run_with_deadline(_slow, timeout_s=0.05, step="test")
        elapsed = time.monotonic() - started
        self.assertTrue(ctx.exception.is_timeout)
        self.assertLess(elapsed, 0.25)


if __name__ == "__main__":
    unittest.main()
