"""Go-live v2: flatten-all, admit cutoff, Chicago clock, fail-open reliability.

These tests do not start the bot and do not write the live news database.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import config
import position_exits
import session_clock


UTC = timezone.utc


def _utc(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


class TestChicagoSchedule(unittest.TestCase):
    def test_named_clocks_follow_chicago_after_dst(self):
        """2026-11-02 is CST (UTC-6). A fixed UTC-5 offset would be an hour early."""
        from midday_delta import is_full_scan_window
        from position_exits import is_eod_flatten_window, is_zero_dte_flatten_window

        # 14:00 UTC = 08:00 CST. UTC-5 would read 09:00 and open the scan.
        self.assertFalse(is_full_scan_window(_utc(2026, 11, 2, 14, 0)))
        # 14:45 UTC = 08:45 CST. First full scan.
        self.assertFalse(is_full_scan_window(_utc(2026, 11, 2, 14, 44)))
        self.assertTrue(is_full_scan_window(_utc(2026, 11, 2, 14, 45)))
        # 18:00 UTC = 12:00 CST. UTC-5 would read 13:00 and flatten 0DTE.
        self.assertFalse(is_zero_dte_flatten_window(_utc(2026, 11, 2, 18, 0)))
        # 19:00 UTC = 13:00 CST.
        self.assertTrue(is_zero_dte_flatten_window(_utc(2026, 11, 2, 19, 0)))
        # 19:45 UTC = 13:45 CST. The last admit slot is still open.
        self.assertIsNone(session_clock.admit_block(_utc(2026, 11, 2, 19, 45)))
        # 20:00 UTC = 14:00 CST. Not the flatten yet.
        self.assertFalse(is_eod_flatten_window(_utc(2026, 11, 2, 20, 0)))
        # 20:15 UTC = 14:15 CST. Admits are closed.
        self.assertEqual(session_clock.admit_block(_utc(2026, 11, 2, 20, 15)), "late_session")
        # 20:45 UTC = 14:45 CST.
        self.assertTrue(is_eod_flatten_window(_utc(2026, 11, 2, 20, 45)))

    def test_scan_stamp_is_chicago_not_utc_minus_five(self):
        # 15:45 UTC on 2026-11-02 is 09:45 CST. UTC-5 would stamp 10:45.
        stamp = session_clock.chicago_stamp(_utc(2026, 11, 2, 15, 45))
        self.assertEqual(stamp, "20261102-094500")
        self.assertNotEqual(stamp, "20261102-104500")

    def test_schedule_modules_have_no_fixed_utc_minus_five(self):
        root = Path(__file__).resolve().parents[1]
        for name in (
            "midday_delta.py",
            "position_exits.py",
            "session_clock.py",
            "master_bot.py",
            "signal_gate.py",
        ):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("timedelta(hours=-5)", text, name)
            self.assertNotIn("timedelta(hours=-6)", text, name)
        midday = (root / "midday_delta.py").read_text(encoding="utf-8")
        self.assertIn("session_clock.chicago_stamp()", midday)

    def test_august_afternoon_utc_is_still_morning_in_chicago(self):
        # The older gate tests pass 14:00 UTC in August. That is 09:00 CDT.
        self.assertIsNone(session_clock.admit_block(_utc(2026, 8, 4, 14, 0)))
        self.assertIsNone(session_clock.admit_block(_utc(2026, 8, 4, 18, 0)))


class TestEarlyCloseAndHoliday(unittest.TestCase):
    def test_thanksgiving_is_closed_and_friday_is_an_early_close(self):
        from master_bot import NYSE_HOLIDAYS

        self.assertIn("2026-11-26", NYSE_HOLIDAYS)
        self.assertNotIn("2026-11-27", NYSE_HOLIDAYS)
        self.assertNotIn("2026-12-24", NYSE_HOLIDAYS)
        self.assertEqual(
            session_clock.next_session(date(2026, 11, 25), NYSE_HOLIDAYS),
            date(2026, 11, 27),
        )

    def test_early_close_blocks_all_day_and_flattens_at_1145(self):
        morning = _utc(2026, 11, 27, 15, 0)  # 09:00 CST
        self.assertEqual(session_clock.admit_block(morning), "early_close")
        self.assertFalse(position_exits.is_eod_flatten_window(_utc(2026, 11, 27, 17, 44)))
        self.assertTrue(position_exits.is_eod_flatten_window(_utc(2026, 11, 27, 17, 45)))
        christmas_eve = _utc(2026, 12, 24, 15, 0)
        self.assertEqual(session_clock.admit_block(christmas_eve), "early_close")
        self.assertTrue(position_exits.is_eod_flatten_window(_utc(2026, 12, 24, 17, 45)))
        # The day before Thanksgiving is a normal session.
        self.assertIsNone(session_clock.admit_block(_utc(2026, 11, 25, 15, 0)))
        self.assertFalse(position_exits.is_eod_flatten_window(_utc(2026, 11, 25, 17, 45)))


class TestAdmitCutoff(unittest.TestCase):
    def _gate(self, persist):
        from signal_gate import GateConfig, SignalGate
        return SignalGate(GateConfig(max_concurrent=10, persist_cycles=persist))

    def test_1345_still_admits_and_1415_is_late_session(self):
        from signal_gate import Observation

        gate = self._gate(1)
        early = gate.process_scan(
            [Observation("SPY", 82, "C", "EXECUTE")],
            _utc(2026, 11, 2, 19, 45),
        )
        self.assertTrue(early[0].admit)
        late_gate = self._gate(1)
        late = late_gate.process_scan(
            [
                Observation("SPY", 82, "C", "EXECUTE"),
                Observation("QQQ", 60, "C", "EXECUTE"),
            ],
            _utc(2026, 11, 2, 20, 15),
        )
        by_t = {d.ticker: d for d in late}
        self.assertFalse(by_t["SPY"].admit)
        self.assertEqual(by_t["SPY"].reason, "late_session")
        self.assertIn("below", by_t["QQQ"].reason)
        self.assertNotEqual(by_t["QQQ"].reason, "late_session")
        summary = late_gate.format_scan_summary(late)
        self.assertIn("late_session×1", summary)
        self.assertIn("below_thr×1", summary)

    def test_late_session_does_not_reset_the_streak(self):
        from signal_gate import Observation

        gate = self._gate(2)
        first = gate.process_scan(
            [Observation("IWM", 88, "C", "EXECUTE")],
            _utc(2026, 11, 2, 19, 45),
        )
        self.assertIn("persistence", first[0].reason)
        self.assertEqual(gate.state["IWM"].streak, 1)
        second = gate.process_scan(
            [Observation("IWM", 88, "C", "EXECUTE")],
            _utc(2026, 11, 2, 20, 15),
        )
        self.assertEqual(second[0].reason, "late_session")
        self.assertFalse(second[0].admit)
        self.assertEqual(gate.state["IWM"].streak, 2)

    def test_empty_book_prints_one_closed_line(self):
        from signal_gate import Observation

        gate = self._gate(1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            gate.process_scan(
                [Observation("QQQ", 50, "C", "EXECUTE")],
                _utc(2026, 11, 2, 21, 0),
            )
        self.assertIn("[Gate] admits closed: late_session", buf.getvalue())

    def test_early_close_reason_is_not_late_session(self):
        from signal_gate import Observation

        gate = self._gate(1)
        dec = gate.process_scan(
            [Observation("META", 90, "C", "EXECUTE")],
            _utc(2026, 11, 27, 16, 0),
        )
        self.assertEqual(dec[0].reason, "early_close")
        self.assertFalse(dec[0].admit)
        self.assertIn("early_close×1", gate.format_scan_summary(dec))


class TestBootAndFlatten(unittest.TestCase):
    def setUp(self):
        position_exits.reset_eod_flags_for_tests()

    def tearDown(self):
        position_exits.reset_eod_flags_for_tests()

    def _trade(self, **over):
        base = {
            "trade_id": "t-boot",
            "ticker": "IWM",
            "direction": "CALL",
            "strike": 302.0,
            "expiration": "2026-08-21",
            "entry_price": 1.42,
            "entry_premium": 1.42,
            "entry_ask": 1.50,
            "stop_loss": 1.14,
            "take_profit": 2.13,
            "entry_score": 85.0,
            "quantity": 1,
            "entry_timestamp": "2026-08-05T14:32:00Z",
        }
        base.update(over)
        return base

    def _chain(self, bid, ask, last):
        return {
            "current_price": 302.0,
            "chains": {
                "2026-08-21": {
                    "calls": [{
                        "strike": 302.0,
                        "bid": bid,
                        "ask": ask,
                        "lastPrice": last,
                    }],
                    "puts": [],
                }
            },
        }

    def test_open_at_boot_pages_critical_and_closes_on_a_clean_mark(self):
        trade = self._trade()
        with mock.patch("tracker_agent.load_active_trades", return_value=[trade]):
            with mock.patch("broadcaster.send_discord_alert", return_value=True) as send:
                n = position_exits.note_open_book_at_boot()
        self.assertEqual(n, 1)
        self.assertTrue(send.call_args.kwargs.get("critical"))
        self.assertIn("OPEN AT BOOT", send.call_args.args[0])
        scored = {"IWM": {"options_dict": self._chain(1.20, 1.40, 1.30), "card": None}}
        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {"ok": True, "ticker": "IWM", "reason": "BOOT_FLATTEN"}
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch("tracker_agent.load_active_trades", return_value=[]):
                        with mock.patch("carry_shadow.record_flatten") as shadow:
                            summary = position_exits.run_scan_exits(
                                [trade],
                                scored,
                                now_cdt=datetime(2026, 8, 5, 10, 0),
                                session_date=date(2026, 8, 5),
                                force_eod=False,
                            )
        self.assertEqual(close.call_args.args[2], "BOOT_FLATTEN")
        self.assertAlmostEqual(close.call_args.args[1], 1.30, places=2)
        self.assertEqual(len(summary["closed"]), 1)
        shadow.assert_not_called()

    def test_last_price_only_is_not_a_clean_boot_mark(self):
        trade = self._trade(entry_timestamp=datetime.now(timezone.utc).isoformat())
        position_exits._boot_flatten_open = True
        scored = {"IWM": {"options_dict": self._chain(None, None, 1.30), "card": None}}
        with mock.patch("position_exits.close_open_position") as close:
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch("tracker_agent.load_active_trades", return_value=[trade]):
                        summary = position_exits.run_scan_exits(
                            [trade],
                            scored,
                            now_cdt=datetime(2026, 8, 5, 10, 0),
                            session_date=date(2026, 8, 5),
                            force_eod=False,
                        )
        close.assert_not_called()
        self.assertEqual(summary["closed"], [])
        self.assertTrue(position_exits._boot_flatten_open)

    def test_boot_during_the_flatten_window_is_eod_flatten(self):
        trade = self._trade()
        position_exits._boot_flatten_open = True
        scored = {"IWM": {"options_dict": self._chain(1.20, 1.40, 1.30), "card": None}}
        with mock.patch("position_exits.close_open_position") as close:
            close.return_value = {
                "ok": True,
                "pnl_fill": -10.0,
                "exit_price": 1.20,
                "exit_bid": 1.20,
                "entry_ask": 1.50,
                "quantity": 1,
            }
            with mock.patch("position_exits.record_position_mark", return_value=True):
                with mock.patch("tracker_agent.save_active_trade", return_value=True):
                    with mock.patch("tracker_agent.load_active_trades", return_value=[]):
                        with mock.patch("carry_shadow.record_flatten") as shadow:
                            position_exits.run_scan_exits(
                                [trade],
                                scored,
                                now_cdt=datetime(2026, 8, 5, 14, 45),
                                session_date=date(2026, 8, 5),
                                force_eod=True,
                            )
        self.assertEqual(close.call_args.args[2], "EOD_FLATTEN")
        shadow.assert_called_once()


class TestCarryShadow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _trade(self, score, trade_id="t-hi"):
        return {
            "trade_id": trade_id,
            "ticker": "SPY",
            "direction": "CALL",
            "strike": 500.0,
            "expiration": "2026-11-20",
            "entry_premium": 2.00,
            "entry_ask": 2.10,
            "entry_score": score,
            "stop_loss": 1.60,
            "take_profit": 3.00,
            "quantity": 1,
        }

    def _closed(self):
        return {
            "ok": True,
            "pnl_fill": -25.0,
            "exit_price": 1.90,
            "exit_bid": 1.85,
            "entry_ask": 2.10,
            "quantity": 1,
        }

    def _quote(self, bid, ask):
        return json.dumps({
            "chains": {
                "2026-11-20": {
                    "calls": [{
                        "strike": 500.0,
                        "bid": bid,
                        "ask": ask,
                        "lastPrice": (bid + ask) / 2.0,
                    }],
                    "puts": [],
                }
            }
        })

    def _rows(self):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute("SELECT * FROM carry_shadow").fetchall())

    def test_only_eighty_plus_eod_flattens_are_shadowed(self):
        import carry_shadow

        with mock.patch("virtual_broker.paper_sell") as sell:
            carry_shadow.record_flatten(self._trade(79, "low"), self._closed(), date(2026, 11, 16))
            carry_shadow.record_flatten(self._trade(85), self._closed(), date(2026, 11, 16))
            sell.assert_not_called()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], "t-hi")
        self.assertEqual(rows[0]["status"], "open")
        self.assertEqual(rows[0]["next_session"], "2026-11-17")
        self.assertAlmostEqual(rows[0]["flatten_pnl"], -25.0)

    def test_time_stop_from_0830_ignores_the_score_exempt(self):
        import carry_shadow

        carry_shadow.record_flatten(self._trade(85), self._closed(), date(2026, 11, 16))
        with mock.patch("virtual_broker.paper_sell") as sell:
            with mock.patch(
                "data_engineer.fetch_contract_quote",
                return_value=self._quote(2.00, 2.10),
            ):
                carry_shadow.advance(datetime(2026, 11, 17, 8, 0))
                self.assertEqual(self._rows()[0]["status"], "open")
                carry_shadow.advance(datetime(2026, 11, 17, 10, 0))
            sell.assert_not_called()
        row = self._rows()[0]
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["shadow_reason"], "TIME_STOP")
        self.assertAlmostEqual(row["shadow_pnl"], -10.0)

    def test_missed_next_session_does_not_invent_a_fill(self):
        import carry_shadow

        carry_shadow.record_flatten(self._trade(85), self._closed(), date(2026, 11, 16))
        with mock.patch("virtual_broker.paper_sell") as sell:
            carry_shadow.advance(datetime(2026, 11, 18, 10, 0))
            sell.assert_not_called()
        row = self._rows()[0]
        self.assertEqual(row["status"], "missed")
        self.assertEqual(row["shadow_reason"], "MISSED_SESSION")
        self.assertIsNone(row["shadow_pnl"])
        self.assertIsNone(row["shadow_px"])

    def test_early_close_next_session_ends_at_1145(self):
        import carry_shadow

        wide = self._trade(86, "eve")
        wide["stop_loss"] = 1.00
        wide["take_profit"] = 4.00
        carry_shadow.record_flatten(wide, self._closed(), date(2026, 11, 25))
        self.assertEqual(self._rows()[0]["next_session"], "2026-11-27")
        with mock.patch(
            "data_engineer.fetch_contract_quote",
            return_value=self._quote(2.25, 2.35),
        ):
            with mock.patch("virtual_broker.paper_sell") as sell:
                carry_shadow.advance(datetime(2026, 11, 27, 11, 44))
                self.assertEqual(self._rows()[0]["status"], "open")
                carry_shadow.advance(datetime(2026, 11, 27, 11, 45))
                sell.assert_not_called()
        row = self._rows()[0]
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["shadow_reason"], "SESSION_END")
        self.assertAlmostEqual(row["shadow_pnl"], 15.0)


class TestReliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_heartbeat_unset_and_failure_are_ignored(self):
        import reliability

        reliability._HEARTBEAT_UNSET_LOGGED = False
        with mock.patch.dict(os.environ, {"HEARTBEAT_URL": ""}):
            with mock.patch("requests.get") as get:
                reliability.ping_heartbeat("scan")
                reliability.ping_heartbeat("exit")
                get.assert_not_called()
        with mock.patch.dict(os.environ, {"HEARTBEAT_URL": "https://example.invalid/hb"}):
            with mock.patch("requests.get", side_effect=RuntimeError("down")):
                reliability.ping_heartbeat("prep")
        self.assertFalse(os.path.exists(self.db))

    def test_resend_does_not_count_another_critical(self):
        import broadcaster
        import fill_accounting as fa
        import reliability

        fa.reset_session_for_tests()
        with mock.patch.object(broadcaster, "WEBHOOK_URL", "https://example.invalid/hook"):
            with mock.patch.object(broadcaster, "_post_chunk", return_value=False):
                with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK": "https://example.invalid/hook"}):
                    ok = broadcaster.send_discord_alert("desk is dark", critical=True)
        self.assertFalse(ok)
        self.assertEqual(fa._session["criticals"], 1)
        with sqlite3.connect(self.db) as conn:
            queued = conn.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0]
        self.assertEqual(queued, 1)
        resp = mock.Mock(status_code=200)
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK": "https://example.invalid/hook"}):
            with mock.patch("requests.post", return_value=resp):
                reliability.flush_discord_outbox()
        self.assertEqual(fa._session["criticals"], 1)
        with sqlite3.connect(self.db) as conn:
            left = conn.execute("SELECT COUNT(*) FROM discord_outbox").fetchone()[0]
        self.assertEqual(left, 0)
        fa.reset_session_for_tests()

    def test_criticals_leave_ahead_of_older_messages(self):
        import reliability

        reliability.enqueue_discord(["ordinary scan note"], critical=False)
        reliability.enqueue_discord(["CRITICAL: mark failed"], critical=True)
        seen = []

        def _post(url, json=None, timeout=None):
            seen.append(json["content"])
            if len(seen) == 1:
                raise RuntimeError("dns")
            return mock.Mock(status_code=200)

        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK": "https://example.invalid/hook"}):
            with mock.patch("requests.post", side_effect=_post):
                reliability.flush_discord_outbox()
        self.assertEqual(seen, ["CRITICAL: mark failed"])
        with sqlite3.connect(self.db) as conn:
            left = [
                row[0]
                for row in conn.execute(
                    "SELECT body FROM discord_outbox ORDER BY critical DESC, id"
                )
            ]
        self.assertEqual(left, ["CRITICAL: mark failed", "ordinary scan note"])

    def test_battery_warning_is_once_per_session_and_not_critical(self):
        import reliability

        reliability._BATTERY_WARNED.clear()
        proc = mock.Mock(stdout="Now drawing from 'Battery Power'\n", stderr="")
        with mock.patch("subprocess.run", return_value=proc):
            with mock.patch("broadcaster.send_discord_alert", return_value=True) as send:
                reliability.warn_if_on_battery(date(2026, 11, 16))
                reliability.warn_if_on_battery(date(2026, 11, 16))
        send.assert_called_once()
        self.assertFalse(send.call_args.kwargs.get("critical"))
        self.assertIn("MAC ON BATTERY", send.call_args.args[0])
        reliability._BATTERY_WARNED.clear()

    def test_news_fallback_is_one_line_per_pass(self):
        import master_bot

        master_bot._news_fallback_notes.clear()
        master_bot._note_news_fallback("SPY", "404")
        master_bot._note_news_fallback("QQQ", "404")
        buf = io.StringIO()
        with redirect_stdout(buf):
            master_bot.flush_news_fallback_errors("scan")
            master_bot.flush_news_fallback_errors("scan")
        text = buf.getvalue()
        self.assertEqual(text.count("[news] yfinance fallback failed"), 1)
        self.assertIn("2 ticker", text)


if __name__ == "__main__":
    unittest.main()
