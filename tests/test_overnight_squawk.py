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


class WallClockNightWaitTests(unittest.TestCase):
    def test_wake_past_the_target_does_not_finish_the_old_sleep(self):
        start = _et(2026, 10, 6, 20, 0)
        target = night_mode.next_premarket(start, set())
        state = {"now": start}
        slept = []
        hours = []

        def sleep(secs):
            self.assertLessEqual(secs, 60)
            slept.append(secs)
            state["now"] = target + timedelta(minutes=5)

        night_mode.sleep_until_premarket(
            start,
            sleep,
            holidays=set(),
            now_fn=lambda: state["now"],
            on_tick=lambda: hours.append("tick"),
            on_hour=lambda: hours.append("hour"),
        )
        self.assertEqual(len(slept), 1)
        self.assertEqual(hours, [])

    def test_multi_hour_jump_reads_once_and_not_after_arrival(self):
        start = _et(2026, 10, 6, 20, 0)
        target = night_mode.next_premarket(start, set())
        state = {"now": start}
        slept = []
        hours = []
        calls = {"n": 0}

        def sleep(secs):
            self.assertLessEqual(secs, 60)
            slept.append(secs)
            calls["n"] += 1
            if calls["n"] == 1:
                state["now"] = start + timedelta(hours=5)
            else:
                state["now"] = target

        night_mode.sleep_until_premarket(
            start,
            sleep,
            holidays=set(),
            now_fn=lambda: state["now"],
            on_hour=lambda: hours.append(state["now"]),
        )
        self.assertEqual(len(hours), 1)
        self.assertEqual(len(slept), 2)
        self.assertLess(hours[0], target)

    def test_sixty_second_ticks_do_not_read_every_tick(self):
        start = _et(2026, 10, 7, 8, 0)
        target = night_mode.next_premarket(start, set())
        self.assertEqual(target, _et(2026, 10, 7, 9, 15))
        state = {"now": start}
        slept = []
        hours = []
        ticks = []

        def sleep(secs):
            self.assertLessEqual(secs, 60)
            slept.append(secs)
            state["now"] = state["now"] + timedelta(seconds=secs)

        night_mode.sleep_until_premarket(
            start,
            sleep,
            holidays=set(),
            now_fn=lambda: state["now"],
            on_tick=lambda: ticks.append(state["now"]),
            on_hour=lambda: hours.append(state["now"]),
        )
        self.assertEqual(len(hours), 1)
        self.assertEqual(hours[0], _et(2026, 10, 7, 9, 0))
        self.assertEqual(len(slept), 75)
        self.assertEqual(len(ticks), 74)
        self.assertGreater(len(slept), len(hours))


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

    def test_the_ninety_day_read_still_returns_stored_squawk_rows(self):
        text = news_memory.get_historical_context("NVDA", days=90)
        self.assertIn("NVIDIA BEATS AND THE STOCK GAINS", text)
        self.assertIn("NVIDIA MISSES AND SHARES PLUNGE", text)
        self.assertIn("REUTERS NOTES A QUIET NVDA SESSION", text)
        _s, metrics, _reasons = score_sentiment(text)
        self.assertGreater(metrics["bullish_hits"], 0)
        self.assertGreater(metrics["bearish_hits"], 0)

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
    def test_squawk_only_book_does_not_call_yahoo_and_stays_out_of_the_score(self):
        import master_bot
        import squawk_shadow

        start, end = night_mode.squawk_window(None, master_bot.NYSE_HOLIDAYS)
        stamp = night_mode.sqlite_utc(start + (end - start) / 2)
        line = f"- [{stamp}] NVIDIA BEATS AND THE STOCK GAINS (First Squawk)"

        with mock.patch.object(master_bot, "get_historical_context", return_value=line), \
                mock.patch.object(master_bot.yf, "Ticker") as ticker:
            ticker.return_value.news = []
            text = master_bot.ensure_news_context("NVDA")
        self.assertEqual(text, "No recent headlines or historical news found.")
        self.assertNotIn("NVIDIA BEATS", text)
        ticker.assert_not_called()
        staged = squawk_shadow.take("NVDA")
        self.assertIn("NVIDIA BEATS", staged)

    def test_other_publishers_score_and_an_old_squawk_line_is_not_staged(self):
        import master_bot
        import squawk_shadow

        start, _end = night_mode.squawk_window(None, master_bot.NYSE_HOLIDAYS)
        inside = night_mode.sqlite_utc(start + timedelta(hours=1))
        text = "\n".join([
            "- [2020-01-01 00:00:00] NVIDIA MISSES AND SHARES PLUNGE (First Squawk)",
            f"- [{inside}] NVIDIA BEATS AND THE STOCK GAINS (First Squawk)",
            "- [2026-10-01 12:00:00] REUTERS NOTES A QUIET NVDA SESSION (Reuters)",
        ])

        with mock.patch.object(master_bot, "get_historical_context", return_value=text), \
                mock.patch.object(master_bot.yf, "Ticker") as ticker:
            scored = master_bot.ensure_news_context("NVDA")
        ticker.assert_not_called()
        self.assertIn("REUTERS NOTES", scored)
        self.assertNotIn("NVIDIA", scored)
        staged = squawk_shadow.take("NVDA")
        self.assertIn("NVIDIA BEATS", staged)
        self.assertNotIn("NVIDIA MISSES", staged)
