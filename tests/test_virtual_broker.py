"""Risk-based sizing + quantity-aware paper ledger."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import config
import virtual_broker as vb


class TestSizePosition(unittest.TestCase):
    def test_aug20_named_risks(self):
        # ACCOUNT_SIZE=10000, RISK=1.5% → $150
        cases = [
            (6.10, 4.88, 1),  # TSLA 335P  risk $122 → floor(150/122)=1
            (5.29, 4.23, 1),  # QQQ  715P  risk $106 → 1
            (4.95, 3.96, 1),  # GOOGL      risk $99  → 1
            (1.35, 1.08, 5),  # IWM  300P  risk $27  → 5
            (1.23, 0.98, 6),  # IWM  299P  risk $25  → 6
            (2.48, 1.98, 3),  # QQQ  710P  risk $50  → 3
            (2.16, 1.73, 3),  # SPY  765P  risk $43  → 3
        ]
        for entry, sl, expect in cases:
            qty = vb.size_position(entry, sl)
            self.assertEqual(qty, expect, msg=f"entry={entry} sl={sl}")

    def test_minimum_one_when_risk_exceeds_budget(self):
        # $400 risk vs $150 budget → floor = 0 → min 1
        self.assertEqual(vb.size_position(20.0, 16.0), 1)

    def test_buying_power_caps_qty(self):
        # Desired 5 of IWM 1.35 ($135 each); BP $270 → 2
        qty = vb.size_position(1.35, 1.08, buying_power=270.0)
        self.assertEqual(qty, 2)

    def test_buying_power_zero_cannot_open(self):
        qty = vb.size_position(6.10, 4.88, buying_power=50.0)
        self.assertEqual(qty, 0)

    def test_seed_equals_account_size(self):
        self.assertEqual(config.STARTING_BUYING_POWER, config.ACCOUNT_SIZE)
        self.assertEqual(config.ACCOUNT_SIZE, 10000.0)
        self.assertEqual(vb.starting_buying_power(), 10000.0)


class TestPaperQty(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.db_patch = mock.patch.object(vb, "DB_PATH", self.db)
        self.cfg_patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.db_patch.start()
        self.cfg_patch.start()
        vb.reset_book_for_tests()
        self.open_patch = mock.patch(
            "tracker_agent.load_active_trades", return_value=[]
        )
        self.open_patch.start()
        vb.ensure_ledger()

    def tearDown(self):
        vb.reset_book_for_tests()
        self.open_patch.stop()
        self.db_patch.stop()
        self.cfg_patch.stop()
        self.tmp.cleanup()

    def test_buy_sell_scales_pnl_and_debit(self):
        contract = {
            "ticker": "TSLA",
            "direction": "PUT",
            "strike": 335.0,
            "expiration": "2026-08-21",
            "quantity": 3,
        }
        buy = vb.paper_buy(contract, 6.10, quantity=3)
        self.assertTrue(buy["ok"])
        self.assertEqual(buy["quantity"], 3)
        self.assertAlmostEqual(buy["cost"], 6.10 * 100 * 3)
        snap = vb.get_portfolio()
        self.assertAlmostEqual(snap["buying_power"], 10000.0 - 1830.0)

        sell = vb.paper_sell(contract, 5.00, "PUT", 6.10, notes="EXIT:STOP_LOSS")
        self.assertTrue(sell["ok"])
        self.assertEqual(sell["quantity"], 3)
        self.assertAlmostEqual(sell["pnl"], (5.00 - 6.10) * 100 * 3)

    def test_buy_blocked_when_qty_unaffordable(self):
        contract = {"ticker": "QQQ", "direction": "PUT", "quantity": 10}
        with vb._connect() as conn:
            conn.execute(
                "UPDATE portfolio_ledger SET buying_power = 200 WHERE id = 1"
            )
            conn.commit()
        buy = vb.paper_buy(contract, 5.29, quantity=10)
        self.assertFalse(buy["ok"])
        self.assertEqual(buy["error"], "insufficient buying_power")
        snap = vb.get_portfolio()
        self.assertAlmostEqual(snap["buying_power"], 200.0)

    def test_bp_limited_sizes_down_not_reject(self):
        """qty-5 can only afford 2 → take 2 and stamp bp_limited(5->2)."""
        contract = {
            "ticker": "IWM",
            "direction": "PUT",
            "entry_premium": 1.35,
            "stop_loss": 1.08,
        }
        with vb._connect() as conn:
            conn.execute(
                "UPDATE portfolio_ledger SET buying_power = 270 WHERE id = 1"
            )
            conn.commit()
        qty = vb.apply_entry_quantity(contract)
        self.assertEqual(qty, 2)
        self.assertEqual(contract["bp_limited"], "bp_limited(5->2)")
        self.assertEqual(contract["qty_desired"], 5)
        bit = vb.format_execute_qty_bit(contract, qty)
        self.assertEqual(bit, "qty=2 bp_limited(5->2)")
        buy = vb.paper_buy(contract, 1.35, quantity=qty)
        self.assertTrue(buy["ok"])
        self.assertEqual(buy["quantity"], 2)
        self.assertAlmostEqual(buy["cost"], 1.35 * 100 * 2)

    def test_book_line_tracks_peak_and_realized(self):
        contract = {
            "ticker": "IWM",
            "direction": "PUT",
            "quantity": 2,
        }
        vb.paper_buy(contract, 1.35, quantity=2)
        line_open = vb.format_book_line()
        self.assertIn("start 10,000", line_open)
        self.assertIn("peak deployed 270", line_open)
        vb.paper_sell(contract, 2.115, "PUT", 1.35, notes="EXIT:TAKE_PROFIT")
        line = vb.format_book_line()
        # (2.115-1.35)*100*2 = +153
        self.assertIn("realized +153", line)
        self.assertIn("peak deployed 270", line)
        self.assertIn("end 10,153", line)

    def test_legacy_100k_seed_rebases_when_flat(self):
        with vb._connect() as conn:
            conn.execute(
                "UPDATE portfolio_ledger SET buying_power = 100000, "
                "total_realized_pnl = 0 WHERE id = 1"
            )
            conn.commit()
        vb.ensure_ledger()
        snap = vb.get_portfolio()
        self.assertAlmostEqual(snap["buying_power"], 10000.0)


class TestRiskConfigMismatch(unittest.TestCase):
    def test_boot_critical_when_seed_disagrees(self):
        alerts = []
        with mock.patch.object(config, "ACCOUNT_SIZE", 10000.0):
            with mock.patch.object(config, "STARTING_BUYING_POWER", 100000.0):
                with mock.patch("broadcaster.send_discord_alert", side_effect=lambda m: alerts.append(m) or True):
                    config.log_risk_config()
        self.assertTrue(any("CRITICAL" in a and "100,000" in a for a in alerts), alerts)


if __name__ == "__main__":
    unittest.main()
