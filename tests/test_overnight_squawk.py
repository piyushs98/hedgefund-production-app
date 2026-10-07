"""Session window for overnight First Squawk. No network."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytz

import news_memory
import night_mode
import pre_market_meeting
from scoring_engine import score_sentiment


EASTERN = pytz.timezone("America/New_York")


def _et(y, m, d, hh, mm, ss=0):
    return EASTERN.localize(datetime(y, m, d, hh, mm, ss))


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SquawkWindowTests(unittest.TestCase):
    def test_wednesday_session_starts_at_tuesday_close(self):
        now = _et(2026, 10, 7, 10, 0)
        self.assertEqual(now.weekday(), 2)
        start, end = night_mode.squawk_window(now, set())
        self.assertEqual(start, _et(2026, 10, 6, 16, 0))
        self.assertEqual(end, _et(2026, 10, 7, 16, 0))
        self.assertEqual(night_mode.sqlite_utc(start), "2026-10-06 20:00:00")

    def test_premarket_uses_the_same_window_as_the_open(self):
        pre = _et(2026, 10, 7, 9, 15)
        cash = _et(2026, 10, 7, 10, 0)
        self.assertEqual(night_mode.squawk_window(pre, set()), night_mode.squawk_window(cash, set()))

    def test_after_the_close_belongs_to_the_next_session(self):
        now = _et(2026, 10, 7, 18, 0)
        start, end = night_mode.squawk_window(now, set())
        self.assertEqual(start, _et(2026, 10, 7, 16, 0))
        self.assertEqual(end, _et(2026, 10, 8, 16, 0))

    def test_the_close_instant_opens_the_next_window(self):
        now = _et(2026, 10, 7, 16, 0)
        start, end = night_mode.squawk_window(now, set())
        self.assertEqual(start, _et(2026, 10, 7, 16, 0))
        self.assertEqual(end, _et(2026, 10, 8, 16, 0))

    def test_monday_morning_starts_at_friday_close(self):
        now = _et(2026, 10, 12, 9, 15)
        self.assertEqual(now.weekday(), 0)
        start, end = night_mode.squawk_window(now, set())
        self.assertEqual(start, _et(2026, 10, 9, 16, 0))
        self.assertEqual(end, _et(2026, 10, 12, 16, 0))

    def test_holiday_monday_keeps_friday_close(self):
        sunday = _et(2026, 9, 6, 20, 0)
        start, end = night_mode.squawk_window(sunday, {"2026-09-07"})
        self.assertEqual(start, _et(2026, 9, 4, 16, 0))
        self.assertEqual(end, _et(2026, 9, 8, 16, 0))

    def test_reads_happen_between_chunks_and_not_after_the_last(self):
        slept = []
        pulled = []
        beats = []

        def sleep(chunk):
            slept.append(chunk)

        def pull():
            pulled.append(len(slept))

        def beat():
            beats.append(len(slept))

        night_mode.sleep_night_chunks(
            [3600, 3600, 120],
            sleep,
            between=pull,
            after_each=beat,
        )
        self.assertEqual(slept, [3600, 3600, 120])
        self.assertEqual(pulled, [1, 2])
        self.assertEqual(beats, [1, 2, 3])

        pulled.clear()
        night_mode.sleep_night_chunks([60], sleep, between=pull)
        self.assertEqual(pulled, [])


class SquawkContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "news.db")
        self.db_patch = mock.patch.object(news_memory, "DB_PATH", self.db)
        self.brief_patch = mock.patch.object(pre_market_meeting, "NEWS_DB_PATH", self.db)
        self.db_patch.start()
        self.brief_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.brief_patch.stop)
        news_memory.init_db()
        start, end = night_mode.squawk_window(_et(2026, 10, 7, 10, 0), set())
        self.since = night_mode.sqlite_utc(start)
        self.until = night_mode.sqlite_utc(end)
        quiet = _utc_stamp(datetime.now(timezone.utc) - timedelta(days=10))
        with sqlite3.connect(self.db) as conn:
            conn.executemany(
                """
                INSERT INTO headlines (timestamp, ticker, sector, publisher, title)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("2026-10-06 20:00:00", "NVDA", "Squawk", "First Squawk",
                     "NVIDIA BEATS AND THE STOCK GAINS"),
                    ("2026-10-05 20:00:00", "NVDA", "Squawk", "First Squawk",
                     "NVIDIA MISSES AND SHARES PLUNGE"),
                    ("2026-10-07 19:59:59", "NVDA", "Squawk", "First Squawk",
                     "NVIDIA GAINS INTO THE CLOSE"),
                    ("2026-10-07 20:00:00", "NVDA", "Squawk", "First Squawk",
                     "NVIDIA DROPS ON A LAWSUIT"),
                    (quiet, "NVDA", "Tech", "Reuters",
                     "REUTERS NOTES A QUIET NVDA SESSION"),
                    ("2026-10-06 21:00:00", "FED", "Squawk", "First Squawk",
                     "FED WARNS ON INFLATION"),
                    ("2026-10-06 21:00:00", "AAPL", "Squawk", "First Squawk",
                     "APPLE BEATS AND GAINS"),
                ],
            )
            conn.execute(
                """
                INSERT INTO innovation_data (timestamp, ticker, source_tag, content)
                VALUES (?, ?, ?, ?)
                """,
                ("2026-10-09 21:30:00", "NVDA", "EARNINGS", "NVDA print 2026-10-28"),
            )
            conn.execute(
                """
                INSERT INTO innovation_data (timestamp, ticker, source_tag, content)
                VALUES (?, ?, ?, ?)
                """,
                ("2026-09-01 12:00:00", "NVDA", "EARNINGS", "STALE EARNINGS ROW"),
            )
            conn.commit()

    def test_score_sees_this_session_and_keeps_the_old_row_out(self):
        text = news_memory.get_historical_context(
            "NVDA",
            days=90,
            squawk_since=self.since,
            squawk_until=self.until,
        )
        self.assertIn("NVIDIA BEATS AND THE STOCK GAINS", text)
        self.assertIn("NVIDIA GAINS INTO THE CLOSE", text)
        self.assertIn("REUTERS NOTES A QUIET NVDA SESSION", text)
        self.assertNotIn("NVIDIA MISSES AND SHARES PLUNGE", text)
        self.assertNotIn("NVIDIA DROPS ON A LAWSUIT", text)
        self.assertNotIn("FED WARNS", text)
        self.assertNotIn("APPLE BEATS", text)
        _s, metrics, _reasons = score_sentiment(text)
        self.assertGreater(metrics["bullish_hits"], 0)
        self.assertEqual(metrics["bearish_hits"], 0)

    def test_without_a_window_the_old_squawk_row_is_still_stored(self):
        text = news_memory.get_historical_context("NVDA", days=90)
        self.assertIn("NVIDIA MISSES AND SHARES PLUNGE", text)
        self.assertIn("NVIDIA DROPS ON A LAWSUIT", text)

    def test_brief_starts_at_the_prior_close(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """
                INSERT INTO headlines (timestamp, ticker, sector, publisher, title)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("2026-10-09 21:00:00", "NVDA", "Squawk", "First Squawk",
                 "NVIDIA BEATS OVER THE WEEKEND"),
            )
            conn.execute(
                """
                INSERT INTO headlines (timestamp, ticker, sector, publisher, title)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("2026-10-09 19:00:00", "NVDA", "Squawk", "First Squawk",
                 "NVIDIA MISSES BEFORE THE CLOSE"),
            )
            conn.commit()
        text = pre_market_meeting.get_overnight_data(since_utc="2026-10-09 20:00:00")
        self.assertIn("NVIDIA BEATS OVER THE WEEKEND", text)
        self.assertIn("NVDA print 2026-10-28", text)
        self.assertNotIn("NVIDIA MISSES BEFORE THE CLOSE", text)
        self.assertNotIn("STALE EARNINGS ROW", text)
        self.assertNotIn("NVIDIA DROPS ON A LAWSUIT", text)


class EnsureNewsWindowTests(unittest.TestCase):
    def test_an_empty_window_does_not_call_yahoo(self):
        import master_bot

        def fake(ticker, days=90, **kwargs):
            if kwargs.get("squawk_since"):
                return ""
            return "- [old] NVIDIA BEATS (First Squawk)"

        with mock.patch.object(master_bot, "get_historical_context", side_effect=fake), \
                mock.patch.object(master_bot.yf, "Ticker") as ticker:
            text = master_bot.ensure_news_context("NVDA")
        self.assertEqual(text, "")
        ticker.assert_not_called()

    def test_an_in_window_line_is_what_the_score_receives(self):
        import master_bot

        def fake(ticker, days=90, **kwargs):
            self.assertIn("squawk_since", kwargs)
            self.assertIn("squawk_until", kwargs)
            return "- [now] NVIDIA BEATS (First Squawk)"

        with mock.patch.object(master_bot, "get_historical_context", side_effect=fake), \
                mock.patch.object(master_bot.yf, "Ticker") as ticker:
            text = master_bot.ensure_news_context("NVDA")
        self.assertIn("NVIDIA BEATS", text)
        ticker.assert_not_called()
