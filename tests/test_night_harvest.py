"""Night harvest runs once, then sleeps until the next 09:15 ET."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, time
from unittest import mock

import pytz

import config
import night_mode
import yf_client


EASTERN = pytz.timezone("America/New_York")


def _et(y, m, d, hh, mm, ss=0):
    return EASTERN.localize(datetime(y, m, d, hh, mm, ss))


class TestNightSchedule(unittest.TestCase):
    def test_evening_sleeps_until_next_premarket_not_one_minute(self):
        # 2026-09-28 20:06:19 ET. clock.time() >= 09:15 is true here.
        # That comparison is what collapsed the old 45-minute sleep to 60s.
        now = _et(2026, 9, 28, 20, 6, 19)
        self.assertGreaterEqual(now.time(), time(9, 15))
        target = night_mode.next_premarket(now, set())
        self.assertEqual(target, _et(2026, 9, 29, 9, 15))
        chunks = list(night_mode.iter_night_sleep_chunks(now, set()))
        self.assertGreaterEqual(chunks[0], 3600)
        self.assertNotIn(60, [int(c) for c in chunks])
        self.assertAlmostEqual(sum(chunks), (target - now).total_seconds(), places=3)
        self.assertEqual(night_mode.harvest_session_key(now, set()), "2026-09-29")

    def test_weekend_harvests_once_for_monday(self):
        friday = _et(2026, 9, 25, 20, 0)
        sunday = _et(2026, 9, 27, 12, 0)
        self.assertEqual(night_mode.harvest_session_key(friday, set()), "2026-09-28")
        self.assertEqual(night_mode.harvest_session_key(sunday, set()), "2026-09-28")
        self.assertEqual(
            night_mode.next_premarket(friday, set()),
            _et(2026, 9, 28, 9, 15),
        )

    def test_holiday_monday_pushes_premarket_to_tuesday(self):
        sunday = _et(2026, 9, 6, 20, 0)
        holidays = {"2026-09-07"}
        self.assertEqual(
            night_mode.next_premarket(sunday, holidays),
            _et(2026, 9, 8, 9, 15),
        )
        self.assertEqual(
            night_mode.harvest_session_key(sunday, holidays),
            "2026-09-08",
        )

    def test_premarket_closer_than_an_hour_sleeps_the_remainder(self):
        now = _et(2026, 9, 29, 9, 14, 0)
        chunks = list(night_mode.iter_night_sleep_chunks(now, set()))
        self.assertEqual(len(chunks), 1)
        self.assertAlmostEqual(chunks[0], 60.0, places=3)

    def test_after_midnight_same_session_key(self):
        evening = _et(2026, 9, 28, 20, 6)
        overnight = _et(2026, 9, 29, 2, 0)
        self.assertEqual(
            night_mode.harvest_session_key(evening, set()),
            night_mode.harvest_session_key(overnight, set()),
        )

    def test_harvest_flag_once_per_date_and_survives_memory_reset(self):
        night_mode.reset_for_tests()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(night_mode.reset_for_tests)
        path = os.path.join(tmp.name, "news.db")
        self.assertFalse(night_mode.already_harvested("2026-09-29", path))
        night_mode.mark_harvested("2026-09-29", path)
        self.assertTrue(night_mode.already_harvested("2026-09-29", path))
        night_mode.reset_for_tests()
        self.assertTrue(night_mode.already_harvested("2026-09-29", path))
        self.assertFalse(night_mode.already_harvested("2026-09-30", path))

    def test_banner_reads_config_intervals(self):
        exit_s, full_s = night_mode.resolve_cadence(
            config.EXIT_INTERVAL_SECONDS,
            config.FULL_SCAN_INTERVAL_SECONDS,
        )
        self.assertEqual(exit_s, 300)
        self.assertEqual(full_s, 1800)
        headline = night_mode.boot_headline(exit_s, full_s)
        mode = night_mode.intraday_mode_line(False, exit_s, full_s)
        exit_line = night_mode.exit_only_line(exit_s)
        self.assertIn("5-min exits", headline)
        self.assertIn("30-min full scan", headline)
        self.assertNotIn("15-min", headline)
        self.assertIn("EXIT every 5m", mode)
        self.assertIn("every 30m", mode)
        self.assertNotIn("15m", mode)
        self.assertIn("EXIT-ONLY PASS (5-min)", exit_line)
        self.assertNotIn("15-min", exit_line)
        # A missing attribute must not fall back to the old 15-minute number.
        self.assertEqual(night_mode.resolve_cadence(None, None), (300, 1800))

    def test_banner_strings_are_gone_from_the_loop(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "master_bot.py",
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("15-min exits", src)
        self.assertNotIn("EXIT every 15m", src)
        self.assertNotIn("EXIT-ONLY PASS (15-min)", src)
        self.assertNotIn("nxt.time() >= meeting_start", src)
        self.assertNotIn("for _ in range(45)", src)
        self.assertNotIn('EXIT_INTERVAL_SECONDS", 900)', src)

    def test_yahoo_budget_is_one_pass(self):
        # 7 tech news + 3 macro news + 2 politics + 2 futures + 7 earnings.
        self.assertEqual(night_mode.yahoo_calls_per_harvest(config.TICKERS), 21)

    def test_daytime_boot_alerts_evening_boot_waits_for_harvest(self):
        self.assertFalse(
            night_mode.harvest_still_ahead(_et(2026, 9, 29, 10, 0), set())
        )
        self.assertTrue(
            night_mode.harvest_still_ahead(_et(2026, 9, 28, 20, 6), set())
        )


class TestCrumbBackoff(unittest.TestCase):
    def setUp(self):
        yf_client.reset_crumb_backoff_for_tests()

    def tearDown(self):
        yf_client.reset_crumb_backoff_for_tests()

    def test_429_backs_off_exponentially(self):
        first = yf_client.note_crumb_429()
        self.assertEqual(first, 15 * 60)
        self.assertGreater(yf_client.crumb_backoff_remaining(), 14 * 60)
        second = yf_client.note_crumb_429()
        self.assertEqual(second, 30 * 60)
        third = yf_client.note_crumb_429()
        self.assertEqual(third, 60 * 60)
        yf_client.note_crumb_ok()
        self.assertEqual(yf_client.crumb_backoff_remaining(), 0)
        self.assertEqual(yf_client.note_crumb_429(), 15 * 60)

    def test_suppressed_crumb_does_not_hit_the_network(self):
        yf_client.note_crumb_429()
        called = []

        def _boom(method, url, *args, **kwargs):
            called.append(url)
            raise AssertionError(url)

        with mock.patch.object(yf_client, "_original_session_request", _boom):
            response = yf_client.SESSION.get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb"
            )
        self.assertEqual(called, [])
        self.assertEqual(response.status_code, 429)
        self.assertIn("Too Many Requests", response.text)

    def test_non_crumb_request_still_uses_the_session(self):
        yf_client.note_crumb_429()
        sentinel = object()

        def _ok(method, url, *args, **kwargs):
            return sentinel

        with mock.patch.object(yf_client, "_original_session_request", _ok):
            got = yf_client.SESSION.get("https://query1.finance.yahoo.com/v8/finance/chart")
        self.assertIs(got, sentinel)

    def test_real_429_response_starts_the_backoff(self):
        class _Resp:
            status_code = 429
            url = "https://query1.finance.yahoo.com/v1/test/getcrumb"
            text = "Too Many Requests"

        def _limited(method, url, *args, **kwargs):
            return _Resp()

        with mock.patch.object(yf_client, "_original_session_request", _limited):
            yf_client.SESSION.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
        self.assertGreater(yf_client.crumb_backoff_remaining(), 14 * 60)

    def test_futures_and_earnings_skip_while_backing_off(self):
        import earnings_calendar_scraper as ecs
        import sector_scrapers

        yf_client.note_crumb_429()
        with mock.patch.object(sector_scrapers.yf, "Ticker") as futures:
            sector_scrapers.fetch_overnight_futures()
        futures.assert_not_called()
        with mock.patch.object(ecs.yf, "Ticker") as earnings:
            with mock.patch.object(ecs, "save_innovation_data") as save:
                outcomes = ecs.scrape_earnings_calendar(["NVDA", "AAPL"])
        earnings.assert_not_called()
        save.assert_not_called()
        self.assertEqual(outcomes, {"NVDA": "failed", "AAPL": "failed"})


if __name__ == "__main__":
    unittest.main()
