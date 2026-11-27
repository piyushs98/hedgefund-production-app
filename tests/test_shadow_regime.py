"""Shadow regime math on fixed bars. No Yahoo calls."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import config
import shadow_regime

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 16, 15, 15, tzinfo=timezone.utc)  # 09:15 CT


def _bar(hour, minute, **kwargs):
    row = {
        "ts": datetime(2026, 11, 16, hour, minute, tzinfo=CT),
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": None,
    }
    row.update(kwargs)
    return row


def _read(db, sql, args=()):
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


class RegimeMathTests(unittest.TestCase):
    def test_wilder_atr_is_the_mean_of_the_first_fourteen_true_ranges(self):
        bars = [(10, 10, 10)] + [(11, 9, 10)] * 14
        self.assertEqual(shadow_regime.wilder_atr(bars), 2.0)

    def test_wilder_atr_smooths_the_next_true_range(self):
        bars = [(10, 10, 10)] + [(11, 9, 10)] * 14 + [(26, 10, 10)]
        self.assertEqual(shadow_regime.wilder_atr(bars), 3.0)

    def test_wilder_atr_is_null_when_the_series_is_short_or_broken(self):
        self.assertIsNone(shadow_regime.wilder_atr([(11, 9, 10)] * 14))
        broken = [(10, 10, 10)] + [(11, 9, 10)] * 14
        broken[3] = (11, None, 10)
        self.assertIsNone(shadow_regime.wilder_atr(broken))
        inverted = [(10, 10, 10)] + [(11, 9, 10)] * 14
        inverted[4] = (9, 11, 10)
        self.assertIsNone(shadow_regime.wilder_atr(inverted))

    def test_vwap_skips_a_bar_with_no_volume(self):
        bars = [
            _bar(8, 30, high=10, low=8, close=9, volume=100),
            _bar(8, 31, high=12, low=10, close=11, volume=100),
            _bar(8, 32, high=20, low=0, close=10, volume=None),
            _bar(8, 33, high=20, low=0, close=10, volume=0),
        ]
        self.assertEqual(shadow_regime.session_vwap(bars), 10.0)

    def test_equal_price_and_vwap_has_no_side(self):
        bars = [_bar(8, 30, open=10, high=10, low=8, close=9, volume=100)]
        row = shadow_regime.measure(
            bars, [], [], None,
            now=datetime(2026, 11, 16, 8, 31, tzinfo=CT),
            atr=None,
            prior_close=None,
        )
        self.assertEqual(row["vwap"], 9)
        self.assertIsNone(row["vwap_side"])
        self.assertEqual(row["vwap_distance_pct"], 0)
        self.assertIsNone(shadow_regime.aligned_with_vwap("CALL", row["vwap_side"]))

    def test_opening_range_excludes_the_09_00_bar_and_waits_until_09_00(self):
        bars = [
            _bar(8, 30, open=100, high=10, low=9, close=9.5, volume=1),
            _bar(8, 45, open=11, high=12, low=10, close=11, volume=1),
            _bar(9, 0, open=13, high=20, low=12, close=13, volume=1),
        ]
        early = shadow_regime.measure(
            bars, [], [], None,
            now=datetime(2026, 11, 16, 8, 50, tzinfo=CT),
            atr=None,
            prior_close=None,
        )
        self.assertIsNone(early["or_status"])
        self.assertIsNone(early["or_high"])
        row = shadow_regime.measure(
            bars, [], [], None,
            now=datetime(2026, 11, 16, 9, 15, tzinfo=CT),
            atr=None,
            prior_close=None,
        )
        self.assertEqual(row["or_high"], 12)
        self.assertEqual(row["or_low"], 9)
        self.assertEqual(row["spy_price"], 13)
        self.assertEqual(row["or_status"], "above")

    def test_the_open_is_the_08_30_bar_and_not_the_next_minute(self):
        import pandas as pd

        missing = pd.DataFrame(
            {"Open": [101.0], "High": [101.0], "Low": [101.0], "Close": [101.0], "Volume": [1]},
            index=pd.DatetimeIndex([datetime(2026, 11, 16, 9, 31)]),
        )
        bars = shadow_regime.bars_from_frame(missing)
        self.assertEqual(bars[0]["ts"].hour, 8)
        self.assertEqual(bars[0]["ts"].minute, 31)
        row = shadow_regime.measure(
            bars, [], [], None,
            now=datetime(2026, 11, 16, 9, 15, tzinfo=CT),
            atr=None,
            prior_close=None,
        )
        self.assertIsNone(row["spy_open"])
        self.assertIsNone(row["spy_move_pct"])

        present = pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [100.4, 101.2],
                "Low": [99.8, 100.6],
                "Close": [100.2, 101.0],
                "Volume": [5, 5],
            },
            index=pd.DatetimeIndex([
                datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc),
                datetime(2026, 11, 16, 15, 15, tzinfo=timezone.utc),
            ]),
        )
        row = shadow_regime.measure(
            shadow_regime.bars_from_frame(present), [], [], None,
            now=NOW,
            atr=2,
            prior_close=98,
        )
        self.assertEqual(row["spy_open"], 100.0)
        self.assertEqual(row["spy_price"], 101.0)
        self.assertAlmostEqual(row["spy_move_pct"], 1.0)
        self.assertAlmostEqual(row["spy_move_atr"], 0.5)
        self.assertAlmostEqual(row["gap_pct"], (100 - 98) / 98 * 100)
        self.assertEqual(row["gap_filled"], 0)

    def test_a_gap_fills_only_when_price_touches_the_prior_close(self):
        pct, filled = shadow_regime.gap_state(100, 98, 101, 99)
        self.assertAlmostEqual(pct, (100 - 98) / 98 * 100)
        self.assertFalse(filled)
        _pct, filled = shadow_regime.gap_state(100, 98, 101, 97.5)
        self.assertTrue(filled)
        _pct, filled = shadow_regime.gap_state(100, 100, 101, 99)
        self.assertTrue(filled)
        self.assertEqual(shadow_regime.gap_state(100, None, 101, 99), (None, None))

    def test_day_labels_use_the_fixed_rules(self):
        self.assertEqual(shadow_regime.label_day(100, 102, 99, 101.5)[0], "normal")
        self.assertEqual(shadow_regime.label_day(100, 101.2, 100, 101.1)[0], "trend")
        self.assertEqual(shadow_regime.label_day(100, 100.5, 100.2, 100.3)[0], "chop")
        self.assertEqual(shadow_regime.label_day(100, 102, 98, 100.2)[0], "chop")
        self.assertEqual(shadow_regime.label_day(100, 100, 100, 100)[0], "chop")
        self.assertEqual(
            shadow_regime.label_day(100, 102, 98, None),
            (None, None, None),
        )
        self.assertEqual(shadow_regime.label_day(100, 101, 100, 100.6)[0], "trend")
        self.assertEqual(shadow_regime.label_day(100, 100.8, 100, 100.24)[0], "normal")

    def test_alignment_flags(self):
        self.assertIsNone(shadow_regime.aligned_with_spy_move("CALL", 0.09))
        self.assertIsNone(shadow_regime.aligned_with_spy_move("CALL", None))
        self.assertTrue(shadow_regime.aligned_with_spy_move("CALL", 0.1))
        self.assertFalse(shadow_regime.aligned_with_spy_move("PUT", 0.1))
        self.assertTrue(shadow_regime.aligned_with_spy_move("put", -0.2))
        self.assertTrue(shadow_regime.aligned_with_vwap("CALL", "above"))
        self.assertFalse(shadow_regime.aligned_with_vwap("CALL", "below"))
        self.assertIsNone(shadow_regime.aligned_with_vwap("CALL", None))
        self.assertTrue(shadow_regime.or_breakout_aligned("CALL", "above"))
        self.assertFalse(shadow_regime.or_breakout_aligned("CALL", "inside"))
        self.assertTrue(shadow_regime.or_breakout_aligned("PUT", "below"))
        self.assertFalse(shadow_regime.or_breakout_aligned("PUT", "above"))
        self.assertIsNone(shadow_regime.or_breakout_aligned("CALL", None))

    def test_breadth_is_null_until_a_ticker_is_observed(self):
        empty = shadow_regime.measure(
            [], [], [], None, now=NOW, atr=None, prior_close=None,
        )
        self.assertIsNone(empty["breadth_up"])
        self.assertIsNone(empty["breadth_down"])
        self.assertIsNone(empty["vix"])
        self.assertIsNone(empty["vix1d"])
        quotes = {"AAPL": {"open": 10, "price": 11}, "MSFT": {"open": 10, "price": 9}}
        spy = [
            _bar(8, 30, open=100, high=101, low=99, close=100, volume=10),
            _bar(9, 15, open=101, high=101, low=99, close=101, volume=10),
        ]
        quotes["QQQ"] = {"open": 100, "price": 99}
        quotes["IWM"] = {"open": 100, "price": 101}
        row = shadow_regime.measure(
            spy, [], [], quotes, now=NOW, atr=None, prior_close=None,
            tickers=list(config.TICKERS),
        )
        self.assertEqual(row["breadth_up"], 2)
        self.assertEqual(row["breadth_down"], 2)
        self.assertEqual(row["qqq_vs_spy"], "opposite")
        self.assertEqual(row["iwm_vs_spy"], "same")

    def test_vix_open_is_not_taken_from_a_later_bar(self):
        late = [_bar(8, 31, open=15, high=16, low=15, close=16, volume=1)]
        row = shadow_regime.measure(
            [], late, [], None, now=NOW, atr=None, prior_close=None,
        )
        self.assertEqual(row["vix"], 16)
        self.assertIsNone(row["vix_open"])
        self.assertIsNone(row["vix_change"])
        both = [
            _bar(8, 30, open=15, high=15, low=15, close=15, volume=1),
            _bar(9, 15, open=16, high=16, low=16, close=16, volume=1),
        ]
        row = shadow_regime.measure(
            [], both, [], None, now=NOW, atr=None, prior_close=None,
        )
        self.assertEqual(row["vix"], 16)
        self.assertEqual(row["vix_open"], 15)
        self.assertEqual(row["vix_change"], 1)

    def test_spark_uses_the_08_30_open(self):
        open_ts = int(datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc).timestamp())
        later = int(datetime(2026, 11, 16, 15, 15, tzinfo=timezone.utc).timestamp())
        payload = {
            "spark": {
                "result": [{
                    "symbol": "QQQ",
                    "response": [{
                        "timestamp": [open_ts, later],
                        "indicators": {
                            "quote": [{
                                "open": [400.0, 401.0],
                                "high": [400.5, 402.0],
                                "low": [399.5, 400.5],
                                "close": [400.2, 401.5],
                                "volume": [10, 10],
                            }]
                        },
                    }],
                }]
            }
        }
        quotes = shadow_regime.quotes_from_spark(payload, NOW)
        self.assertEqual(quotes["QQQ"]["open"], 400.0)
        self.assertEqual(quotes["QQQ"]["price"], 401.5)
        self.assertIsNone(shadow_regime.quotes_from_spark({"spark": {}}, NOW))


class RegimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "news.db")

    def tearDown(self):
        deadline = time.perf_counter() + 2
        while shadow_regime._scan_token is not None and time.perf_counter() < deadline:
            time.sleep(0.02)

    def test_scan_budget_is_four_calls(self):
        calls = []

        def history(symbol, **_kwargs):
            calls.append(("history", symbol))
            return []

        def batch(symbols):
            calls.append(("batch", tuple(symbols)))
            return {}

        with mock.patch("yf_client.download", side_effect=AssertionError("download")):
            with mock.patch("yf_client.ticker", side_effect=AssertionError("ticker")):
                payload = shadow_regime._fetch_scan(NOW, history=history, batch=batch)
        self.assertEqual(
            [item[0] for item in calls],
            ["history", "history", "history", "batch"],
        )
        self.assertEqual([item[1] for item in calls[:3]], ["SPY", "^VIX", "^VIX1D"])
        self.assertEqual(set(calls[3][1]), set(config.TICKERS))
        self.assertEqual(len(calls[3][1]), 10)
        self.assertEqual(len(payload["health"]), 4)
        self.assertTrue(all(item["status"] == "fail" for item in payload["health"]))

    def test_the_default_path_does_not_call_yahoo_from_the_suite(self):
        called = []

        def boom(*_args, **_kwargs):
            called.append(1)
            raise AssertionError("yahoo")

        moment = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
        with mock.patch("yf_client.ticker", side_effect=boom):
            shadow_regime.mark_full_scan(moment, db_path=self.db)
            shadow_regime.ensure_reference(moment, db_path=self.db)
        self.assertEqual(called, [])
        self.assertFalse(os.path.exists(self.db))

    def test_a_failed_fetch_still_writes_one_null_row(self):
        def fetch(_now):
            raise RuntimeError("nope")

        shadow_regime.mark_full_scan(NOW, db_path=self.db, fetch=fetch, timeout_s=2)
        rows = _read(self.db, "SELECT * FROM regime_marks")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["spy_price"])
        self.assertIsNone(rows[0]["spy_move_pct"])
        self.assertIsNone(rows[0]["vix1d"])
        health = _read(
            self.db,
            "SELECT source, status, detail, slot FROM source_health",
        )
        self.assertEqual(health[0]["source"], "yahoo_shadow")
        self.assertEqual(health[0]["status"], "fail")
        self.assertEqual(health[0]["detail"], "RuntimeError")
        self.assertNotIn("nope", health[0]["detail"])

    def test_a_slow_fetch_returns_before_the_worker_finishes(self):
        def fetch(_now):
            time.sleep(0.4)
            return {
                "spy_bars": [
                    _bar(8, 30, open=100, high=101, low=99, close=100.5, volume=10)
                ],
                "vix_bars": [],
                "vix1d_bars": [],
                "quotes": {},
                "health": [],
            }

        started = time.perf_counter()
        shadow_regime.mark_full_scan(
            datetime(2026, 11, 16, 9, 15, tzinfo=CT),
            db_path=self.db,
            fetch=fetch,
            timeout_s=0.05,
        )
        self.assertLess(time.perf_counter() - started, 0.3)
        row = None
        deadline = time.perf_counter() + 2
        while time.perf_counter() < deadline:
            found = _read(self.db, "SELECT spy_price FROM regime_marks")
            if found:
                row = found[0]
                break
            time.sleep(0.02)
        self.assertIsNotNone(row)
        self.assertEqual(row["spy_price"], 100.5)

    def test_reference_is_stored_once_and_retried_only_after_thirty_minutes(self):
        calls = []

        def fetch(_day):
            calls.append(1)
            return {"prior_close": 100.0, "atr_14": 2.0}

        moment = datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)
        shadow_regime.ensure_reference(moment, db_path=self.db, fetch=fetch)
        shadow_regime.ensure_reference(
            moment + timedelta(minutes=10), db_path=self.db, fetch=fetch,
        )
        self.assertEqual(calls, [1])
        row = _read(self.db, "SELECT prior_close, atr_14 FROM regime_reference")[0]
        self.assertEqual(row["prior_close"], 100.0)
        self.assertEqual(row["atr_14"], 2.0)

        calls.clear()

        def fail(_day):
            calls.append(1)
            raise RuntimeError("down")

        other = os.path.join(self.tmp.name, "retry.db")
        start = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)
        shadow_regime.ensure_reference(start, db_path=other, fetch=fail)
        shadow_regime.ensure_reference(
            start + timedelta(minutes=10), db_path=other, fetch=fail,
        )
        self.assertEqual(calls, [1])
        shadow_regime.ensure_reference(
            start + timedelta(minutes=30), db_path=other, fetch=fail,
        )
        self.assertEqual(calls, [1, 1])

    def test_an_entry_tag_picks_up_the_scan_that_contains_it(self):
        def up(_now):
            return {
                "spy_bars": [
                    _bar(8, 30, open=100, high=101, low=99, close=100, volume=10),
                    _bar(9, 15, open=101, high=101, low=99, close=101, volume=10),
                ],
                "vix_bars": [],
                "vix1d_bars": [],
                "quotes": {
                    "QQQ": {"open": 1, "price": 2},
                    "IWM": {"open": 2, "price": 1},
                },
                "health": [],
            }

        def down(_now):
            return {
                "spy_bars": [
                    _bar(8, 30, open=100, high=101, low=99, close=100, volume=10),
                    _bar(9, 45, open=99, high=100, low=99, close=99, volume=10),
                ],
                "vix_bars": [],
                "vix1d_bars": [],
                "quotes": {},
                "health": [],
            }

        shadow_regime.mark_full_scan(
            datetime(2026, 11, 16, 9, 15, tzinfo=CT),
            db_path=self.db,
            fetch=up,
            timeout_s=2,
        )
        shadow_regime.tag_entry(
            ticker="SPY",
            direction="CALL",
            entry_at=datetime(2026, 11, 16, 9, 40, tzinfo=CT),
            db_path=self.db,
            trade_id="t1",
        )
        first = _read(self.db, "SELECT spy_move_pct, aligned_with_spy_move FROM regime_entry_tags")
        self.assertEqual(len(first), 1)
        self.assertAlmostEqual(first[0]["spy_move_pct"], 1.0)
        self.assertEqual(first[0]["aligned_with_spy_move"], 1)
        shadow_regime.mark_full_scan(
            datetime(2026, 11, 16, 9, 45, tzinfo=CT),
            db_path=self.db,
            fetch=down,
            timeout_s=2,
        )
        tags = _read(
            self.db,
            "SELECT spy_move_pct, aligned_with_spy_move, or_breakout_aligned FROM regime_entry_tags",
        )
        self.assertEqual(len(tags), 1)
        self.assertAlmostEqual(tags[0]["spy_move_pct"], -1.0)
        self.assertEqual(tags[0]["aligned_with_spy_move"], 0)
        marks = _read(self.db, "SELECT scanned_at FROM regime_marks ORDER BY scanned_at")
        self.assertEqual(len(marks), 2)

    def test_finish_day_stores_null_without_yahoo_and_labels_a_daily_bar(self):
        called = []

        def boom(*_args, **_kwargs):
            called.append(1)
            raise AssertionError("yahoo")

        with mock.patch("yf_client.ticker", side_effect=boom):
            shadow_regime.finish_day(date(2026, 11, 16), db_path=self.db)
        self.assertEqual(called, [])
        row = _read(self.db, "SELECT label, range_pct FROM day_labels")[0]
        self.assertIsNone(row["label"])
        self.assertIsNone(row["range_pct"])

        def fetch(_day):
            return {"open": 100, "high": 101.2, "low": 100, "close": 101.1}

        shadow_regime.finish_day(date(2026, 11, 17), db_path=self.db, fetch=fetch)
        labeled = _read(
            self.db,
            "SELECT label, range_pct FROM day_labels WHERE session_date='2026-11-17'",
        )[0]
        self.assertEqual(labeled["label"], "trend")
        self.assertAlmostEqual(labeled["range_pct"], 1.2)
        text = shadow_regime.session_clause(date(2026, 11, 17), db_path=self.db)
        self.assertEqual(text, "regime: trend, range 1.20%")
        self.assertNotIn("|", text)

    def test_session_final_puts_the_regime_ahead_of_clean(self):
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
            when = datetime(2026, 11, 16, 21, 0, tzinfo=CT)
            shadow_reconcile.reconcile_session(when, holidays=())
        finally:
            for patch in reversed(patches):
                patch.stop()
            vb.reset_book_for_tests()
            fa.reset_session_for_tests()
        line = _read(
            self.db,
            "SELECT line FROM session_lines WHERE kind='final'",
        )[0]["line"]
        self.assertLess(line.index("|regime:"), line.index("|CLEAN "))
        self.assertIn("|CLEAN yes", line)
        regime = [part for part in line.split("|") if part.startswith("regime:")]
        self.assertEqual(regime, ["regime: NULL, range NULL"])
        self.assertNotIn("incomplete=1", line)

    def test_the_brief_uses_the_prior_session(self):
        shadow_regime.write_day_label(
            date(2026, 11, 24),
            open_=100, high=101.2, low=100, close=101.1,
            vix1d_open=9,
            db_path=self.db,
        )
        shadow_regime.write_day_label(
            date(2026, 11, 25),
            open_=100, high=101, low=99.5, close=100.2,
            vix1d_open=12.4,
            db_path=self.db,
        )
        text = shadow_regime.brief_prior_line(
            datetime(2026, 11, 26, 14, 15, tzinfo=timezone.utc),
            db_path=self.db,
        )
        self.assertEqual(text, "SHADOW prior day: chop. VIX1D at the open: 12.40")
        missing = shadow_regime.brief_prior_line(
            datetime(2026, 11, 26, 14, 15, tzinfo=timezone.utc),
            db_path=os.path.join(self.tmp.name, "missing.db"),
        )
        self.assertEqual(missing, "SHADOW prior day: NULL. VIX1D at the open: NULL")

    def test_weekly_lines_are_marked_shadow(self):
        shadow_regime.write_day_label(
            date(2026, 11, 30),
            open_=100, high=101.2, low=100, close=101.1,
            vix1d_open=11,
            db_path=self.db,
        )
        shadow_regime.write_day_label(
            date(2026, 12, 1),
            open_=100, high=100.5, low=100.2, close=100.3,
            db_path=self.db,
        )
        meta = json.dumps({"stop_loss": 1.0, "quantity": 1})
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    closed_at TEXT NOT NULL,
                    ticker TEXT,
                    direction TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    pnl REAL,
                    contract_json TEXT,
                    notes TEXT,
                    pnl_fill REAL,
                    entry_mid REAL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO trade_history
                    (closed_at, ticker, direction, entry_price, exit_price, pnl,
                     contract_json, notes, pnl_fill, entry_mid)
                VALUES ('2026-11-30T21:00:00+00:00', 'SPY', 'CALL',
                        2.0, 2.5, 50, ?, 'EXIT:STOP_LOSS', 50, 2.0)
                """,
                (meta,),
            )
        lines = shadow_regime.weekly_lines(
            self.db,
            datetime(2026, 12, 2, 18, 0, tzinfo=timezone.utc),
        )
        text = "\n".join(lines)
        self.assertIn("SHADOW", text)
        self.assertIn("not a live rule", text)
        self.assertIn("trend days: 1", text)
        self.assertIn("chop days: 1", text)
        self.assertIn("normal days: 0", text)
        self.assertIn("mean R trend: 0.50", text)
        self.assertIn("mean R chop: NULL", text)
        absent = "\n".join(
            shadow_regime.weekly_lines(os.path.join(self.tmp.name, "nope.db"))
        )
        self.assertIn("not on this disk yet", absent)


if __name__ == "__main__":
    unittest.main()
