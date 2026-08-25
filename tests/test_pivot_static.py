"""
Regression: Stage 2 static pivots must not drift within a session.

Protects the highest-value fix in the project — daily floor pivots are
computed once from the prior completed session and frozen per
(ticker, session_date).
"""
from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest import mock

import pandas as pd

import ticker_desk


class TestStaticPivots(unittest.TestCase):
    def setUp(self):
        ticker_desk._PIVOT_CACHE.clear()

    def tearDown(self):
        ticker_desk._PIVOT_CACHE.clear()

    def _fake_history(self):
        """Two completed daily bars + one developing session bar."""
        idx = pd.to_datetime(
            ["2026-08-01", "2026-08-03", "2026-08-04"],
        ).tz_localize("America/New_York")
        return pd.DataFrame(
            {
                "High": [100.0, 110.0, 120.0],
                "Low": [90.0, 100.0, 108.0],
                "Close": [95.0, 105.0, 115.0],
                "Open": [92.0, 102.0, 110.0],
                "Volume": [1e6, 1e6, 1e6],
            },
            index=idx,
        )

    def test_repeated_calls_identical_pivot(self):
        """Pivots for (ticker, session_date) are identical across calls."""
        hist = self._fake_history()
        session = date(2026, 8, 4)

        with mock.patch.object(ticker_desk, "_session_date_et", return_value=session):
            with mock.patch("ticker_desk.yf.Ticker") as mock_ticker:
                inst = mock_ticker.return_value
                inst.history.return_value = hist

                first = ticker_desk.fetch_pivot_data("TEST")
                second = ticker_desk.fetch_pivot_data("TEST")
                third = ticker_desk.fetch_pivot_data("TEST")

        self.assertEqual(first["pivot"], second["pivot"])
        self.assertEqual(first["pivot"], third["pivot"])
        self.assertEqual(first["r1"], third["r1"])
        self.assertEqual(first["s1"], third["s1"])
        self.assertEqual(first["r2"], third["r2"])
        self.assertEqual(first["s2"], third["s2"])

        # Basis = 2026-08-03 completed bar: H=110 L=100 C=105 -> P=105.00
        self.assertEqual(first["pivot"], 105.0)
        # Live close still tracks the developing bar
        self.assertEqual(first["close"], 115.0)
        self.assertEqual(third["close"], 115.0)

    def test_basis_date_strictly_before_session(self):
        """Cached basis_date must be strictly before the session date."""
        hist = self._fake_history()
        session = date(2026, 8, 4)

        with mock.patch.object(ticker_desk, "_session_date_et", return_value=session):
            with mock.patch("ticker_desk.yf.Ticker") as mock_ticker:
                inst = mock_ticker.return_value
                inst.history.return_value = hist
                ticker_desk.fetch_pivot_data("TEST")

        key = ("TEST", session.isoformat())
        self.assertIn(key, ticker_desk._PIVOT_CACHE)
        basis = ticker_desk._PIVOT_CACHE[key].get("basis_date")
        self.assertIsNotNone(basis)
        basis_d = date.fromisoformat(basis) if isinstance(basis, str) else basis
        self.assertLess(basis_d, session)

    def test_intraday_spot_move_does_not_change_pivot(self):
        """Even if yfinance returns a new developing H/L/C, pivot stays frozen."""
        hist1 = self._fake_history()
        hist2 = hist1.copy()
        # Blow out today's developing bar — old bug would recompute pivot from this
        hist2.iloc[-1, hist2.columns.get_loc("High")] = 200.0
        hist2.iloc[-1, hist2.columns.get_loc("Low")] = 50.0
        hist2.iloc[-1, hist2.columns.get_loc("Close")] = 150.0

        session = date(2026, 8, 4)
        with mock.patch.object(ticker_desk, "_session_date_et", return_value=session):
            with mock.patch("ticker_desk.yf.Ticker") as mock_ticker:
                inst = mock_ticker.return_value
                inst.history.return_value = hist1
                p1 = ticker_desk.fetch_pivot_data("TEST")
                inst.history.return_value = hist2
                p2 = ticker_desk.fetch_pivot_data("TEST")

        self.assertEqual(p1["pivot"], p2["pivot"])
        self.assertEqual(p1["pivot"], 105.0)
        # Live close may refresh
        self.assertEqual(p2["close"], 150.0)

    def test_empty_history_is_error_not_spot_100(self):
        session = date(2026, 8, 4)
        empty = pd.DataFrame()
        with mock.patch.object(ticker_desk, "_session_date_et", return_value=session):
            with mock.patch("ticker_desk.yf.Ticker") as mock_ticker:
                inst = mock_ticker.return_value
                inst.history.return_value = empty
                out = ticker_desk.fetch_pivot_data("NVDA")
        self.assertEqual(out.get("error"), "no_pivot_data")
        self.assertIsNone(out.get("pivot"))
        self.assertIsNone(out.get("close"))
        self.assertNotEqual(out.get("pivot"), 100.0)

    def test_fetch_exception_is_error_not_spot_100(self):
        session = date(2026, 8, 4)
        with mock.patch.object(ticker_desk, "_session_date_et", return_value=session):
            with mock.patch("ticker_desk.yf.Ticker", side_effect=RuntimeError("yahoo down")):
                out = ticker_desk.fetch_pivot_data("AAPL")
        self.assertEqual(out.get("error"), "no_pivot_data")
        self.assertIsNone(out.get("close"))


