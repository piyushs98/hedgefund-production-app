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
        # ACCOUNT_SIZE=25000, RISK=1.5% → $375
        # SL is 80% of entry (strike_selector default)
        cases = [
            (6.10, 4.88, 3),   # TSLA 335P  risk $122 → floor(375/122)=3
            (5.29, 4.23, 3),   # QQQ  715P  risk $106 → 3
            (4.95, 3.96, 3),   # GOOGL      risk $99  → 3
            (1.35, 1.08, 10),  # IWM  300P  risk $27  → 13 capped at 10
            (1.23, 0.98, 10),  # IWM  299P  risk $25  → 15 capped at 10
        ]
        for entry, sl, expect in cases:
            qty = vb.size_position(entry, sl)
            self.assertEqual(qty, expect, msg=f"entry={entry} sl={sl}")

    def test_minimum_one_when_risk_exceeds_budget(self):
        # $400 risk vs $375 budget → floor = 0 → min 1
        self.assertEqual(vb.size_position(20.0, 16.0), 1)

    def test_buying_power_caps_qty(self):
        # 3 contracts of $6.10 cost $1830; BP $700 → 1
        qty = vb.size_position(6.10, 4.88, buying_power=700.0)
        self.assertEqual(qty, 1)

    def test_buying_power_zero_cannot_open(self):
        qty = vb.size_position(6.10, 4.88, buying_power=50.0)
        self.assertEqual(qty, 0)


class TestPaperQty(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        self.db_patch = mock.patch.object(vb, "DB_PATH", self.db)
        self.cfg_patch = mock.patch.object(config, "NEWS_DB_PATH", self.db)
        self.db_patch.start()
        self.cfg_patch.start()
        vb.ensure_ledger()

    def tearDown(self):
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
        self.assertAlmostEqual(snap["buying_power"], 100_000.0 - 1830.0)

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


if __name__ == "__main__":
    unittest.main()
