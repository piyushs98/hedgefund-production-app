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
                            "bid": 2.06,
                            "ask": 2.14,
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
        # Tight spread so the binding filter is decay, not MAX_CONTRACT_SPREAD_PCT
        od = self._chain("2026-08-05", 302.0, 1.38, 1.42, oi=50000, vol=20000)
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

    def test_fetch_contract_quote_shape(self):
        """Light path returns options_dict with single strike for lookup_option_mark."""
        import json
        from data_engineer import fetch_contract_quote
        import pandas as pd

        calls = pd.DataFrame(
            [
                {
                    "strike": 500.0,
                    "bid": 1.0,
                    "ask": 1.2,
                    "lastPrice": 1.1,
                    "volume": 10,
                    "openInterest": 100,
                    "impliedVolatility": 0.2,
                }
            ]
        )
        puts = pd.DataFrame(columns=calls.columns)

        class _Chain:
            def __init__(self):
                self.calls = calls
                self.puts = puts

        class _T:
            options = ["2026-08-14"]

            def history(self, period="1d"):
                return pd.DataFrame({"Close": [505.0]})

            def option_chain(self, exp):
                return _Chain()

        with mock.patch("data_engineer.yf.Ticker", return_value=_T()):
            raw = fetch_contract_quote("SPY", "2026-08-14", 500.0, "CALL")
        od = json.loads(raw)
        self.assertNotIn("error", od)
        self.assertEqual(od.get("quote_mode"), "single_contract")
        self.assertEqual(len(od["chains"]["2026-08-14"]["calls"]), 1)
        trade = {
            "ticker": "SPY",
            "direction": "CALL",
            "strike": 500.0,
            "expiration": "2026-08-14",
        }
        info = __import__("position_exits").lookup_option_mark(trade, od)
        self.assertTrue(info["found"])
        self.assertAlmostEqual(info["mark"], 1.1)

    def test_bad_quote_negative_extrinsic(self):
        ok, tag = ss._passes_entry_filters(
            cal_dte=2, rm_atr=0.1, dens=1.0, extrinsic=-0.59, extrinsic_pct=-4.8
        )
        self.assertFalse(ok)
        self.assertIn("bad_quote", tag or "")

    def test_min_extrinsic_pct(self):
        ok, tag = ss._passes_entry_filters(
            cal_dte=2, rm_atr=0.1, dens=1.0, extrinsic=0.20, extrinsic_pct=1.5
        )
        self.assertFalse(ok)
        self.assertIn("min_ext", tag or "")

    def test_spread_cap_rejects_and_formats_tag(self):
        ok, tag = ss._passes_entry_filters(
            cal_dte=2, rm_atr=0.1, dens=1.0,
            extrinsic=0.50, extrinsic_pct=25.0,
            bid=1.00, ask=1.28, spread_pct=12.4,
        )
        self.assertFalse(ok)
        self.assertEqual(tag, "spread(12.4>8.0)")

    def test_no_two_sided_quote_is_no_liq_data(self):
        ok, tag = ss._passes_entry_filters(
            cal_dte=2, rm_atr=0.1, dens=1.0,
            extrinsic=0.50, extrinsic_pct=25.0,
            bid=0.0, ask=2.00, spread_pct=200.0,
        )
        self.assertFalse(ok)
        self.assertEqual(tag, "no_liq_data")

    def test_spread_walks_to_next_ranked_candidate(self):
        """Top rank can be a wide market; selector must try the next row."""
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        options = {
            "current_price": 302.10,
            "chains": {
                "2026-08-06": {
                    "calls": [
                        {
                            # ~12% spread, huge OI/vol so it ranks first
                            "strike": 303.0,
                            "bid": 1.88,
                            "ask": 2.12,
                            "openInterest": 100_000,
                            "volume": 50_000,
                            "impliedVolatility": 0.20,
                        },
                        {
                            # ~3% spread, thin — ranks second, should be chosen
                            "strike": 302.0,
                            "bid": 1.97,
                            "ask": 2.03,
                            "openInterest": 10,
                            "volume": 1,
                            "impliedVolatility": 0.80,
                        },
                    ],
                    "puts": [],
                }
            },
        }
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            with mock.patch.object(config, "REQUIRED_MOVE_ATR_K", 5.0):
                with mock.patch.object(config, "EXIT_MAX_DECAY_DENSITY", 100.0):
                    with mock.patch.object(config, "MAX_CONTRACT_SPREAD_PCT", 8.0):
                        out = ss.select_optimal_contract(
                            options, pivot, atr_abs=5.0, now=now
                        )
        self.assertNotIn("error", out)
        self.assertEqual(out["strike"], 302.0)
        self.assertLessEqual(out["bid_ask_spread_pct"], 8.0)
        self.assertGreaterEqual(out.get("rejected_better_ranks") or 0, 1)

    def test_all_wide_rejects_ticker_with_spread_tag(self):
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        od = self._chain("2026-08-06", 303.0, 1.88, 2.12)
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            with mock.patch.object(config, "REQUIRED_MOVE_ATR_K", 5.0):
                with mock.patch.object(config, "EXIT_MAX_DECAY_DENSITY", 100.0):
                    with mock.patch.object(config, "MAX_CONTRACT_SPREAD_PCT", 8.0):
                        out = ss.select_optimal_contract(
                            od, pivot, atr_abs=5.0, now=now
                        )
        self.assertIn("error", out)
        self.assertTrue(
            str(out.get("reject_tag") or "").startswith("spread("),
            out.get("reject_tag"),
        )

    def test_risk_too_large_tag_on_rich_premium(self):
        # mid 10.50 → SL 8.40 → 1-lot risk $210 > $150
        ok, tag = ss._passes_entry_filters(
            cal_dte=2, rm_atr=0.1, dens=1.0,
            extrinsic=2.00, extrinsic_pct=20.0,
            bid=10.40, ask=10.60, spread_pct=1.9,
            entry=10.50,
        )
        self.assertFalse(ok)
        self.assertEqual(tag, "risk_too_large($210>$150 at qty1)")

    def test_min_premium_rejects_cheap(self):
        ok, tag = ss._passes_entry_filters(
            cal_dte=2, rm_atr=0.1, dens=1.0,
            extrinsic=0.50, extrinsic_pct=75.0,
            bid=0.64, ask=0.70, spread_pct=9.0,
            entry=0.67,
        )
        # spread 9% also fails first — use tight quotes
        ok, tag = ss._passes_entry_filters(
            cal_dte=2, rm_atr=0.1, dens=1.0,
            extrinsic=0.50, extrinsic_pct=75.0,
            bid=0.66, ask=0.68, spread_pct=3.0,
            entry=0.67,
        )
        self.assertFalse(ok)
        self.assertEqual(tag, "min_premium($0.67<1.00)")

    def test_min_premium_walks_to_richer_candidate(self):
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        options = {
            "current_price": 302.10,
            "chains": {
                "2026-08-06": {
                    "calls": [
                        {
                            "strike": 304.0,
                            "bid": 0.64,
                            "ask": 0.70,
                            "openInterest": 100_000,
                            "volume": 50_000,
                            "impliedVolatility": 0.20,
                        },
                        {
                            "strike": 303.0,
                            "bid": 2.06,
                            "ask": 2.14,
                            "openInterest": 10,
                            "volume": 1,
                            "impliedVolatility": 0.80,
                        },
                    ],
                    "puts": [],
                }
            },
        }
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            with mock.patch.object(config, "REQUIRED_MOVE_ATR_K", 5.0):
                with mock.patch.object(config, "EXIT_MAX_DECAY_DENSITY", 100.0):
                    out = ss.select_optimal_contract(
                        options, pivot, atr_abs=5.0, now=now, ticker="AAPL"
                    )
        self.assertNotIn("error", out)
        self.assertGreaterEqual(out["entry_premium"], 1.00)

    def test_tsla_610_does_not_breach_150(self):
        ok, tag = ss._passes_entry_filters(
            cal_dte=2, rm_atr=0.1, dens=1.0,
            extrinsic=1.00, extrinsic_pct=16.0,
            bid=6.00, ask=6.20, spread_pct=3.3,
            entry=6.10,
        )
        self.assertTrue(ok)
        self.assertIsNone(tag)

    def test_risk_walks_to_cheaper_ranked_candidate(self):
        """Top rank can be too rich; selector must try a cheaper row in-band."""
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        options = {
            "current_price": 302.10,
            "chains": {
                "2026-08-06": {
                    "calls": [
                        {
                            # ranks first (tight + huge OI) but 1-lot risk $210
                            "strike": 303.0,
                            "bid": 10.40,
                            "ask": 10.60,
                            "openInterest": 100_000,
                            "volume": 50_000,
                            "impliedVolatility": 0.20,
                        },
                        {
                            # cheaper, thinner — 1-lot risk ~$42, should be chosen
                            "strike": 304.0,
                            "bid": 2.06,
                            "ask": 2.14,
                            "openInterest": 10,
                            "volume": 1,
                            "impliedVolatility": 0.80,
                        },
                    ],
                    "puts": [],
                }
            },
        }
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            with mock.patch.object(config, "REQUIRED_MOVE_ATR_K", 5.0):
                with mock.patch.object(config, "EXIT_MAX_DECAY_DENSITY", 100.0):
                    with mock.patch.object(config, "MAX_CONTRACT_SPREAD_PCT", 8.0):
                        out = ss.select_optimal_contract(
                            options, pivot, atr_abs=5.0, now=now, ticker="TEST"
                        )
        self.assertNotIn("error", out)
        self.assertEqual(out["strike"], 304.0)
        self.assertLessEqual(out["entry_premium"], 3.0)
        self.assertGreaterEqual(out.get("rejected_better_ranks") or 0, 1)

    def test_all_rich_rejects_ticker_with_risk_tag(self):
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        od = self._chain("2026-08-06", 303.0, 10.40, 10.60)
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            with mock.patch.object(config, "REQUIRED_MOVE_ATR_K", 5.0):
                with mock.patch.object(config, "EXIT_MAX_DECAY_DENSITY", 100.0):
                    out = ss.select_optimal_contract(
                        od, pivot, atr_abs=5.0, now=now, ticker="XYZ"
                    )
        self.assertIn("error", out)
        self.assertTrue(
            str(out.get("reject_tag") or "").startswith("risk_too_large("),
            out.get("reject_tag"),
        )

    def test_zero_bids_in_band_are_no_liq_data(self):
        now = datetime(2026, 8, 5, 10, 32, tzinfo=ET)
        od = self._chain("2026-08-06", 303.0, 0.0, 0.0)
        pivot = {"close": 302.10, "pivot": 300.0, "pct_change": 0.5}
        with mock.patch.object(config, "MIN_DTE", 1):
            out = ss.select_optimal_contract(od, pivot, atr_abs=5.0, now=now)
        self.assertIn("error", out)
        self.assertEqual(out.get("reject_tag"), "no_liq_data")


class TestRequiredMove(unittest.TestCase):
    def test_call_breakeven(self):
        # IWM-like: need 1.32, atr 5, dte 0.84
        rm = ss.required_move_atr(302.10, 302.0, 1.42, "CALL", 5.0, 0.84)
        self.assertIsNotNone(rm)
        self.assertAlmostEqual(rm, 1.32 / (5.0 * (0.84 ** 0.5)), places=3)
        self.assertLess(rm, 0.5)  # passes k=0.5


if __name__ == "__main__":
    unittest.main()
