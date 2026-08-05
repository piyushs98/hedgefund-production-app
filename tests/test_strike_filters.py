"""Stage 4 Part C: honest DTE, MIN_DTE, required-move, decay density."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest import mock

import config
import strike_selector as ss

ET = ZoneInfo("America/New_York")
CDT = ZoneInfo("America/Chicago")


class TestHonestDte(unittest.TestCase):
    def test_0dte_no_floor_at_one(self):
        # 09:32 CDT = 10:32 ET → remaining ~0.84 of RTH
        now = datetime(2026, 8, 5, 9, 32, tzinfo=CDT)
        dte = ss.effective_dte("2026-08-05", now)
        self.assertGreater(dte, 0.7)
        self.assertLess(dte, 0.95)
        self.assertNotEqual(dte, 1.0)

    def test_1dte_includes_remaining_today(self):
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        dte = ss.effective_dte("2026-08-06", now)
        # ~0.84 + 1.0
        self.assertGreater(dte, 1.5)
        self.assertLess(dte, 2.1)


class TestEntryFilters(unittest.TestCase):
    def _chain(self, exp, strike, bid, ask, oi=5000, vol=2000, iv=0.3):
        mid = (bid + ask) / 2
        return {
            "current_price": 302.10,
            "chains": {
                exp: {
                    "calls": [
                        {
                            "strike": strike,
                            "bid": bid,
                            "ask": ask,
                            "lastPrice": mid,
                            "openInterest": oi,
                            "volume": vol,
                            "impliedVolatility": iv,
                        }
                    ],
                    "puts": [],
                }
            },
        }

    def test_min_dte_rejects_0dte(self):
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        # Only 0DTE available
        od = self._chain("2026-08-05", 302.0, 1.30, 1.50)
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            out = ss.select_optimal_contract(od, pivot, atr_abs=5.0, now=now)
        self.assertIn("error", out)
        self.assertIn("MIN_DTE", out["error"] + str(out.get("error", "")))

    def test_selects_1dte_when_available(self):
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        options = {
            "current_price": 302.10,
            "chains": {
                "2026-08-05": {
                    "calls": [
                        {
                            "strike": 302.0,
                            "bid": 1.30,
                            "ask": 1.50,
                            "openInterest": 90000,
                            "volume": 50000,
                            "impliedVolatility": 0.25,
                        }
                    ],
                    "puts": [],
                },
                "2026-08-06": {
                    "calls": [
                        {
                            "strike": 303.0,
                            "bid": 2.00,
                            "ask": 2.20,
                            "openInterest": 8000,
                            "volume": 3000,
                            "impliedVolatility": 0.28,
                        }
                    ],
                    "puts": [],
                },
            },
        }
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            with mock.patch.object(config, "REQUIRED_MOVE_ATR_K", 5.0):  # loose
                with mock.patch.object(config, "EXIT_MAX_DECAY_DENSITY", 100.0):
                    out = ss.select_optimal_contract(
                        options, pivot, atr_abs=5.0, now=now
                    )
        self.assertNotIn("error", out)
        self.assertEqual(out["expiration"], "2026-08-06")
        self.assertEqual(out["calendar_dte"], 1)
        self.assertIn("required_move_atr", out)
        self.assertIn("decay_density", out)

    def test_decay_density_rejects_rich_0dte_if_min_dte_zero(self):
        """C-F alone catches IWM-class when MIN_DTE env-lowered to 0."""
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        # ~93% extrinsic ATM: strike 302, spot 302.10, mid 1.40 → almost all ext
        od = self._chain("2026-08-05", 302.0, 1.30, 1.50, oi=50000, vol=20000)
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 0):
            with mock.patch.object(config, "REQUIRED_MOVE_ATR_K", 5.0):
                with mock.patch.object(config, "EXIT_MAX_DECAY_DENSITY", 8.0):
                    out = ss.select_optimal_contract(od, pivot, atr_abs=5.0, now=now)
        self.assertIn("error", out)
        self.assertIn("decay_density", out["error"])

    def test_decay_density_math_iwm(self):
        # 93% / 5.5h ≈ 16.9
        dens = ss.decay_density(93.0, 5.5)
        self.assertAlmostEqual(dens, 93.0 / 5.5, places=2)
        self.assertGreater(dens, 8.0)


class TestRequiredMove(unittest.TestCase):
    def test_call_breakeven(self):
        # IWM-like: need 1.32, atr 5, dte 0.84
        rm = ss.required_move_atr(302.10, 302.0, 1.42, "CALL", 5.0, 0.84)
        self.assertIsNotNone(rm)
        self.assertAlmostEqual(rm, 1.32 / (5.0 * (0.84 ** 0.5)), places=3)
        self.assertLess(rm, 0.5)  # passes k=0.5


if __name__ == "__main__":
    unittest.main()
