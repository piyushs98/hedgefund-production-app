"""Calibrated conviction scoring + data-failure gates (2026-08 rewrite)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

import config
import scoring_engine as se
import signal_gate as sg


def _tight_chain(spot: float = 100.0) -> dict:
    """ATM contracts with tight spreads → liq_mult = 1.0."""
    return {
        "current_price": spot,
        "chains": {
            "2099-01-01": {
                "calls": [
                    {
                        "strike": spot,
                        "bid": 1.0,
                        "ask": 1.02,
                        "volume": 50_000,
                        "openInterest": 100_000,
                    }
                ],
                "puts": [
                    {
                        "strike": spot,
                        "bid": 1.0,
                        "ask": 1.02,
                        "volume": 50_000,
                        "openInterest": 100_000,
                    }
                ],
            }
        },
    }


def _zero_bid_chain(spot: float = 100.0) -> dict:
    """ATM rows present but bid=0 (Yahoo fillna) → no usable spreads."""
    return {
        "current_price": spot,
        "chains": {
            "2099-01-01": {
                "calls": [
                    {
                        "strike": spot,
                        "bid": 0.0,
                        "ask": 0.0,
                        "volume": 0,
                        "openInterest": 0,
                    }
                ],
                "puts": [
                    {
                        "strike": spot,
                        "bid": 0.0,
                        "ask": 1.5,
                        "volume": 0,
                        "openInterest": 0,
                    }
                ],
            }
        },
    }


def _wide_spread_chain(spot: float = 100.0) -> dict:
    """Median spread >10% → spread_untradeable."""
    return {
        "current_price": spot,
        "chains": {
            "2099-01-01": {
                "calls": [
                    {
                        "strike": spot,
                        "bid": 0.50,
                        "ask": 2.00,
                        "volume": 100,
                        "openInterest": 100,
                    }
                ],
                "puts": [
                    {
                        "strike": spot,
                        "bid": 0.50,
                        "ask": 2.00,
                        "volume": 100,
                        "openInterest": 100,
                    }
                ],
            }
        },
    }


class TestThresholdConstant(unittest.TestCase):
    def test_execute_threshold_still_70(self):
        self.assertEqual(config.EXECUTE_THRESHOLD, 70)


class TestNeutralAndStrong(unittest.TestCase):
    def test_neutral_setup_scores_zero(self):
        """Spot at pivot, flat day on placeholder 100/100 → T=0 + dead zone."""
        pivot_data = {
            "close": 100.0,
            "pivot": 100.0,
            "r1": 101.0,
            "s1": 99.0,
            "pct_change": 0.0,  # placeholder pair — not treated as live
        }
        T, tm, _, mom_status = se.score_technical(
            pivot_data, atr_pct=1.5, atr_abs=2.0, direction_sign=1.0
        )
        self.assertEqual(T, 0.0)
        self.assertEqual(tm.get("pivot_sub"), 0.0)
        self.assertIsNone(mom_status)  # 100/100 not "live" → 0.0 allowed as measured

        card = se.score_ticker(
            "TEST",
            _tight_chain(100.0),
            pivot_data,
            headlines_text="",
            atr_pct=1.5,
            atr_abs=2.0,
        )
        self.assertEqual(card.total_score, 0.0)
        self.assertEqual(card.action_flag, "PASS")
        self.assertEqual(card.block_reason, "dead_zone")

    def test_strong_setup_near_ceil(self):
        """1.5 ATR aligned + 1.5% day, no news, vol in sweet spot → T ≈ 85."""
        atr = 2.0
        close = 100.0 + 1.5 * atr  # 103.0
        pivot_data = {
            "close": close,
            "pivot": 100.0,
            "r1": 104.0,
            "s1": 96.0,
            "pct_change": 1.5,
        }
        T, tm, _, mom_status = se.score_technical(
            pivot_data, atr_pct=2.0, atr_abs=atr, direction_sign=1.0
        )
        self.assertIsNone(mom_status)
        self.assertGreaterEqual(T, 80.0)
        self.assertLessEqual(T, 85.0)
        self.assertAlmostEqual(T, 84.9, delta=1.5)

        card = se.score_ticker(
            "TEST",
            _tight_chain(close),
            pivot_data,
            headlines_text="",
            atr_pct=2.0,
            atr_abs=atr,
        )
        self.assertIsNone(card.block_reason)
        self.assertEqual(card.action_flag, "EXECUTE")
        self.assertGreaterEqual(card.total_score, 70.0)
        # No-news S ≈ 0, so final ≈ T (liq no longer haircuts)
        self.assertAlmostEqual(card.total_score, card.technical_score, delta=0.2)
        self.assertLessEqual(card.total_score, 85.0)

    def test_t_plus_s_ceiling_is_100_not_compressed(self):
        """TECH_CEIL + SENT_MAX = 100; clamp does not squash the top."""
        self.assertEqual(config.TECH_CEIL + config.SENT_MAX, 100.0)
        atr = 2.0
        close = 100.0 + 1.5 * atr
        pivot_data = {
            "close": close,
            "pivot": 100.0,
            "r1": 104.0,
            "s1": 96.0,
            "pct_change": 1.5,
        }
        card = se.score_ticker(
            "TEST",
            _tight_chain(close),
            pivot_data,
            headlines_text="beats record surge rally upgrade outperform",
            macro_vector="EXPANSIONARY_TAILWIND",
            futures_pct=0.75,
            atr_pct=2.0,
            atr_abs=atr,
        )
        self.assertGreaterEqual(card.technical_score, 80.0)
        self.assertLessEqual(card.technical_score, 85.0)
        self.assertGreater(card.sentiment_score, 8.0)
        self.assertLessEqual(card.total_score, 100.0)
        self.assertAlmostEqual(
            card.total_score,
            min(100.0, card.technical_score + card.sentiment_score),
            places=1,
        )


class TestDataFailures(unittest.TestCase):
    def test_empty_spreads_do_not_zero_or_block_score(self):
        """Zero-bid ATM list is forensic no_liq_data — T+S still stands."""
        pivot_data = {
            "close": 605.69,
            "pivot": 598.39,
            "r1": 604.74,
            "s1": 588.56,
            "pct_change": 1.81,
        }
        card = se.score_ticker(
            "META",
            _zero_bid_chain(605.69),
            pivot_data,
            "",
            atr_pct=3.79,
            atr_abs=22.54,
        )
        self.assertNotEqual(card.block_reason, "no_liq_data")
        self.assertGreater(card.technical_score, 50.0)
        self.assertGreater(card.total_score, 50.0)
        self.assertEqual(
            card.metrics["subscores"]["liq_status"], "no_liq_data"
        )
        self.assertAlmostEqual(
            card.total_score,
            card.technical_score + card.sentiment_score,
            places=1,
        )

    def test_wide_chain_median_does_not_zero_score(self):
        """Chain-median >10% is forensic only — score is T+S, not 0.0."""
        pivot_data = {
            "close": 103.0,
            "pivot": 100.0,
            "r1": 104.0,
            "s1": 96.0,
            "pct_change": 1.5,
        }
        card = se.score_ticker(
            "X",
            _wide_spread_chain(103.0),
            pivot_data,
            "",
            atr_pct=2.0,
            atr_abs=2.0,
        )
        self.assertNotEqual(card.block_reason, "spread_untradeable")
        self.assertGreater(card.total_score, 70.0)
        self.assertEqual(card.action_flag, "EXECUTE")
        self.assertEqual(
            card.metrics["subscores"]["liq_status"], "spread_untradeable"
        )

    def test_pct_zero_with_live_spot_is_no_momentum_data(self):
        """Partial failure shape: live spot/pivot, pct stuck at 0 → not pivot-only score."""
        pivot_data = {
            "close": 605.69,
            "pivot": 598.39,
            "r1": 604.74,
            "s1": 588.56,
            "pct_change": 0.0,
        }
        card = se.score_ticker(
            "META",
            _tight_chain(605.69),
            pivot_data,
            "",
            atr_pct=3.79,
            atr_abs=22.54,
        )
        self.assertEqual(card.block_reason, "no_momentum_data")
        self.assertEqual(card.action_flag, "PASS")
        self.assertIsNone(card.metrics["subscores"]["mom_sub"])
        # Must NOT look like pivot-only ~49 EXECUTE-adjacent score path
        self.assertNotEqual(card.block_reason, None)

    def test_pct_none_is_no_momentum_data(self):
        pivot_data = {
            "close": 605.69,
            "pivot": 598.39,
            "r1": 604.74,
            "s1": 588.56,
            "pct_change": None,
        }
        card = se.score_ticker(
            "META",
            _tight_chain(605.69),
            pivot_data,
            "",
            atr_pct=3.79,
            atr_abs=22.54,
        )
        self.assertEqual(card.block_reason, "no_momentum_data")


class TestDeadZone(unittest.TestCase):
    def test_dead_zone_hard_pass_below_threshold_atr(self):
        atr = 10.0
        close = 100.0 + 0.20 * atr
        pivot_data = {
            "close": close,
            "pivot": 100.0,
            "r1": 110.0,
            "s1": 90.0,
            "pct_change": 3.0,
        }
        with mock.patch.object(config, "DEAD_ZONE_ATR", 0.30):
            card = se.score_ticker(
                "SPY",
                _tight_chain(close),
                pivot_data,
                headlines_text="",
                atr_pct=2.0,
                atr_abs=atr,
            )
        self.assertEqual(card.action_flag, "PASS")
        self.assertEqual(card.block_reason, "dead_zone")


class TestGateDataReasons(unittest.TestCase):
    def test_compact_reasons_distinct(self):
        self.assertEqual(sg._compact_reason("no_liq_data"), "no_liq_data")
        self.assertEqual(sg._compact_reason("spread_untradeable"), "spread_untradeable")
        self.assertEqual(sg._compact_reason("no_momentum_data"), "no_momentum_data")
        self.assertEqual(sg._compact_reason("dead_zone"), "dead_zone")
        # liq-killed total no longer the only path — but below_thr still exists
        self.assertEqual(sg._compact_reason("score 0.0 below 70"), "below_thr")

    def test_gate_shows_no_liq_data_not_below_thr(self):
        gate = sg.reset_gate_for_tests(
            sg.GateConfig(threshold=70.0, persist_cycles=1, max_concurrent=10)
        )
        now = datetime.now(timezone.utc)
        decs = gate.process_scan(
            [
                sg.Observation(
                    ticker="META",
                    score=65.0,
                    direction=None,
                    action_flag="PASS",
                    block_reason="no_liq_data",
                ),
                sg.Observation(
                    ticker="GOOGL",
                    score=0.0,
                    direction=None,
                    action_flag="PASS",
                    block_reason="spread_untradeable",
                ),
                sg.Observation(
                    ticker="MSFT",
                    score=40.0,
                    direction=None,
                    action_flag="PASS",
                    block_reason="no_momentum_data",
                ),
            ],
            now,
        )
        summary = gate.format_scan_summary(decs)
        self.assertIn("no_liq_data×1", summary)
        self.assertIn("spread_untradeable×1", summary)
        self.assertIn("no_momentum_data×1", summary)
        self.assertNotIn("below_thr", summary)


class TestSubscoreFormat(unittest.TestCase):
    def test_format_includes_all_keys(self):
        pivot_data = {
            "close": 103.0,
            "pivot": 100.0,
            "r1": 104.0,
            "s1": 96.0,
            "pct_change": 1.5,
        }
        card = se.score_ticker(
            "TSLA",
            _tight_chain(103.0),
            pivot_data,
            "",
            atr_pct=2.0,
            atr_abs=2.0,
        )
        bits = se.format_subscore_bits(card)
        for token in (
            "piv=", "mom=", "vol=", "T=", "S=", "liq=", "dATR=",
            "atm_n=", "med_spr=", "usable=",
        ):
            self.assertIn(token, bits)


class TestWeightsRetired(unittest.TestCase):
    def test_score_ticker_ignores_weights_arg(self):
        atr = 2.0
        close = 100.0 + 1.5 * atr
        pivot_data = {
            "close": close,
            "pivot": 100.0,
            "r1": 104.0,
            "s1": 96.0,
            "pct_change": 1.5,
        }
        a = se.score_ticker(
            "X",
            _tight_chain(close),
            pivot_data,
            "",
            atr_pct=2.0,
            atr_abs=atr,
            weights={"liquidity": 30, "technical": 40, "sentiment": 30},
        )
        b = se.score_ticker(
            "X",
            _tight_chain(close),
            pivot_data,
            "",
            atr_pct=2.0,
            atr_abs=atr,
            weights=None,
        )
        self.assertEqual(a.total_score, b.total_score)
        self.assertEqual(a.action_flag, b.action_flag)


if __name__ == "__main__":
    unittest.main()
