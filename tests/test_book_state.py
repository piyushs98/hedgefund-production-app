"""Discord BOOK_STATE format, split, restore, and emit."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import book_state
import config
import fill_accounting as fa
import signal_gate
import virtual_broker as vb


def _trade(**over):
    base = {
        "trade_id": "550e8400-e29b-41d4-a716-446655440000",
        "ticker": "NVDA",
        "direction": "PUT",
        "strike": 210.0,
        "expiration": "2026-09-18",
        "quantity": 1,
        "entry_price": 6.90,
        "entry_premium": 6.90,
        "entry_mid": 6.90,
        "entry_ask": 6.98,
        "entry_spot": 209.33,
        "entry_score": 77.0,
        "entry_pivot": 208.50,
        "entry_dte": 3.0,
        "stop_loss": 5.52,
        "take_profit": 10.35,
        "trailing_stop": None,
        "peak_pnl_pct": 12.0,
        "trough_pnl_pct": -8.0,
        "entry_timestamp": "2026-09-15T14:29:00+00:00",
        "entry_time": "2026-09-15T14:29:00+00:00",
        "stop_spot": 210.4,
        "target_spot": 206.1,
        "stop_entry_spot": 209.33,
        "delta": 0.45,
        "underlying_delta": 0.45,
        "underlying_delta_est": False,
        "last_mark": 6.85,
        "last_bid": 6.80,
        "last_ask": 6.90,
        "last_mark_at": "2026-09-15T19:40:00+00:00",
        "last_live_score": 72.0,
        "last_spot": 209.10,
        "fill_est": False,
        "thesis_below_streak": 0,
        "mark_fail_streak": 0,
        "option_contract": {
            "direction": "PUT",
            "strike": 210.0,
            "expiration": "2026-09-18",
            "quantity": 1,
            "delta": 0.45,
            "spot": 209.33,
            "bid": 6.80,
            "ask": 6.90,
        },
    }
    base.update(over)
    return base


class TestFormatParse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.trades_path = Path(self.tmp.name) / "active_trades.json"
        self.trades_path.write_text("[]", encoding="utf-8")
        self.patches = [
            mock.patch.object(vb, "DB_PATH", self.db),
            mock.patch.object(config, "NEWS_DB_PATH", self.db),
            mock.patch("tracker_agent.NEWS_DB_PATH", self.db),
            mock.patch("tracker_agent.ACTIVE_TRADES_PATH", self.trades_path),
            mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "741584eabcdef"}, clear=False),
        ]
        for p in self.patches:
            p.start()
        fa.reset_session_for_tests()
        vb.reset_book_for_tests()
        book_state.reset_for_tests()
        vb.ensure_ledger()
        signal_gate.get_gate().reset_day()

    def tearDown(self):
        book_state.reset_for_tests()
        fa.reset_session_for_tests()
        vb.reset_book_for_tests()
        signal_gate.get_gate().reset_day()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_roundtrip_verbose_pos(self):
        from tracker_agent import replace_active_trades

        replace_active_trades([_trade()])
        vb.restore_ledger_snapshot(
            buying_power=7004.0, realized_mid=250.0, realized_fill=213.0
        )
        book_state.set_sessions_elapsed(3)
        line = book_state.format_book_state_line(
            now=datetime(2026, 9, 15, 15, 0, 0)
        )
        self.assertTrue(line.startswith("BOOK_STATE|"))
        self.assertIn("equity_fill", line)
        self.assertIn("bp 7004", line)
        self.assertIn("realized_cum +213", line)
        self.assertIn("sessions_elapsed 3", line)
        self.assertIn("positions 1", line)
        self.assertIn("POS~NVDA~P~210~2026-09-18~qty1~", line)
        self.assertIn("entry_mid 6.90", line)
        self.assertIn("entry_ask 6.98", line)
        self.assertIn("sl 5.52", line)
        self.assertIn("tp 10.35", line)
        self.assertIn("stop_spot 210.4", line)
        self.assertIn("thesis_below 0", line)
        snap = book_state.parse_book_state_line(line)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["sessions_elapsed"], 3)
        self.assertEqual(len(snap["trades"]), 1)
        t = snap["trades"][0]
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["direction"], "PUT")
        self.assertAlmostEqual(t["strike"], 210.0)
        self.assertEqual(t["expiration"], "2026-09-18")
        self.assertEqual(t["quantity"], 1)
        self.assertAlmostEqual(t["entry_mid"], 6.90)
        self.assertAlmostEqual(t["entry_ask"], 6.98)
        self.assertAlmostEqual(t["stop_loss"], 5.52)
        self.assertAlmostEqual(t["take_profit"], 10.35)
        self.assertAlmostEqual(t["peak_pnl_pct"], 12.0)
        self.assertAlmostEqual(t["trough_pnl_pct"], -8.0)
        self.assertAlmostEqual(t["stop_spot"], 210.4)
        self.assertAlmostEqual(t["target_spot"], 206.1)
        self.assertAlmostEqual(t["entry_score"], 77.0)
        self.assertEqual(t["thesis_below_streak"], 0)
        self.assertEqual(t["trade_id"], "550e8400-e29b-41d4-a716-446655440000")

    def test_split_at_four_verbose_positions(self):
        names = ["NVDA", "AAPL", "MSFT", "AMZN"]
        trades = [
            _trade(ticker=name, trade_id=f"id-{name.lower()}")
            for name in names
        ]
        line4 = book_state.format_book_state_line(
            trades=trades, now=datetime(2026, 9, 15, 15, 0, 0)
        )
        line3 = book_state.format_book_state_line(
            trades=trades[:3], now=datetime(2026, 9, 15, 15, 0, 0)
        )
        self.assertLessEqual(len(line3), 2000)
        self.assertGreater(len(line4), 2000)
        parts = book_state.split_book_state_messages(line4)
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].startswith("BOOK_STATE_1of"))
        self.assertLessEqual(max(len(p) for p in parts), 2000)

    def test_split_and_assemble_with_write_id(self):
        pos = book_state._TYPICAL_POS_FOR_SPLIT
        header = "BOOK_STATE|v741584e|2026-09-15|15:00|equity_fill 10203|bp 7004|"
        line = header + "positions 4|" + "|".join([pos] * 4)
        parts = book_state.split_book_state_messages(line)
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].startswith("BOOK_STATE_1of"))
        self.assertLessEqual(max(len(p) for p in parts), 2000)
        # Newest-first: last part first
        assembled = book_state.select_latest_book_state(list(reversed(parts)))
        self.assertEqual(assembled, line)

    def test_select_skips_incomplete_newest_split(self):
        complete = "BOOK_STATE|v741584e|2026-09-14|15:00|equity_fill 10000|bp 10000|realized_cum +0|sessions_elapsed 2|positions 0|session_date 2026-09-14"
        newest_partial = "BOOK_STATE_2of2|w=99|NOTENOUGH"
        contents = [newest_partial, complete]
        got = book_state.select_latest_book_state(contents)
        self.assertEqual(got, complete)


class TestRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.trades_path = Path(self.tmp.name) / "active_trades.json"
        self.trades_path.write_text("[]", encoding="utf-8")
        self.alerts = []
        self.patches = [
            mock.patch.object(vb, "DB_PATH", self.db),
            mock.patch.object(config, "NEWS_DB_PATH", self.db),
            mock.patch("tracker_agent.NEWS_DB_PATH", self.db),
            mock.patch("tracker_agent.ACTIVE_TRADES_PATH", self.trades_path),
            mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "741584eabcdef"}, clear=False),
            mock.patch(
                "broadcaster.send_discord_alert",
                side_effect=lambda m: self.alerts.append(m) or True,
            ),
        ]
        for p in self.patches:
            p.start()
        fa.reset_session_for_tests()
        vb.reset_book_for_tests()
        book_state.reset_for_tests()
        vb.ensure_ledger()
        signal_gate.get_gate().reset_day()

    def tearDown(self):
        book_state.reset_for_tests()
        fa.reset_session_for_tests()
        vb.reset_book_for_tests()
        signal_gate.get_gate().reset_day()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _line(self, **header_over):
        from tracker_agent import replace_active_trades

        replace_active_trades([_trade()])
        vb.restore_ledger_snapshot(
            buying_power=7004.0, realized_mid=250.0, realized_fill=213.0
        )
        book_state.set_sessions_elapsed(3)
        line = book_state.format_book_state_line(
            now=datetime(2026, 9, 15, 15, 0, 0)
        )
        replace_active_trades([])
        vb.restore_ledger_snapshot(
            buying_power=10000.0, realized_mid=0.0, realized_fill=0.0
        )
        book_state.set_sessions_elapsed(0)
        book_state.reset_for_tests()
        return line

    def test_restore_rebuilds_book_and_gate(self):
        line = self._line()
        now = datetime(2026, 9, 15, 8, 5, 0)
        result = book_state.restore_at_boot(contents=[line], now=now)
        self.assertTrue(result["ok"])
        self.assertFalse(result["unrecovered"])
        self.assertFalse(result["blocked"])
        from tracker_agent import load_active_trades

        trades = load_active_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["ticker"], "NVDA")
        port = vb.get_portfolio()
        self.assertAlmostEqual(port["buying_power"], 7004.0)
        self.assertAlmostEqual(port["total_realized_pnl_fill"], 213.0)
        self.assertEqual(book_state.current_session_number(), 4)
        gate = signal_gate.get_gate()
        self.assertIn("NVDA", gate._open)
        self.assertTrue(result["snapshot"])

    def test_missing_book_state_is_critical_and_flat(self):
        result = book_state.restore_at_boot(
            contents=["SESSION|v741584e|2026-09-14|scans 1"],
            now=datetime(2026, 9, 14, 8, 5, 0),
        )
        self.assertTrue(result["unrecovered"])
        self.assertFalse(result["blocked"])
        from tracker_agent import load_active_trades

        self.assertEqual(load_active_trades(), [])
        port = vb.get_portfolio()
        self.assertAlmostEqual(port["buying_power"], 10000.0)
        self.assertTrue(any("BOOK_STATE NOT RECOVERED" in a for a in self.alerts))

    def test_stale_halts_until_confirm(self):
        line = self._line()
        # 2026-09-15 snapshot vs 2026-09-20 is 5 calendar days.
        result = book_state.restore_at_boot(
            contents=[line], now=datetime(2026, 9, 20, 8, 5, 0)
        )
        self.assertTrue(result["stale"])
        self.assertTrue(result["blocked"])
        self.assertTrue(book_state.trading_blocked())
        self.assertTrue(any("BOOK_STATE STALE" in a for a in self.alerts))
        from tracker_agent import load_active_trades

        self.assertEqual(len(load_active_trades()), 1)

    def test_stale_confirm_allows_trading(self):
        line = self._line()
        with mock.patch.dict(os.environ, {"CONFIRM_STALE_BOOK": "2026-09-15"}):
            with mock.patch.object(config, "CONFIRM_STALE_BOOK", "2026-09-15"):
                result = book_state.restore_at_boot(
                    contents=[line], now=datetime(2026, 9, 20, 8, 5, 0)
                )
        self.assertTrue(result["stale"])
        self.assertFalse(result["blocked"])
        self.assertFalse(book_state.trading_blocked())

    def test_gate_exit_cooldown_survives_restore(self):
        import position_exits

        position_exits.reset_eod_flags_for_tests()
        book_state.note_exit(
            "MSFT", when=datetime(2026, 9, 15, 15, 30, 0, tzinfo=timezone.utc)
        )
        line = self._line()
        self.assertIn("gate_exits MSFT@", line)
        result = book_state.restore_at_boot(
            contents=[line], now=datetime(2026, 9, 15, 15, 40, 0)
        )
        self.assertTrue(result["ok"])
        st = signal_gate.get_gate()._st("MSFT")
        self.assertIsNotNone(st.last_exit_at)
        # 10 minutes after the 15:30 close — still inside 45m cooldown.
        # MSFT is not in the open book, so position_open does not mask this.
        decision = signal_gate.get_gate()._try_admit(
            "MSFT", "P", 80.0, datetime(2026, 9, 15, 15, 40, 0, tzinfo=timezone.utc)
        )
        self.assertFalse(decision.admit)
        self.assertIn("post_exit_cooldown", decision.reason)

    def test_eod_and_carry_flags_block_second_run(self):
        import position_exits
        from datetime import date

        position_exits.reset_eod_flags_for_tests()
        position_exits.mark_eod_done(date(2026, 9, 15))
        position_exits.mark_carry_review_done(date(2026, 9, 15))
        position_exits.mark_eod_book_done(date(2026, 9, 15))
        line = self._line()
        self.assertIn("eod_done 2026-09-15", line)
        self.assertIn("carry_done 2026-09-15", line)
        self.assertIn("eod_book_done 2026-09-15", line)
        position_exits.reset_eod_flags_for_tests()
        book_state.restore_at_boot(
            contents=[line], now=datetime(2026, 9, 15, 15, 10, 0)
        )
        self.assertTrue(position_exits.eod_already_done(date(2026, 9, 15)))
        self.assertTrue(position_exits.carry_review_already_done(date(2026, 9, 15)))
        self.assertTrue(position_exits.eod_book_already_done(date(2026, 9, 15)))
        self.assertIsNone(
            position_exits.maybe_emit_eod_book(datetime(2026, 9, 15, 15, 10, 0))
        )

    def test_version_mismatch_restores_and_flags(self):
        line = self._line()
        # Rewrite version token.
        parts = line.split("|")
        parts[1] = "vdeadbee"
        line = "|".join(parts)
        result = book_state.restore_at_boot(
            contents=[line], now=datetime(2026, 9, 15, 8, 5, 0)
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["version_mismatch"])
        self.assertFalse(result["blocked"])
        self.assertTrue(any("VERSION MISMATCH" in a for a in self.alerts))
        from tracker_agent import load_active_trades

        self.assertEqual(len(load_active_trades()), 1)


class TestEmit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.trades_path = Path(self.tmp.name) / "active_trades.json"
        self.trades_path.write_text("[]", encoding="utf-8")
        self.alerts = []
        self.patches = [
            mock.patch.object(vb, "DB_PATH", self.db),
            mock.patch.object(config, "NEWS_DB_PATH", self.db),
            mock.patch("tracker_agent.NEWS_DB_PATH", self.db),
            mock.patch("tracker_agent.ACTIVE_TRADES_PATH", self.trades_path),
            mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "741584eabcdef"}, clear=False),
            mock.patch(
                "broadcaster.send_discord_alert",
                side_effect=lambda m: self.alerts.append(m) or True,
            ),
        ]
        for p in self.patches:
            p.start()
        fa.reset_session_for_tests()
        vb.reset_book_for_tests()
        book_state.reset_for_tests()
        vb.ensure_ledger()

    def tearDown(self):
        book_state.reset_for_tests()
        fa.reset_session_for_tests()
        vb.reset_book_for_tests()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_identical_scan_is_skipped_eod_is_not(self):
        book_state.emit_book_state(reason="scan", now=datetime(2026, 9, 15, 10, 0, 0))
        n = len(self.alerts)
        book_state.emit_book_state(reason="scan", now=datetime(2026, 9, 15, 10, 5, 0))
        self.assertEqual(len(self.alerts), n)
        book_state.emit_book_state(reason="eod", now=datetime(2026, 9, 15, 14, 45, 0))
        self.assertGreater(len(self.alerts), n)
        self.assertEqual(book_state.sessions_elapsed(), 1)

    def test_eod_hook_posts_book_state(self):
        import position_exits

        position_exits.reset_eod_flags_for_tests()
        with mock.patch("tracker_agent.load_active_trades", return_value=[]):
            line = position_exits.maybe_emit_eod_book(
                datetime(2026, 9, 15, 14, 45, 0)
            )
        self.assertIsNotNone(line)
        self.assertTrue(any(a.startswith("BOOK_STATE|") for a in self.alerts))