class TestSignalGateRanking(unittest.TestCase):
    def test_rank_before_admit_highest_score_wins(self):
        from datetime import timezone
        from signal_gate import SignalGate, GateConfig, Observation

        gate = SignalGate(GateConfig(max_concurrent=2, persist_cycles=2))
        now = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
        obs = [
            Observation("AAPL", 71.0, "C", "EXECUTE"),
            Observation("IWM", 90.7, "C", "EXECUTE"),
            Observation("META", 76.2, "C", "EXECUTE"),
            Observation("SPY", 80.0, "C", "EXECUTE"),
        ]
        # First cycle: persistence only
        gate.process_scan(obs, now)
        # Second cycle: admit ranked
        later = datetime(2026, 8, 4, 14, 30, tzinfo=timezone.utc)
        decs = gate.process_scan(obs, later)
        admitted = [d for d in decs if d.admit]
        self.assertEqual(len(admitted), 2)
        self.assertEqual(admitted[0].ticker, "IWM")  # highest score
        self.assertEqual(admitted[1].ticker, "SPY")
        # META and AAPL blocked by book full, not by list order
        by_t = {d.ticker: d for d in decs}
        self.assertIn("book full", by_t["META"].reason)
        self.assertIn("book full", by_t["AAPL"].reason)

    def test_on_close_frees_slot(self):
        from datetime import timezone
        from signal_gate import SignalGate, GateConfig, Observation

        gate = SignalGate(GateConfig(max_concurrent=1, persist_cycles=1))
        now = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
        d1 = gate.process_scan(
            [Observation("SPY", 80, "C", "EXECUTE")], now
        )
        self.assertTrue(d1[0].admit)
        d2 = gate.process_scan(
            [Observation("QQQ", 85, "C", "EXECUTE")], now
        )
        self.assertFalse(d2[0].admit)
        self.assertIn("book full", d2[0].reason)

        gate.on_close("SPY")
        d3 = gate.process_scan(
            [Observation("QQQ", 85, "C", "EXECUTE")], now
        )
        self.assertTrue(d3[0].admit)

    def test_rollback_admit_clears_cooldown_and_daily_cap(self):
        """Strike failure after admit must not burn daily attempts or cooldown."""
        from datetime import timedelta, timezone
        from signal_gate import SignalGate, GateConfig, Observation

        gate = SignalGate(
            GateConfig(
                max_concurrent=2,
                persist_cycles=1,
                max_entries_per_ticker=1,
                reentry_cooldown_minutes=45,
                post_exit_cooldown_minutes=45,
            )
        )
        t0 = datetime(2026, 8, 5, 15, 0, 0, tzinfo=timezone.utc)
        d1 = gate.process_scan(
            [Observation("IWM", 80, "C", "EXECUTE")], t0
        )
        self.assertTrue(d1[0].admit)
        self.assertEqual(gate._st("IWM").entries_today, 1)
        self.assertIsNotNone(gate._st("IWM").last_entry_at)

        ok = gate.rollback_admit("IWM")
        self.assertTrue(ok)
        self.assertEqual(gate._st("IWM").entries_today, 0)
        self.assertIsNone(gate._st("IWM").last_entry_at)
        self.assertFalse(gate._st("IWM").position_open)
        self.assertNotIn("IWM", gate._open)

        # Next scan moments later: re-admit must succeed (no cooldown / day cap burn)
        t1 = t0 + timedelta(minutes=1)
        d2 = gate.process_scan(
            [Observation("IWM", 81, "C", "EXECUTE")], t1
        )
        self.assertTrue(
            d2[0].admit,
            f"expected re-admit after rollback, got {d2[0].reason}",
        )

    def test_post_exit_cooldown_blocks_readmit(self):
        from datetime import timedelta, timezone
        from signal_gate import SignalGate, GateConfig, Observation

        gate = SignalGate(
            GateConfig(
                max_concurrent=5,
                persist_cycles=1,
                reentry_cooldown_minutes=0,  # isolate post-exit
                post_exit_cooldown_minutes=45,
            )
        )
        t0 = datetime(2026, 8, 7, 15, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(
            gate.process_scan([Observation("SPY", 90, "C", "EXECUTE")], t0)[0].admit
        )
        gate.on_close("SPY", closed_at=t0)
        t1 = t0 + timedelta(minutes=10)
        d = gate.process_scan([Observation("SPY", 91, "C", "EXECUTE")], t1)
        self.assertFalse(d[0].admit)
        self.assertIn("post_exit_cooldown", d[0].reason)
        t2 = t0 + timedelta(minutes=50)
        d2 = gate.process_scan([Observation("SPY", 91, "C", "EXECUTE")], t2)
        self.assertTrue(d2[0].admit, d2[0].reason)

    def test_same_scan_exit_blocks_readmit(self):
        from datetime import timezone
        from signal_gate import SignalGate, GateConfig, Observation

        gate = SignalGate(
            GateConfig(
                max_concurrent=5,
                persist_cycles=1,
                reentry_cooldown_minutes=0,
                post_exit_cooldown_minutes=0,
            )
        )
        now = datetime(2026, 8, 7, 16, 0, 0, tzinfo=timezone.utc)
        d = gate.process_scan(
            [Observation("QQQ", 88, "C", "EXECUTE")],
            now,
            closed_this_scan={"QQQ"},
        )
        self.assertFalse(d[0].admit)
        self.assertIn("same_scan", d[0].reason)


if __name__ == "__main__":
    unittest.main()
