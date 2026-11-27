"""Shadow backup, pillars, post-exit quotes, blocked names, and quote quality.

Fixed rows only. No Yahoo calls.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

import config
import shadow_tape

CT = ZoneInfo("America/Chicago")
DAY = date(2026, 11, 16)


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 16, hour, minute, second, tzinfo=CT)


def _rows(db, sql, args=()):
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, args))
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _wait_tape() -> None:
    deadline = time.time() + 2
    while time.time() < deadline and shadow_tape._running:
        time.sleep(0.01)


class _TapeDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")

    def tearDown(self):
        _wait_tape()
        self.tmp.cleanup()


class QuoteAndPillarTests(_TapeDb):
    def test_spread_uses_mid_and_a_missing_bid_stays_null(self):
        row = shadow_tape.quote_from_row({"bid": 1.00, "ask": 1.10, "volume": 4})
        self.assertAlmostEqual(row["mid"], 1.05)
        self.assertAlmostEqual(row["spread_pct"], (0.10 / 1.05) * 100.0)
        self.assertEqual(row["volume"], 4.0)
        missing = shadow_tape.quote_from_row({"ask": 1.10, "volume": 0, "openInterest": 0})
        self.assertIsNone(missing["bid"])
        self.assertIsNone(missing["spread_pct"])
        self.assertIsNone(missing["mid"])
        self.assertEqual(missing["volume"], 0.0)
        self.assertEqual(missing["open_interest"], 0.0)

    def test_quote_age_is_null_unless_the_row_has_one(self):
        bare = shadow_tape.quote_from_row(
            {"bid": 1.0, "ask": 1.2, "lastTradeDate": 0},
            now=_at(10, 0),
        )
        self.assertIsNone(bare["quote_age_s"])
        aged = shadow_tape.quote_from_row({"bid": 1.0, "ask": 1.2, "quote_age_s": 12})
        self.assertEqual(aged["quote_age_s"], 12.0)

    def test_pillars_come_from_subscores_and_a_zero_liquidity_score_is_not_stored(self):
        class Card:
            technical_score = 40
            sentiment_score = 20
            liquidity_score = 0
            metrics = {
                "subscores": {
                    "T": 11,
                    "S": 7,
                    "pivot_sub": 3,
                    "drift_sub": None,
                    "vol_mult": 1,
                    "squawk_s": 0,
                }
            }

        parts = shadow_tape.components_from_card(Card())
        self.assertEqual(parts["t_score"], 11.0)
        self.assertEqual(parts["s_score"], 7.0)
        self.assertEqual(parts["pivot_sub"], 3.0)
        self.assertIsNone(parts["mom_sub"])
        self.assertIsNone(parts["drift_sub"])
        self.assertIsNone(parts["liq_mult"])
        self.assertEqual(parts["vol_mult"], 1.0)
        self.assertEqual(parts["squawk_s"], 0.0)

    def test_an_entry_stores_the_pillars_and_the_chain_row(self):
        class Card:
            liquidity_score = 0
            metrics = {"subscores": {"T": 11, "S": 7, "liq_mult": 0.5}}

        shadow_tape.note_entry(
            ticker="AAPL",
            direction="CALL",
            entry_at=_at(10, 0),
            components=shadow_tape.components_from_card(Card()),
            quote={
                "bid": 1.00,
                "ask": 1.10,
                "volume": 5,
                "openInterest": 9,
                "impliedVolatility": 0.25,
            },
            contract={"strike": 100, "expiration": "2026-11-20", "direction": "CALL"},
            db_path=self.db,
            trade_id="t1",
        )
        row = _rows(self.db, "SELECT * FROM shadow_entry_detail")[0]
        self.assertEqual(row["ticker"], "AAPL")
        self.assertEqual(row["trade_id"], "t1")
        self.assertEqual(row["t_score"], 11.0)
        self.assertEqual(row["liq_mult"], 0.5)
        self.assertAlmostEqual(row["spread_pct"], (0.10 / 1.05) * 100.0)
        self.assertIsNone(row["quote_age_s"])
        self.assertEqual(row["volume"], 5.0)
        self.assertEqual(row["open_interest"], 9.0)
        self.assertEqual(row["iv"], 0.25)
        self.assertIsNone(row["mom_sub"])


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self, name: str, count: int) -> str:
        path = os.path.join(self.tmp.name, name)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO rows (v) VALUES (?)",
            [(str(i),) for i in range(count)],
        )
        conn.commit()
        conn.close()
        return path

    def _count(self, path: str) -> int:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        finally:
            conn.close()

    def test_backup_and_restore_keep_the_row_counts(self):
        day = date(2026, 11, 26)
        hedge = self._db("hedge_fund.db", 3)
        news = self._db("news_room.db", 5)
        webull = self._db("webull_exec.db", 7)
        log = os.path.join(self.tmp.name, f"bot-{day.isoformat()}.log")
        with open(log, "w", encoding="utf-8") as handle:
            handle.write("session log\n")
        key = os.path.join(self.tmp.name, "secret.key")
        with open(key, "w", encoding="utf-8") as handle:
            handle.write("nope")
        folder = os.path.join(self.tmp.name, "backups")
        sources = [
            ("hedge_fund.db", hedge),
            ("news_room.db", news),
            ("webull_exec.db", webull),
            (f"bot-{day.isoformat()}.log", log),
        ]
        self.assertTrue(shadow_tape.run_backup(day, backup_dir=folder, sources=sources))
        dest = os.path.join(folder, day.isoformat())
        names = set(os.listdir(dest))
        self.assertEqual(
            names,
            {"hedge_fund.db", "news_room.db", "webull_exec.db", f"bot-{day.isoformat()}.log"},
        )
        self.assertNotIn(".env", names)
        self.assertFalse(any(name.endswith(".key") for name in names))
        for name, count in (("hedge_fund.db", 3), ("news_room.db", 5), ("webull_exec.db", 7)):
            restored = os.path.join(self.tmp.name, f"restored-{name}")
            shadow_tape.restore_db(os.path.join(dest, name), restored)
            self.assertEqual(self._count(restored), count)
        with open(os.path.join(dest, f"bot-{day.isoformat()}.log"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "session log\n")
        text = shadow_tape.backup_clause(day, backup_dir=folder, sources=sources, notify=False)
        self.assertEqual(text, "backup ok")
        self.assertNotIn("|", text)

    def test_retention_keeps_thirty_calendar_days_including_the_session(self):
        day = date(2026, 11, 26)
        folder = os.path.join(self.tmp.name, "backups")
        os.makedirs(folder)
        for name in ("2026-10-27", "2026-10-28", "keep-me"):
            os.makedirs(os.path.join(folder, name))
        with open(os.path.join(folder, "readme.txt"), "w", encoding="utf-8") as handle:
            handle.write("leave")
        hedge = self._db("hedge_fund.db", 1)
        shadow_tape.run_backup(
            day,
            backup_dir=folder,
            sources=[("hedge_fund.db", hedge)],
        )
        self.assertFalse(os.path.exists(os.path.join(folder, "2026-10-27")))
        self.assertTrue(os.path.isdir(os.path.join(folder, "2026-10-28")))
        self.assertTrue(os.path.isdir(os.path.join(folder, day.isoformat())))
        self.assertTrue(os.path.isdir(os.path.join(folder, "keep-me")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "readme.txt")))

    def test_an_env_file_is_refused(self):
        day = date(2026, 11, 26)
        env = os.path.join(self.tmp.name, ".env")
        with open(env, "w", encoding="utf-8") as handle:
            handle.write("BACKUP_DIR=/tmp/nope\n")
        folder = os.path.join(self.tmp.name, "backups")
        with self.assertRaises(RuntimeError):
            shadow_tape.run_backup(day, backup_dir=folder, sources=[(".env", env)])
        dest = os.path.join(folder, day.isoformat())
        if os.path.isdir(dest):
            self.assertNotIn(".env", os.listdir(dest))

    def test_a_missing_backup_dir_fails_and_creates_nothing(self):
        with mock.patch.object(shadow_tape, "_under_test", return_value=False), \
                mock.patch.object(shadow_tape, "_backup_dir_value", return_value=None), \
                mock.patch.object(shadow_tape, "_notify_backup_failure") as note, \
                mock.patch("os.makedirs") as made:
            text = shadow_tape.backup_clause(date(2026, 11, 26))
        self.assertEqual(text, "backup FAILED")
        self.assertNotIn("|", text)
        note.assert_called_once()
        made.assert_not_called()

    def test_rehearsal_does_not_read_the_env_or_post(self):
        news = os.path.join(self.tmp.name, "rehearsal-base", "news.db")
        with mock.patch.object(config, "NEWS_DB_PATH", news), \
                mock.patch.object(shadow_tape, "_under_test", return_value=False), \
                mock.patch.dict(os.environ, {"BACKUP_DIR": self.tmp.name}, clear=False), \
                mock.patch.object(shadow_tape, "_notify_backup_failure") as note, \
                mock.patch.object(shadow_tape, "run_backup") as copy:
            text = shadow_tape.backup_clause(date(2026, 11, 26))
        self.assertEqual(text, "backup FAILED")
        note.assert_not_called()
        copy.assert_not_called()

    def test_a_missing_database_is_a_failed_clause(self):
        text = shadow_tape.backup_clause(
            date(2026, 11, 26),
            backup_dir=os.path.join(self.tmp.name, "backups"),
            sources=[("hedge_fund.db", os.path.join(self.tmp.name, "missing.db"))],
            notify=False,
        )
        self.assertEqual(text, "backup FAILED")

    def test_a_slow_copy_still_returns_the_failed_token(self):
        def _slow(*_args, **_kwargs):
            time.sleep(0.3)
            return True

        with mock.patch.object(shadow_tape, "BACKUP_TIMEOUT_S", 0.05), \
                mock.patch.object(shadow_tape, "run_backup", side_effect=_slow):
            text = shadow_tape.backup_clause(
                date(2026, 11, 26),
                backup_dir=self.tmp.name,
                sources=[("hedge_fund.db", os.path.join(self.tmp.name, "x.db"))],
                notify=False,
            )
        self.assertEqual(text, "backup FAILED")
        time.sleep(0.4)

    def test_the_failure_note_is_not_critical_and_has_no_path(self):
        with mock.patch.object(shadow_tape, "_under_test", return_value=False), \
                mock.patch("broadcaster.send_discord_alert", return_value=True) as send:
            shadow_tape._notify_backup_failure(date(2026, 11, 26))
        body = send.call_args.args[0]
        self.assertFalse(send.call_args.kwargs.get("critical"))
        self.assertEqual(
            body,
            "SHADOW backup FAILED for 2026-11-26. "
            "The session line is stored. No .env or keys were copied.",
        )
        self.assertNotIn("/", body)


class PostExitTests(_TapeDb):
    def _pass(self, hour, minute, scored=None, closed=None, open_before=None, fetch=None):
        shadow_tape.on_exit_pass(
            _at(hour, minute),
            scored=scored,
            closed=closed,
            open_before=open_before,
            db_path=self.db,
            fetch=fetch,
        )

    def test_checkpoints_land_on_the_pass_nearest_the_target(self):
        calls = []
        quotes = [(2.40, 2.60), (1.40, 1.60), (2.10, 2.30), (1.70, 1.90)]

        def fetch(ticker, expiration, strike, direction):
            calls.append((ticker, expiration, strike, direction))
            bid, ask = quotes.pop(0)
            return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2.0}

        chain = {
            "chains": {
                "2026-11-20": {
                    "calls": [{
                        "strike": 100,
                        "bid": 1.90,
                        "ask": 2.10,
                        "volume": 8,
                        "openInterest": 12,
                        "impliedVolatility": 0.4,
                    }],
                    "puts": [],
                }
            }
        }
        trade = {
            "ticker": "AAPL",
            "direction": "CALL",
            "strike": 100,
            "expiration": "2026-11-20",
            "trade_id": "t1",
        }
        self._pass(
            10, 0,
            scored={"AAPL": {"options_dict": chain}},
            closed=[{"ticker": "AAPL", "reason": "STOP_LOSS"}],
            open_before=[trade],
            fetch=fetch,
        )
        self.assertEqual(calls, [])
        row = _rows(self.db, "SELECT * FROM shadow_post_exit")[0]
        self.assertAlmostEqual(row["exit_mid"], 2.0)
        self.assertIsNone(row["mid_15"])
        exit_row = _rows(self.db, "SELECT * FROM shadow_exit_detail")[0]
        self.assertAlmostEqual(exit_row["bid"], 1.90)
        self.assertAlmostEqual(exit_row["ask"], 2.10)
        self.assertAlmostEqual(exit_row["spread_pct"], (0.20 / 2.0) * 100.0)
        self.assertIsNone(exit_row["quote_age_s"])
        self.assertEqual(exit_row["volume"], 8.0)
        self.assertEqual(exit_row["open_interest"], 12.0)

        self._pass(10, 15, fetch=fetch)
        self._pass(10, 20, fetch=fetch)
        self.assertEqual(len(calls), 1)
        row = _rows(self.db, "SELECT * FROM shadow_post_exit")[0]
        self.assertAlmostEqual(row["mid_15"], 2.50)
        self.assertIsNone(row["mid_30"])

        self._pass(10, 30, fetch=fetch)
        self._pass(11, 0, fetch=fetch)
        self._pass(14, 45, fetch=fetch)
        self.assertEqual(len(calls), 4)
        row = _rows(self.db, "SELECT * FROM shadow_post_exit")[0]
        self.assertAlmostEqual(row["mid_30"], 1.50)
        self.assertAlmostEqual(row["mid_60"], 2.20)
        self.assertAlmostEqual(row["mid_1445"], 1.80)
        self.assertAlmostEqual(row["min_mid"], 1.50)
        self.assertAlmostEqual(row["max_mid"], 2.50)

        before = len(calls)
        self._pass(14, 55, fetch=fetch)
        self.assertEqual(len(calls), before)
        again = _rows(self.db, "SELECT min_mid, max_mid FROM shadow_post_exit")[0]
        self.assertAlmostEqual(again["min_mid"], 1.50)
        self.assertAlmostEqual(again["max_mid"], 2.50)

    def test_the_daily_cap_refuses_another_post_exit_fetch(self):
        shadow_tape.ensure_schema(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """
                INSERT INTO shadow_post_exit (
                    session_date, ticker, strike, expiration, exit_at, exit_mid
                ) VALUES ('2026-11-16', 'AAPL', 100, '2026-11-20',
                          '2026-11-16T16:00:00Z', 2.0)
                """
            )
            conn.execute(
                "INSERT INTO shadow_yahoo_extra (session_date, calls) VALUES ('2026-11-16', 150)"
            )

        fetched = []

        def fetch(*_args):
            fetched.append(True)
            return {"bid": 1.0, "ask": 1.2, "mid": 1.1}

        self._pass(10, 15, fetch=fetch)
        self.assertEqual(fetched, [])
        row = _rows(self.db, "SELECT mid_15, calls FROM shadow_post_exit, shadow_yahoo_extra")[0]
        self.assertIsNone(row["mid_15"])
        self.assertEqual(row["calls"], 150)


class BlockedTests(_TapeDb):
    def test_five_per_scan_and_the_chain_already_fetched(self):
        seen = []

        def select(options, pivot, atr, ticker):
            seen.append(ticker)
            return {
                "direction": "CALL",
                "strike": 100,
                "expiration": "2026-11-20",
                "ask": 1.25,
            }

        fetched = []

        def fetch(*_args):
            fetched.append(True)
            return {"bid": 1.0, "ask": 1.2, "mid": 1.1}

        decisions = [
            {"ticker": ticker, "score": score, "admit": False, "reason": "max_concurrent"}
            for ticker, score in (
                ("NVDA", 90),
                ("AAPL", 88),
                ("MSFT", 86),
                ("AMZN", 84),
                ("META", 82),
                ("GOOGL", 80),
                ("TSLA", 69),
                ("QQQ", 91),
            )
        ]
        # Admit stays out. Score 69 stays out. The cap keeps five.
        decisions.append({"ticker": "IWM", "score": 95, "admit": True, "reason": "admit"})
        shadow_tape.on_full_scan(
            _at(10, 0),
            scored={},
            decisions=decisions,
            db_path=self.db,
            fetch=fetch,
            select=select,
        )
        self.assertEqual(seen, ["QQQ", "NVDA", "AAPL", "MSFT", "AMZN"])
        rows = _rows(self.db, "SELECT ticker, score, block_reason, ask FROM shadow_blocked ORDER BY score DESC")
        self.assertEqual([row["ticker"] for row in rows], ["QQQ", "NVDA", "AAPL", "MSFT", "AMZN"])
        self.assertEqual(rows[0]["block_reason"], "max_concurrent")
        self.assertEqual(rows[0]["ask"], 1.25)
        self.assertFalse(_rows(self.db, "SELECT calls FROM shadow_yahoo_extra"))

        chain = {
            "chains": {
                "2026-11-20": {
                    "calls": [{"strike": 100, "bid": 1.10, "ask": 1.30}],
                    "puts": [],
                }
            }
        }
        shadow_tape.on_full_scan(
            _at(10, 30),
            scored={"QQQ": {"options_dict": chain}},
            decisions=[],
            db_path=self.db,
            fetch=fetch,
            select=select,
        )
        self.assertEqual(seen, ["QQQ", "NVDA", "AAPL", "MSFT", "AMZN"])
        self.assertEqual(fetched, [])
        marks = _rows(
            self.db,
            "SELECT bid, mid FROM shadow_blocked_marks WHERE mid IS NOT NULL",
        )
        self.assertEqual(len(marks), 1)
        self.assertAlmostEqual(marks[0]["bid"], 1.10)
        self.assertAlmostEqual(marks[0]["mid"], 1.20)

    def test_the_day_cap_is_fifteen_and_stops_after_1445(self):
        shadow_tape.ensure_schema(self.db)
        with sqlite3.connect(self.db) as conn:
            for index in range(15):
                conn.execute(
                    """
                    INSERT INTO shadow_blocked (
                        session_date, scanned_at, ticker, score, block_reason
                    ) VALUES ('2026-11-16', ?, ?, 80, 'seed')
                    """,
                    (f"2026-11-16T15:{index:02d}:00Z", f"T{index}"),
                )

        seen = []

        def select(*_args):
            seen.append(True)
            return None

        shadow_tape.on_full_scan(
            _at(11, 0),
            decisions=[{"ticker": "SPY", "score": 99, "admit": False, "reason": "day_cap"}],
            db_path=self.db,
            select=select,
        )
        self.assertEqual(seen, [])
        self.assertEqual(
            _rows(self.db, "SELECT COUNT(*) AS n FROM shadow_blocked")[0]["n"],
            15,
        )

    def test_a_scan_after_1445_does_not_add_a_name(self):
        seen = []

        def select(*_args):
            seen.append(True)
            return None

        shadow_tape.on_full_scan(
            _at(14, 50),
            decisions=[{"ticker": "SPY", "score": 90, "admit": False, "reason": "late"}],
            db_path=self.db,
            select=select,
        )
        self.assertEqual(seen, [])
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) AS n FROM shadow_blocked")[0]["n"], 0)

        seen = []

        def allow(options, pivot, atr, ticker):
            seen.append(ticker)
            return {"direction": "PUT", "strike": 50, "expiration": "2026-11-20", "ask": 0}

        shadow_tape.on_full_scan(
            _at(14, 45),
            decisions=[{"ticker": "SPY", "score": 90, "admit": False, "reason": "late_session"}],
            db_path=self.db,
            select=allow,
        )
        self.assertEqual(seen, ["SPY"])
        row = _rows(self.db, "SELECT ask, block_reason FROM shadow_blocked")[0]
        self.assertIsNone(row["ask"])
        self.assertEqual(row["block_reason"], "late_session")


class BudgetTests(unittest.TestCase):
    def test_the_published_budget_stays_under_the_cap_until_thirty_closes(self):
        self.assertEqual(shadow_tape.BLOCKED_CALLS, 0)
        self.assertEqual(shadow_tape.REGIME_CALLS, 54)
        self.assertEqual(shadow_tape.POST_EXIT_PER_CLOSE, 4)
        self.assertEqual(shadow_tape.DAILY_CAP, 150)
        self.assertLess(
            shadow_tape.REGIME_CALLS + 8 * shadow_tape.POST_EXIT_PER_CLOSE,
            shadow_tape.DAILY_CAP,
        )
        self.assertGreater(
            shadow_tape.REGIME_CALLS + 30 * shadow_tape.POST_EXIT_PER_CLOSE,
            shadow_tape.DAILY_CAP,
        )


class ReportTests(_TapeDb):
    def test_weekly_lines_are_marked_shadow(self):
        missing = shadow_tape.weekly_lines(os.path.join(self.tmp.name, "absent.db"))
        text = "\n".join(missing)
        self.assertIn("SHADOW POST-EXIT  (logged only; not a live rule)", text)
        self.assertIn("SHADOW BLOCKED  (logged only; not a live rule)", text)
        self.assertIn("SHADOW QUOTE QUALITY  (logged only; not a live rule)", text)
        self.assertIn("not on this disk yet.", text)

        shadow_tape.ensure_schema(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """
                INSERT INTO shadow_post_exit (
                    session_date, ticker, exit_at, exit_mid, mid_15, min_mid, max_mid
                ) VALUES ('2026-11-16', 'AAPL', '2026-11-16T16:00:00Z', 2, 2.5, 2, 2.5)
                """
            )
            conn.execute(
                """
                INSERT INTO shadow_blocked (
                    session_date, scanned_at, ticker, score, ask
                ) VALUES ('2026-11-16', '2026-11-16T16:00:00Z', 'QQQ', 90, 1.5)
                """
            )
            conn.execute(
                """
                INSERT INTO shadow_blocked_marks (blocked_id, scanned_at, bid, mid)
                VALUES (1, '2026-11-16T16:30:00Z', 1.2, 1.25)
                """
            )
            conn.execute(
                """
                INSERT INTO shadow_entry_detail (session_date, ticker, spread_pct)
                VALUES ('2026-11-16', 'AAPL', 4)
                """
            )
            conn.execute(
                """
                INSERT INTO shadow_exit_detail (session_date, ticker, spread_pct)
                VALUES ('2026-11-16', 'AAPL', 6)
                """
            )
        lines = shadow_tape.weekly_lines(self.db, now=_at(12, 0))
        text = "\n".join(lines)
        self.assertIn("closes tracked: 1", text)
        self.assertIn("+15 filled: 1", text)
        self.assertIn("mean mid range: 0.50", text)
        self.assertIn("candidates: 1", text)
        self.assertIn("with a later quote: 1", text)
        self.assertIn("mean ask at the block: 1.50", text)
        self.assertIn("entry quotes: 1  exit quotes: 1", text)
        self.assertIn("mean entry spread %: 4.00", text)
        self.assertIn("mean exit spread %: 6.00", text)


class SessionLineTests(_TapeDb):
    def test_backup_sits_between_regime_and_clean(self):
        import fill_accounting as fa
        import reliability
        import shadow_reconcile
        import virtual_broker as vb
        from tests.test_clean_session import _mark_day

        patches = [
            mock.patch.object(config, "NEWS_DB_PATH", self.db),
            mock.patch.object(vb, "DB_PATH", self.db),
            mock.patch("tracker_agent.load_active_trades", return_value=[]),
            mock.patch("broadcaster.send_discord_alert", return_value=True),
        ]
        for patch in patches:
            patch.start()
        try:
            vb.reset_book_for_tests()
            fa.reset_session_for_tests()
            sqlite3.connect(self.db).close()
            vb.ensure_ledger()
            _mark_day()
            shadow_reconcile.reconcile_session(_at(21, 0), holidays=())
        finally:
            for patch in reversed(patches):
                patch.stop()
            vb.reset_book_for_tests()
            fa.reset_session_for_tests()
        line = _rows(
            self.db,
            "SELECT line FROM session_lines WHERE kind='final'",
        )[0]["line"]
        self.assertLess(line.index("|regime:"), line.index("|backup "))
        self.assertLess(line.index("|backup "), line.index("|CLEAN "))
        self.assertIn("|CLEAN yes", line)
        backup = [part for part in line.split("|") if part.startswith("backup")]
        self.assertEqual(backup, ["backup FAILED"])
        self.assertNotIn("incomplete=1", line)
