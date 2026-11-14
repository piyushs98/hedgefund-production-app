"""Unpriced positions are flat by 14:55 CT. Estimated fills count in fill P&L."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import config
import position_exits
import virtual_broker


def _trade(**over):
    base = {
        "trade_id": "t-force",
        "ticker": "SPY",
        "direction": "CALL",
        "strike": 600.0,
        "expiration": "2026-11-20",
        "entry_price": 2.00,
        "entry_premium": 2.00,
        "entry_mid": 2.00,
        "entry_ask": 2.10,
        "stop_loss": 1.60,
        "take_profit": 3.00,
        "entry_timestamp": "2026-11-16T15:00:00+00:00",
        "delta": 0.50,
        "stop_entry_spot": 100.0,
        "stop_spot": 90.0,
        "target_spot": 120.0,
        "quantity": 1,
    }
    base.update(over)
    return base


def _dark(spot=None):
    body = {"error": "quote_failed", "chains": {}}
    if spot is not None:
        body["current_price"] = spot
    return {"SPY": {"options_dict": body, "card": None}}


class TestEodForceClose(unittest.TestCase):
    def setUp(self):
        position_exits.reset_eod_flags_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.db_patch = mock.patch.object(virtual_broker, "DB_PATH", self.db)
        self.cfg_patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.db_patch.start()
        self.cfg_patch.start()
        virtual_broker.reset_book_for_tests()
        virtual_broker.ensure_ledger()
        self.alerts = []

    def tearDown(self):
        position_exits.reset_eod_flags_for_tests()
        virtual_broker.reset_book_for_tests()
        self.db_patch.stop()
        self.cfg_patch.stop()
        self.tmp.cleanup()

    def _fill(self) -> float:
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT total_realized_pnl_fill FROM portfolio_ledger WHERE id=1"
            ).fetchone()
        return float(row[0] or 0.0)

    def _pass(self, book, when, scored):
        def send(msg, critical=False):
            self.alerts.append((msg, critical))
            return True

        def remove(trade):
            book[:] = [
                row for row in book
                if row.get("trade_id") != trade.get("trade_id")
            ]
            return True

        with mock.patch("tracker_agent.load_active_trades", side_effect=lambda: list(book)), \
                mock.patch("tracker_agent.remove_active_trade", side_effect=remove), \
                mock.patch("tracker_agent.save_active_trade", return_value=True), \
                mock.patch("broadcaster.send_discord_alert", side_effect=send), \
                mock.patch("data_engineer.fetch_spot", return_value=None), \
                mock.patch("earnings_blackout.should_flatten_trade", return_value=False):
            summary = position_exits.run_scan_exits(
                list(book),
                scored,
                session_date=when.date(),
                now_cdt=when,
            )
        return summary

    def _forced(self):
        return [msg for msg, critical in self.alerts if critical and "EOD FORCED CLOSE" in msg]

    def test_quote_dark_with_spot_closes_at_1455_on_delta(self):
        book = [_trade()]
        scored = _dark(100.50)
        early = self._pass(book, datetime(2026, 11, 16, 14, 40), scored)
        self.assertEqual(len(book), 1)
        self.assertEqual(early["closed"], [])
        self.assertFalse(self._forced())
        self.assertFalse(position_exits.eod_already_done(datetime(2026, 11, 16).date()))

        mid = self._pass(book, datetime(2026, 11, 16, 14, 45), scored)
        self.assertEqual(len(book), 1)
        self.assertFalse(mid.get("eod_triggered"))
        self.assertFalse(position_exits.eod_already_done(datetime(2026, 11, 16).date()))

        before = self._fill()
        done = self._pass(book, datetime(2026, 11, 16, 14, 55), scored)
        self.assertEqual(book, [])
        self.assertEqual(done["open_after"], 0)
        self.assertTrue(done.get("eod_triggered"))
        self.assertTrue(position_exits.eod_already_done(datetime(2026, 11, 16).date()))
        closed = done["closed"][0]
        self.assertEqual(closed["reason"], "EOD_FLATTEN_ESTIMATED")
        self.assertAlmostEqual(closed["exit_price"], 2.025)
        self.assertTrue(closed["fill_est"])
        self.assertAlmostEqual(closed["pnl_fill"], -7.5)
        self.assertAlmostEqual(self._fill() - before, -7.5)
        forced = self._forced()
        self.assertEqual(len(forced), 1)
        self.assertIn("SPY", forced[0])
        self.assertIn("rule delta x 0.90", forced[0])
        self.assertIn("price 2.0250", forced[0])

    def test_quote_and_spot_dark_closes_at_floor(self):
        book = [_trade(trade_id="floor")]
        scored = _dark()
        self._pass(book, datetime(2026, 11, 16, 14, 40), scored)
        self.assertEqual(len(book), 1)
        before = self._fill()
        done = self._pass(book, datetime(2026, 11, 16, 14, 55), scored)
        self.assertEqual(book, [])
        self.assertEqual(done["open_after"], 0)
        closed = done["closed"][0]
        self.assertEqual(closed["reason"], "EOD_FLATTEN_ESTIMATED")
        self.assertAlmostEqual(closed["exit_price"], 0.01)
        self.assertTrue(closed["fill_est"])
        self.assertAlmostEqual(closed["pnl_fill"], (0.01 - 2.10) * 100)
        self.assertAlmostEqual(self._fill() - before, closed["pnl_fill"])
        forced = self._forced()
        self.assertEqual(len(forced), 1)
        self.assertIn("rule floor", forced[0])
        self.assertTrue(any(critical for _, critical in self.alerts if "EOD FORCED CLOSE" in _))

    def test_boot_without_quotes_is_flat_at_the_deadline(self):
        position_exits._boot_flatten_open = True
        position_exits._boot_flatten_since = datetime(2026, 11, 16, 9, 0)
        book = [_trade(trade_id="boot")]
        scored = _dark()
        self._pass(book, datetime(2026, 11, 16, 9, 10), scored)
        self.assertEqual(len(book), 1)
        self.assertFalse(self._forced())
        self._pass(book, datetime(2026, 11, 16, 9, 31), scored)
        self.assertEqual(len(book), 1)
        self.assertFalse(self._forced())
        done = self._pass(book, datetime(2026, 11, 16, 14, 55), scored)
        self.assertEqual(book, [])
        self.assertEqual(done["open_after"], 0)
        self.assertEqual(done["closed"][0]["reason"], "EOD_FLATTEN_ESTIMATED")
        self.assertTrue(done["closed"][0]["fill_est"])
        self.assertTrue(self._forced())
        self.assertIn("rule floor", self._forced()[0])

    def test_boot_last_bid_closes_after_30_minutes(self):
        position_exits._boot_flatten_open = True
        position_exits._boot_flatten_since = datetime(2026, 11, 16, 9, 0)
        book = [_trade(
            trade_id="boot-bid",
            last_bid=1.25,
            last_bid_at="2026-11-16T15:10:00+00:00",
        )]
        scored = _dark()
        self._pass(book, datetime(2026, 11, 16, 9, 20), scored)
        self.assertEqual(len(book), 1)
        self.assertFalse(self._forced())
        before = self._fill()
        done = self._pass(book, datetime(2026, 11, 16, 9, 30), scored)
        self.assertEqual(book, [])
        closed = done["closed"][0]
        self.assertEqual(closed["reason"], "BOOT_FLATTEN")
        self.assertAlmostEqual(closed["exit_price"], 1.25)
        self.assertTrue(closed["fill_est"])
        self.assertAlmostEqual(closed["pnl_fill"], (1.25 - 2.10) * 100)
        self.assertAlmostEqual(self._fill() - before, closed["pnl_fill"])
        forced = self._forced()
        self.assertEqual(len(forced), 1)
        self.assertIn("rule last good bid", forced[0])
        self.assertFalse(position_exits._boot_flatten_open)

    def test_early_close_deadline_is_1155(self):
        book = [_trade(trade_id="early", expiration="2026-12-04")]
        scored = _dark(100.50)
        self._pass(book, datetime(2026, 11, 27, 11, 40), scored)
        self.assertEqual(len(book), 1)
        self._pass(book, datetime(2026, 11, 27, 11, 50), scored)
        self.assertEqual(len(book), 1)
        self.assertFalse(position_exits.eod_already_done(datetime(2026, 11, 27).date()))
        done = self._pass(book, datetime(2026, 11, 27, 11, 55), scored)
        self.assertEqual(book, [])
        self.assertEqual(done["open_after"], 0)
        self.assertTrue(done.get("eod_triggered"))
        self.assertEqual(done["closed"][0]["reason"], "EOD_FLATTEN_ESTIMATED")
        self.assertAlmostEqual(done["closed"][0]["exit_price"], 2.025)
        self.assertTrue(done["closed"][0]["fill_est"])
        self.assertIn("rule delta x 0.90", self._forced()[0])
        self.assertFalse(position_exits.is_eod_force_deadline(datetime(2026, 11, 27, 11, 54)))
        self.assertTrue(position_exits.is_eod_force_deadline(datetime(2026, 11, 27, 11, 55)))

    def test_expired_without_official_close_is_flat_at_the_deadline(self):
        book = [_trade(trade_id="expired", expiration="2026-11-13")]
        before = self._fill()

        def send(msg, critical=False):
            self.alerts.append((msg, critical))
            return True

        def remove(trade):
            book[:] = [
                row for row in book
                if row.get("trade_id") != trade.get("trade_id")
            ]
            return True

        with mock.patch("position_exits.settlement_for_expired", return_value=(True, None)), \
                mock.patch("tracker_agent.load_active_trades", side_effect=lambda: list(book)), \
                mock.patch("tracker_agent.remove_active_trade", side_effect=remove), \
                mock.patch("tracker_agent.save_active_trade", return_value=True), \
                mock.patch("broadcaster.send_discord_alert", side_effect=send), \
                mock.patch("data_engineer.fetch_spot", return_value=None):
            waiting = position_exits.run_scan_exits(
                list(book),
                _dark(),
                session_date=datetime(2026, 11, 16).date(),
                now_cdt=datetime(2026, 11, 16, 14, 50),
            )
            self.assertEqual(len(book), 1)
            self.assertEqual(waiting.get("expired_waiting"), [{"ticker": "SPY", "reason": "EXPIRED"}])
            done = position_exits.run_scan_exits(
                list(book),
                _dark(),
                session_date=datetime(2026, 11, 16).date(),
                now_cdt=datetime(2026, 11, 16, 14, 55),
            )
        self.assertEqual(book, [])
        self.assertEqual(done["open_after"], 0)
        self.assertEqual(done["closed"][0]["reason"], "EOD_FLATTEN_ESTIMATED")
        self.assertTrue(done["closed"][0]["fill_est"])
        self.assertAlmostEqual(self._fill() - before, done["closed"][0]["pnl_fill"])
        self.assertTrue(self._forced())

    def test_retry_sleep_is_60s_only_inside_the_flatten_window(self):
        self.assertIsNone(
            position_exits.eod_quote_retry_sleep_s(datetime(2026, 11, 16, 10, 0))
        )
        self.assertEqual(
            position_exits.eod_quote_retry_sleep_s(datetime(2026, 11, 16, 14, 46)),
            60,
        )
        self.assertAlmostEqual(
            position_exits.eod_quote_retry_sleep_s(datetime(2026, 11, 16, 14, 54, 30)),
            30,
        )
        self.assertEqual(
            position_exits.eod_quote_retry_sleep_s(datetime(2026, 11, 16, 14, 55)),
            60,
        )
        self.assertIsNone(
            position_exits.eod_quote_retry_sleep_s(
                datetime(2026, 11, 16, 14, 50), latched=True
            )
        )
        self.assertAlmostEqual(
            position_exits.eod_quote_retry_sleep_s(datetime(2026, 11, 27, 11, 54, 20)),
            40,
        )

    def test_price_rules_prefer_bid_then_delta_then_intrinsic(self):
        now = position_exits._chicago_now(datetime(2026, 11, 16, 14, 55))
        recent = _trade(
            last_bid=1.80,
            last_bid_at="2026-11-16T20:45:00+00:00",
        )
        price, rule, _age = position_exits.eod_estimated_exit(recent, now, 100.50)
        self.assertEqual(rule, "last good bid")
        self.assertAlmostEqual(price, 1.80)
        stale = _trade(
            last_bid=1.80,
            last_bid_at="2026-11-16T19:00:00+00:00",
        )
        price, rule, _age = position_exits.eod_estimated_exit(stale, now, 100.50)
        self.assertEqual(rule, "delta x 0.90")
        self.assertAlmostEqual(price, 2.025)
        bare = _trade()
        bare.pop("stop_entry_spot")
        price, rule, _age = position_exits.eod_estimated_exit(bare, now, 100.50)
        self.assertEqual(rule, "intrinsic")
        self.assertAlmostEqual(price, 0.01)
        price, rule, age = position_exits.eod_estimated_exit(_trade(), now, None)
        self.assertEqual((price, rule, age), (0.01, "floor", "none"))


class TestNewsPlaceholderScore(unittest.TestCase):
    def test_empty_book_matches_the_nov12_placeholder_score(self):
        import master_bot
        import scoring_engine

        with mock.patch.object(master_bot, "get_historical_context", return_value=""), \
                mock.patch.object(master_bot.yf, "Ticker") as ticker:
            text = master_bot.ensure_news_context("MSFT")
        ticker.assert_not_called()
        self.assertEqual(text, "No recent headlines or historical news found.")
        _s, metrics, _reasons = scoring_engine.score_sentiment(
            text, "", 1.3255128, direction_sign=-1.0
        )
        # logs/bot-2026-11-12.log MSFT 08:30 CT: S=-4.69, direction PUT,
        # after the Yahoo news call failed and this sentence was the input.
        self.assertEqual(metrics["S"], -4.69)
        self.assertEqual(metrics["headline_count"], 1)
