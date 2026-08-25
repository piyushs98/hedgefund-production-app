"""Earnings blackout: window, scraper+env merge, gate BLOCK, flatten vs hold."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import config
import earnings_blackout as eb
import news_memory
import position_exits
import signal_gate as sg


class TestParseAndWindow(unittest.TestCase):
    def setUp(self):
        eb.reset_for_tests()
        eb.set_calendar_for_tests(
            {"NVDA": date(2026, 8, 26)},
            {"NVDA": "env"},
        )

    def tearDown(self):
        eb.reset_for_tests()

    def test_parse_env_overrides(self):
        parsed = eb.parse_env_overrides("NVDA:2026-08-26, AAPL:2026-09-01")
        self.assertEqual(parsed["NVDA"], date(2026, 8, 26))
        self.assertEqual(parsed["AAPL"], date(2026, 9, 1))

    def test_parse_env_skips_malformed(self):
        parsed = eb.parse_env_overrides("NVDA, :2026-08-26, TSLA:not-a-date")
        self.assertEqual(parsed, {})

    def test_parse_print_date_from_scraper_string(self):
        self.assertEqual(
            eb.parse_print_date("Corporate Earnings Scheduled for 2026-08-26 00:00:00"),
            date(2026, 8, 26),
        )
        self.assertEqual(
            eb.parse_print_date("Corporate Earnings Scheduled for 2026-08-26"),
            date(2026, 8, 26),
        )

    def test_nvda_window_default_1_1(self):
        # print Wed Aug 26 → blackout Tue 25, Wed 26, Thu 27
        self.assertFalse(eb.is_blacked_out("NVDA", date(2026, 8, 24)))
        self.assertTrue(eb.is_blacked_out("NVDA", date(2026, 8, 25)))
        self.assertTrue(eb.is_blacked_out("NVDA", date(2026, 8, 26)))
        self.assertTrue(eb.is_blacked_out("NVDA", date(2026, 8, 27)))
        self.assertFalse(eb.is_blacked_out("NVDA", date(2026, 8, 28)))
        self.assertFalse(eb.is_blacked_out("AAPL", date(2026, 8, 26)))

    def test_window_respects_before_after(self):
        with mock.patch.object(config, "BLACKOUT_DAYS_BEFORE", 2):
            with mock.patch.object(config, "BLACKOUT_DAYS_AFTER", 0):
                self.assertTrue(eb.is_blacked_out("NVDA", date(2026, 8, 24)))
                self.assertTrue(eb.is_blacked_out("NVDA", date(2026, 8, 26)))
                self.assertFalse(eb.is_blacked_out("NVDA", date(2026, 8, 27)))


class TestScraperAndEnvMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.db_patch = mock.patch.object(news_memory, "DB_PATH", self.db)
        self.cfg_patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.db_patch.start()
        self.cfg_patch.start()
        news_memory.init_db()
        eb.reset_for_tests()

    def tearDown(self):
        eb.reset_for_tests()
        self.db_patch.stop()
        self.cfg_patch.stop()
        self.tmp.cleanup()

    def test_scraper_row_auto_loads(self):
        news_memory.save_innovation_data(
            "NVDA",
            "EARNINGS",
            "Corporate Earnings Scheduled for 2026-08-26",
        )
        with mock.patch.dict(os.environ, {"EARNINGS_BLACKOUT": ""}, clear=False):
            cal = eb.load_calendar(force=True)
        self.assertEqual(cal["NVDA"], date(2026, 8, 26))
        self.assertEqual(eb.calendar_source("NVDA"), "scraper")
        self.assertTrue(eb.is_blacked_out("NVDA", date(2026, 8, 25)))

    def test_env_overrides_scraper_same_ticker(self):
        news_memory.save_innovation_data(
            "NVDA",
            "EARNINGS",
            "Corporate Earnings Scheduled for 2026-08-30",
        )
        with mock.patch.dict(
            os.environ, {"EARNINGS_BLACKOUT": "NVDA:2026-08-26"}, clear=False
        ):
            cal = eb.load_calendar(force=True)
        self.assertEqual(cal["NVDA"], date(2026, 8, 26))
        self.assertEqual(eb.calendar_source("NVDA"), "env")

    def test_env_adds_ticker_scraper_keeps_others(self):
        news_memory.save_innovation_data(
            "AAPL",
            "EARNINGS",
            "Corporate Earnings Scheduled for 2026-09-01",
        )
        with mock.patch.dict(
            os.environ, {"EARNINGS_BLACKOUT": "NVDA:2026-08-26"}, clear=False
        ):
            cal = eb.load_calendar(force=True)
        self.assertEqual(cal["AAPL"], date(2026, 9, 1))
        self.assertEqual(eb.calendar_source("AAPL"), "scraper")
        self.assertEqual(cal["NVDA"], date(2026, 8, 26))
        self.assertEqual(eb.calendar_source("NVDA"), "env")


class TestGateBlockReason(unittest.TestCase):
    def setUp(self):
        eb.reset_for_tests()
        eb.set_calendar_for_tests(
            {"NVDA": date(2026, 8, 26)},
            {"NVDA": "env"},
        )

    def tearDown(self):
        eb.reset_for_tests()

    def test_compact_reason(self):
        self.assertEqual(sg._compact_reason("earnings_blackout"), "earnings_blackout")

    def test_gate_blocks_nvda_inside_window(self):
        gate = sg.reset_gate_for_tests(
            sg.GateConfig(threshold=70.0, persist_cycles=1, max_concurrent=10)
        )
        now = datetime(2026, 8, 25, 14, 30, tzinfo=timezone.utc)  # 09:30 ET Aug 25
        decs = gate.process_scan(
            [
                sg.Observation("NVDA", 82.0, "P", "EXECUTE"),
                sg.Observation("AAPL", 81.0, "C", "EXECUTE"),
            ],
            now,
        )
        by_t = {d.ticker: d for d in decs}
        self.assertFalse(by_t["NVDA"].admit)
        self.assertEqual(by_t["NVDA"].reason, "earnings_blackout")
        self.assertTrue(by_t["AAPL"].admit)
        summary = gate.format_scan_summary(decs)
        self.assertIn("earnings_blackout×1", summary)
        self.assertIn("ADMIT AAPL:C@81", summary)

    def test_gate_allows_nvda_before_window(self):
        gate = sg.reset_gate_for_tests(
            sg.GateConfig(threshold=70.0, persist_cycles=1, max_concurrent=10)
        )
        now = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)  # Aug 24 ET
        decs = gate.process_scan(
            [sg.Observation("NVDA", 82.0, "P", "EXECUTE")],
            now,
        )
        self.assertTrue(decs[0].admit)
        self.assertNotIn("earnings_blackout", gate.format_scan_summary(decs))

    def test_blackout_check_exception_blocks_fail_closed(self):
        eb.reset_for_tests()
        alerts = []
        gate = sg.reset_gate_for_tests(
            sg.GateConfig(threshold=70.0, persist_cycles=1, max_concurrent=10)
        )
        now = datetime(2026, 8, 25, 14, 30, tzinfo=timezone.utc)
        with mock.patch.object(eb, "is_blacked_out", side_effect=RuntimeError("db locked")):
            with mock.patch(
                "broadcaster.send_discord_alert",
                side_effect=lambda m: alerts.append(m) or True,
            ):
                decs = gate.process_scan(
                    [
                        sg.Observation("NVDA", 82.0, "P", "EXECUTE"),
                        sg.Observation("AAPL", 81.0, "C", "EXECUTE"),
                    ],
                    now,
                )
                # Second scan: still blocked, no second CRITICAL
                gate.process_scan(
                    [sg.Observation("MSFT", 80.0, "C", "EXECUTE")],
                    now,
                )
        by_t = {d.ticker: d for d in decs}
        self.assertFalse(by_t["NVDA"].admit)
        self.assertEqual(by_t["NVDA"].reason, "blackout_check_failed")
        self.assertFalse(by_t["AAPL"].admit)
        self.assertEqual(by_t["AAPL"].reason, "blackout_check_failed")
        summary = gate.format_scan_summary(decs)
        self.assertIn("blackout_check_failed×2", summary)
        crits = [m for m in alerts if "BLACKOUT CHECK FAILED" in m]
        self.assertEqual(len(crits), 1)

    def test_blackout_beats_dead_zone_label(self):
        gate = sg.reset_gate_for_tests(
            sg.GateConfig(threshold=70.0, persist_cycles=1, max_concurrent=10)
        )
        now = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
        decs = gate.process_scan(
            [
                sg.Observation(
                    "NVDA",
                    40.0,
                    None,
                    "PASS",
                    block_reason="dead_zone",
                )
            ],
            now,
        )
        self.assertEqual(decs[0].reason, "earnings_blackout")
        self.assertIn("earnings_blackout×1", gate.format_scan_summary(decs))
        self.assertNotIn("dead_zone", gate.format_scan_summary(decs))


class TestFlattenSpanning(unittest.TestCase):
    def setUp(self):
        eb.reset_for_tests()
        eb.set_calendar_for_tests(
            {"NVDA": date(2026, 8, 26)},
            {"NVDA": "env"},
        )
        position_exits.reset_eod_flags_for_tests()

    def tearDown(self):
        eb.reset_for_tests()
        position_exits.reset_eod_flags_for_tests()

    def _nvda_0831(self, **over):
        trade = {
            "trade_id": "nvda-210p",
            "ticker": "NVDA",
            "direction": "PUT",
            "strike": 210.0,
            "expiration": "2026-08-31",
            "entry_price": 2.62,
            "entry_premium": 2.62,
            "stop_loss": 2.10,
            "take_profit": 3.93,
        }
        trade.update(over)
        return trade

    def test_flatten_open_lot_that_spans_print(self):
        # NVDA 210P 08/31 held into Tue Aug 25 (window start)
        reason, px = position_exits.evaluate_exit_reason_for_mark(
            self._nvda_0831(),
            2.50,
            sess=date(2026, 8, 25),
            now=datetime(2026, 8, 25, 9, 0, 0),
            include_time_stop=False,
            do_eod=False,
        )
        self.assertEqual(reason, "EARNINGS_FLATTEN")
        self.assertAlmostEqual(px, 2.50)

    def test_hold_when_flatten_disabled(self):
        with mock.patch.object(config, "EARNINGS_FLATTEN_SPANNING", False):
            reason, px = position_exits.evaluate_exit_reason_for_mark(
                self._nvda_0831(),
                2.50,
                sess=date(2026, 8, 25),
                now=datetime(2026, 8, 25, 9, 0, 0),
                include_time_stop=False,
                do_eod=False,
            )
        self.assertIsNone(reason)
        self.assertIsNone(px)

    def test_no_flatten_before_window(self):
        reason, _px = position_exits.evaluate_exit_reason_for_mark(
            self._nvda_0831(),
            2.50,
            sess=date(2026, 8, 24),
            now=datetime(2026, 8, 24, 14, 0, 0),
            include_time_stop=False,
            do_eod=False,
        )
        self.assertIsNone(reason)

    def test_expiry_before_print_does_not_earnings_flatten(self):
        # 08/25 expiry does not span Wed 08/26 print
        trade = self._nvda_0831(expiration="2026-08-25")
        reason, _px = position_exits.evaluate_exit_reason_for_mark(
            trade,
            2.50,
            sess=date(2026, 8, 25),
            now=datetime(2026, 8, 25, 10, 0, 0),  # before 13:00 0DTE flatten
            include_time_stop=False,
            do_eod=False,
        )
        self.assertNotEqual(reason, "EARNINGS_FLATTEN")


class TestConfigDefaults(unittest.TestCase):
    def test_blackout_defaults(self):
        self.assertEqual(config.BLACKOUT_DAYS_BEFORE, 1)
        self.assertEqual(config.BLACKOUT_DAYS_AFTER, 1)
        self.assertTrue(config.EARNINGS_FLATTEN_SPANNING)


class TestChinaScraperDisabled(unittest.TestCase):
    def test_does_not_write_innovation_data(self):
        from china_macro_scraper import scrape_china_macro
        with mock.patch("news_memory.save_innovation_data") as save:
            scrape_china_macro(["AAPL", "NVDA", "TSLA"])
        save.assert_not_called()


class TestGovPolicyScraperDisabled(unittest.TestCase):
    def test_does_not_write_innovation_data(self):
        from gov_policy_scraper import scrape_gov_policy
        with mock.patch("news_memory.save_innovation_data") as save:
            scrape_gov_policy(["SPY", "AAPL", "NVDA"])
        save.assert_not_called()

    def test_macro_vector_ignores_leftover_gov_and_china(self):
        import midday_delta
        leftover = (
            "- [t] [GOV_POLICY] Federal Reserve signals potential rate cut in Q3\n"
            "- [t] [CHINA_MACRO] Severe supply-chain bottlenecks detected at Shenzhen ports\n"
        )
        with mock.patch.object(
            midday_delta, "get_innovation_context", return_value=leftover
        ):
            vec = midday_delta.macro_vector_local("AAPL")
        self.assertNotIn("EXPANSIONARY_TAILWIND", vec)
        self.assertNotIn("SUPPLY_CHAIN_BOTTLENECK", vec)


if __name__ == "__main__":
    unittest.main()
