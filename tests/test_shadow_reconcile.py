"""SESSION_FINAL sits beside the 14:45 SESSION row. A second write updates it."""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import config
import fill_accounting as fa
import reliability
import shadow_context
import shadow_news
import shadow_reconcile
import virtual_broker as vb


def _iso(hour: int, minute: int) -> str:
    return datetime(2026, 11, 16, hour, minute, tzinfo=timezone.utc).isoformat()


class SessionFinalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.patches = [
            mock.patch.object(config, "NEWS_DB_PATH", self.db),
            mock.patch.object(vb, "DB_PATH", self.db),
            mock.patch("tracker_agent.load_active_trades", return_value=[]),
        ]
        for patch in self.patches:
            patch.start()
        vb.reset_book_for_tests()
        fa.reset_session_for_tests()
        vb.ensure_ledger()
        shadow_news.ensure_schema(self.db)
        self.sent = []

        def _send(message, critical=False):
            self.sent.append((str(message), bool(critical)))
            return True

        self.send_patch = mock.patch(
            "broadcaster.send_discord_alert", side_effect=_send
        )
        self.send_patch.start()

    def tearDown(self):
        self.send_patch.stop()
        for patch in reversed(self.patches):
            patch.stop()
        vb.reset_book_for_tests()
        fa.reset_session_for_tests()
        self.tmp.cleanup()

    def _seed_trade(self) -> None:
        meta = {"stop_loss": 4.0, "quantity": 1}
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """
                INSERT INTO trade_history
                    (closed_at, ticker, direction, strike, expiration,
                     entry_price, exit_price, pnl, contract_json, notes,
                     pnl_mid, pnl_fill)
                VALUES (?, 'META', 'CALL', 642, '2026-11-20',
                        5.0, 5.0, 0.0, ?, 'PAPER_BUY_OPEN', 0.0, 0.0)
                """,
                (_iso(17, 30), json.dumps(meta)),
            )
            conn.execute(
                """
                INSERT INTO trade_history
                    (closed_at, ticker, direction, strike, expiration,
                     entry_price, exit_price, pnl, contract_json, notes,
                     pnl_mid, pnl_fill)
                VALUES (?, 'META', 'CALL', 642, '2026-11-20',
                        5.0, 0.35, -465.0, ?, 'EXIT:STOP_LOSS', -465.0, -470.0)
                """,
                (_iso(18, 30), json.dumps(meta)),
            )
            conn.execute(
                """
                INSERT INTO calendar_events
                    (event_id, name, release_at, keywords, kind, ticker, tier)
                VALUES ('rehearsal-cpi', 'Consumer Price Index',
                        '2026-11-16T18:00:00+00:00', '["CPI"]', 'macro', NULL, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO shadow_items
                    (source, ticker, event_id, title, url, published_at,
                     fetched_at, raw_text, content_hash, actual, forecast,
                     prior, component)
                VALUES ('firstsquawk', NULL, 'rehearsal-cpi', 'cpi',
                        'https://x.com/FirstSquawk/status/1',
                        '2026-11-16T18:01:00+00:00',
                        '2026-11-16T18:01:00+00:00',
                        'US CPI (MOM) ACTUAL: 0.2% VS 0.5% PREVIOUS; EST 0.4%',
                        'hash-cpi-mom', '0.2%', '0.4%', '0.5%', 'headline_mom')
                """
            )

    def test_final_sits_beside_session_and_a_second_call_does_not_duplicate(self):
        self._seed_trade()
        original = (
            "SESSION|v91c6693|2026-11-16|scans 1|entries 0|closes 0|"
            "realized_mid +0|realized_fill +0|spread_cost 0|R_fill n/a|"
            "equity_fill 10000|peak_deployed 0|open_value 500|spy_open n/a|"
            "spy_close n/a|spy_range_pct n/a|criticals 0|bp 10000|"
            "chain=blocked|news=blocked|futures=blocked|earnings=none"
        )
        self.assertTrue(reliability.persist_session_line(original))
        when = datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc)
        shadow_reconcile.reconcile_session(when, holidays=())
        shadow_reconcile.reconcile_session(when, holidays=())
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT kind, line, incomplete FROM session_lines
                WHERE session_date=?
                ORDER BY kind
                """,
                ("2026-11-16",),
            ).fetchall()
            tags = conn.execute(
                "SELECT trade_id, event_id, actual, surprise, label FROM shadow_close_tags"
            ).fetchall()
        self.assertEqual([row["kind"] for row in rows], ["final", "session"])
        session = next(row for row in rows if row["kind"] == "session")
        final = next(row for row in rows if row["kind"] == "final")
        self.assertEqual(session["line"], original)
        self.assertTrue(final["line"].startswith("SESSION_FINAL|"))
        self.assertIn("closes 1", final["line"])
        self.assertIn("entries 1", final["line"])
        self.assertIn("open_value 0", final["line"])
        self.assertEqual(int(final["incomplete"]), 0)
        self.assertNotIn("incomplete=1", final["line"])
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0]["event_id"], "rehearsal-cpi")
        self.assertEqual(tags[0]["actual"], "0.2%")
        self.assertEqual(tags[0]["label"], "COLD")
        self.assertEqual(tags[0]["surprise"], "-0.2")
        posted = [body for body, critical in self.sent if body.startswith("SESSION_FINAL|")]
        self.assertEqual(len(posted), 1)
        shadow_context.record_close_tags(when.date(), db_path=self.db)
        with sqlite3.connect(self.db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM shadow_close_tags").fetchone()[0]
        self.assertEqual(count, 1)

    def test_open_book_is_incomplete_and_a_later_flat_book_updates_that_row(self):
        when = datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc)
        with mock.patch("tracker_agent.load_active_trades", return_value=[{"ticker": "IWM"}]):
            shadow_reconcile.reconcile_session(when, holidays=())
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                """
                SELECT line, incomplete FROM session_lines
                WHERE session_date=? AND kind='final'
                """,
                ("2026-11-16",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[1]), 1)
        self.assertIn("incomplete=1", row[0])
        self.assertTrue(any(critical for _body, critical in self.sent))
        before = len(self.sent)
        with mock.patch("tracker_agent.load_active_trades", return_value=[]):
            shadow_reconcile.reconcile_session(when, holidays=())
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT kind, incomplete FROM session_lines WHERE session_date=?",
                ("2026-11-16",),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "final")
        self.assertEqual(int(rows[0][1]), 0)
        self.assertGreater(len(self.sent), before)

    def test_weekend_holiday_and_pre_flatten_write_nothing(self):
        saturday = datetime(2026, 11, 14, 21, 0, tzinfo=timezone.utc)
        holiday = datetime(2026, 11, 26, 21, 0, tzinfo=timezone.utc)
        morning = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)
        shadow_reconcile.reconcile_session(saturday, holidays=())
        shadow_reconcile.reconcile_session(holiday, holidays={"2026-11-26"})
        shadow_reconcile.reconcile_session(morning, holidays=())
        self.assertFalse(os.path.exists(self.db) and self._final_rows())
        self.assertEqual(self.sent, [])

    def test_a_failure_sends_one_noncritical_note(self):
        when = datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc)
        with mock.patch.object(
            shadow_reconcile, "_ledger_totals", side_effect=RuntimeError("boom")
        ):
            shadow_reconcile.reconcile_session(when, holidays=())
        self.assertEqual(len(self.sent), 1)
        body, critical = self.sent[0]
        self.assertFalse(critical)
        self.assertIn("SESSION_FINAL reconciliation failed", body)
        self.assertIn("boom", body)

    def _final_rows(self) -> int:
        if not os.path.exists(self.db):
            return 0
        with sqlite3.connect(self.db) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "session_lines" not in names:
                return 0
            return conn.execute(
                "SELECT COUNT(*) FROM session_lines WHERE kind='final'"
            ).fetchone()[0]
