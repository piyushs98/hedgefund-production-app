"""Calibrated conviction scoring + dead zone (2026-08 rewrite)."""

from __future__ import annotations

import math
import unittest
from unittest import mock

import config
import scoring_engine as se


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


class TestThresholdConstant(unittest.TestCase):
    def test_execute_threshold_still_70(self):
        self.assertEqual(config.EXECUTE_THRESHOLD, 70)


class TestNeutralAndStrong(unittest.TestCase):
    def test_neutral_setup_scores_zero(self):
        """Spot exactly at pivot, zero day move → T=0, total=0 (and dead zone)."""
        pivot_data = {
            "close": 100.0,
            "pivot": 100.0,
            "r1": 101.0,
            "s1": 99.0,
            "pct_change": 0.0,
        }
        # atr_abs large so distance is 0; dead zone also applies
        T, tm, _ = se.score_technical(
            pivot_data, atr_pct=1.5, atr_abs=2.0, direction_sign=1.0
        )
        self.assertEqual(T, 0.0)
        self.assertEqual(tm.get("pivot_sub"), 0.0)
        self.assertEqual(tm.get("mom_sub"), 0.0)

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
        T, tm, _ = se.score_technical(
            pivot_data, atr_pct=2.0, atr_abs=atr, direction_sign=1.0
        )
        self.assertGreaterEqual(T, 80.0)
        self.assertLessEqual(T, 85.0)
        # Analytical target ~84.9 under default params
        self.assertAlmostEqual(T, 84.9, delta=1.5)

        card = se.score_ticker(
            "TEST",
            _tight_chain(close),
            pivot_data,
            headlines_text="",
            atr_pct=2.0,
            atr_abs=atr,
        )
        self.assertEqual(card.block_reason, None)
        self.assertEqual(card.action_flag, "EXECUTE")
        self.assertGreaterEqual(card.total_score, 70.0)
        self.assertLessEqual(card.total_score, 100.0)


class TestDeadZone(unittest.TestCase):
    def test_dead_zone_hard_pass_below_threshold_atr(self):
        """Below DEAD_ZONE_ATR separation → PASS regardless of strong pillars."""
        # Place spot just inside 0.30 ATR with enough momentum that T would be high
        atr = 10.0
        # 0.20 ATR above pivot — inside 0.30 dead zone
        close = 100.0 + 0.20 * atr
        pivot_data = {
            "close": close,
            "pivot": 100.0,
            "r1": 110.0,
            "s1": 90.0,
            "pct_change": 3.0,  # strong day move — still must PASS
        }
        with mock.patch.object(config, "DEAD_ZONE_ATR", 0.30):
            card = se.score_ticker(
                "SPY",
                _tight_chain(close),
                pivot_data,
                headlines_text="beats surge rally upgrade growth strong bullish\n" * 3,
                futures_pct=0.5,
                atr_pct=2.0,
                atr_abs=atr,
            )
        self.assertEqual(card.action_flag, "PASS")
        self.assertEqual(card.block_reason, "dead_zone")
        self.assertTrue(card.metrics["subscores"]["dead_zone"])
        # Components may be non-zero (forensics) but flag is hard PASS
        self.assertLess(card.metrics["subscores"]["atr_distance"], 0.30)

    def test_outside_dead_zone_can_execute(self):
        atr = 10.0
        close = 100.0 + 0.50 * atr  # 0.50 ATR — outside dead zone
        pivot_data = {
            "close": close,
            "pivot": 100.0,
            "r1": 110.0,
            "s1": 90.0,
            "pct_change": 1.5,
        }
        with mock.patch.object(config, "DEAD_ZONE_ATR", 0.30):
            card = se.score_ticker(
                "IWM",
                _tight_chain(close),
                pivot_data,
                headlines_text="",
                atr_pct=2.0,
                atr_abs=atr,
            )
        self.assertNotEqual(card.block_reason, "dead_zone")
        self.assertGreaterEqual(card.metrics["subscores"]["atr_distance"], 0.30)


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


class TestSubscoreFormat(unittest.TestCase):
    def test_format_subscore_bits_keys(self):
        atr = 2.0
        close = 103.0
        pivot_data = {
            "close": close,
            "pivot": 100.0,
            "r1": 104.0,
            "s1": 96.0,
            "pct_change": 1.5,
        }
        card = se.score_ticker(
            "TSLA",
            _tight_chain(close),
            pivot_data,
            "",
            atr_pct=2.0,
            atr_abs=atr,
        )
        bits = se.format_subscore_bits(card)
        for token in ("piv=", "mom=", "vol=", "T=", "S=", "liq=", "dATR="):
            self.assertIn(token, bits)


class TestGateDeadZoneReason(unittest.TestCase):
    def test_compact_reason_dead_zone(self):
        import signal_gate as sg

        self.assertEqual(sg._compact_reason("dead_zone"), "dead_zone")
        self.assertEqual(sg._compact_reason("Dead zone: ATR-dist 0.1"), "dead_zone")

    def test_process_scan_counts_dead_zone(self):
        import signal_gate as sg
        from datetime import datetime, timezone

        gate = sg.reset_gate_for_tests(
            sg.GateConfig(threshold=70.0, persist_cycles=1, max_concurrent=10)
        )
        now = datetime.now(timezone.utc)
        decs = gate.process_scan(
            [
                sg.Observation(
                    ticker="SPY",
                    score=80.0,
                    direction="C",
                    action_flag="PASS",
                    block_reason="dead_zone",
                ),
                sg.Observation(
                    ticker="QQQ",
                    score=75.0,
                    direction="C",
                    action_flag="PASS",
                    block_reason="dead_zone",
                ),
                sg.Observation(
                    ticker="IWM",
                    score=82.0,
                    direction="C",
                    action_flag="EXECUTE",
                ),
            ],
            now,
        )
        summary = gate.format_scan_summary(decs)
        self.assertIn("dead_zone×2", summary)
        self.assertIn("ADMIT IWM", summary)


if __name__ == "__main__":
    unittest.main()
