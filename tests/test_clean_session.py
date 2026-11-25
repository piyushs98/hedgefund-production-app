"""CLEAN on SESSION_FINAL follows the 2026-11-25 extension rules."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import config
import fill_accounting as fa
import reliability
import shadow_reconcile
import virtual_broker as vb

CT = ZoneInfo("America/Chicago")
DAY = datetime(2026, 11, 16, 12, 0, tzinfo=CT).date()


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 16, hour, minute, tzinfo=CT)


def _mark_day(*, scans: int = 12, zero_fetch: int = 0, skip_exits=()) -> None:
    zeros_left = zero_fetch
    for index in range(scans):
        minute = 8 * 60 + 45 + 30 * index
        fetched = 10
        if zeros_left:
            fetched = 0
            zeros_left -= 1
        reliability.note_completed_scan(_at(minute // 60, minute % 60), fetched, 10)
    minute = 8 * 60 + 30
    end = 14 * 60 + 55
    skipped = set(skip_exits)
    while minute <= end:
        if minute not in skipped:
            reliability.note_exit_pass(_at(minute // 60, minute % 60))
        minute += 5
    reliability.seal_ledger()


class CleanSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.patch.start()
        sqlite3.connect(self.db).close()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_twelve_scans_and_tight_exits_are_clean(self):
        _mark_day()
        self.assertEqual(reliability.clean_field(DAY), "CLEAN yes")

    def test_a_fifteen_minute_exit_gap_still_passes(self):
        _mark_day(skip_exits={8 * 60 + 35, 8 * 60 + 40})
        self.assertEqual(reliability.clean_field(DAY), "CLEAN yes")

    def test_a_short_scan_count_fails(self):
        _mark_day(scans=11)
        text = reliability.clean_field(DAY)
        self.assertIn("CLEAN no scans 11/12", text)
        self.assertNotIn("exit gap", text)

    def test_an_exit_gap_over_fifteen_minutes_fails(self):
        _mark_day(skip_exits={10 * 60, 10 * 60 + 5, 10 * 60 + 10})
        text = reliability.clean_field(DAY)
        self.assertIn("CLEAN no exit gap 20 min 09:55-10:15", text)
        self.assertNotIn("scans ", text)

    def test_a_zero_of_ten_scan_fails(self):
        _mark_day(zero_fetch=1)
        self.assertEqual(reliability.clean_field(DAY), "CLEAN no scan 0/10 x1")

    def test_a_ledger_edit_after_the_seal_fails(self):
        _mark_day()
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """CREATE TABLE trade_history (
                    id INTEGER PRIMARY KEY,
                    closed_at TEXT,
                    ticker TEXT,
                    notes TEXT,
                    pnl REAL
                )"""
            )
            conn.execute(
                """INSERT INTO trade_history
                       (closed_at, ticker, notes, pnl)
                   VALUES ('2026-11-16T15:00:00+00:00', 'GOOGL', 'EXIT:STOP_LOSS', -1)"""
            )
        text = reliability.clean_field(DAY)
        self.assertIn("ledger edited after the last bot mark", text)

    def test_a_missing_day_is_not_clean(self):
        text = reliability.clean_field(DAY)
        self.assertIn("scans 0/12", text)
        self.assertIn("exit gap 385 min 08:30-14:55", text)
        self.assertIn("no bot seal", text)

    def test_rehearsal_against_the_live_database_is_named(self):
        _mark_day()
        with mock.patch.object(reliability, "_rehearsal_hitting_live_db", return_value=True):
            text = reliability.clean_field(DAY)
        self.assertIn("simulated edit of the production database", text)

    def test_session_final_carries_clean_and_the_session_line_does_not(self):
        patches = [
            mock.patch.object(vb, "DB_PATH", self.db),
            mock.patch("tracker_agent.load_active_trades", return_value=[]),
            mock.patch("broadcaster.send_discord_alert", return_value=True),
        ]
        for patch in patches:
            patch.start()
        try:
            vb.reset_book_for_tests()
            fa.reset_session_for_tests()
            vb.ensure_ledger()
            _mark_day()
            when = datetime(2026, 11, 16, 21, 0, tzinfo=CT)
            shadow_reconcile.reconcile_session(when, holidays=())
        finally:
            for patch in reversed(patches):
                patch.stop()
            vb.reset_book_for_tests()
            fa.reset_session_for_tests()
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT kind, line FROM session_lines WHERE session_date=?",
                (DAY.isoformat(),),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], ["final"])
        self.assertIn("|CLEAN yes", rows[0][1])
        self.assertTrue(rows[0][1].startswith("SESSION_FINAL|"))
