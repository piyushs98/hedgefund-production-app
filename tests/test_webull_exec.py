"""Executor paths with a fake broker. The Webull SDK is not imported."""

from __future__ import annotations

import ast
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import session_clock
from webull_exec.broker import BrokerError, choose_account
from webull_exec.clock import entry_block
from webull_exec.envfile import read_mode, set_mode_off
from webull_exec.kill import flatten, shutdown
from webull_exec.limits import stop_limit_price
from webull_exec.notify import Notifier
from webull_exec.orders import entry_with_stop
from webull_exec.paper import PaperOpen
from webull_exec.store import Store
from webull_exec.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
BANNED = {
    "config",
    "position_exits",
    "scoring_engine",
    "signal_gate",
    "strike_selector",
    "virtual_broker",
    "master_bot",
    "reliability",
    "webull",
}


def _imported(node) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module.split(".")[0]]
    return []


def ct(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=session_clock.CHICAGO)


def paper(**overrides) -> PaperOpen:
    fields = dict(
        trade_id="t1",
        ticker="SPY",
        option_type="CALL",
        strike=500.0,
        expiration="2026-12-18",
        entry_ask=2.0,
        stop_loss=1.60,
        entry_score=85.0,
        quantity=1,
        entry_mid=1.90,
    )
    fields.update(overrides)
    return PaperOpen(**fields)


class FakeBroker:
    def __init__(self):
        self.holdings = []
        self.orders = {}
        self.placed = []
        self.cancelled = []
        self.reject_combo = False
        self.reject_stops = False
        self.reject_all = False
        self.bid = 1.25
        self.ask = 2.55
        self.quote_time = "2026-12-02T16:00:00Z"
        self.autofill = False

    def positions(self):
        return list(self.holdings)

    def open_orders(self):
        return [row for row in self.orders.values() if row["status"] == "WORKING"]

    def order_status(self, client_order_id):
        return dict(self.orders.get(client_order_id) or {
            "client_order_id": client_order_id,
            "status": "UNKNOWN",
            "filled_qty": 0,
            "avg_price": None,
        })

    def place(self, orders, combo_id=None):
        self.placed.append((orders, combo_id))
        if self.reject_all or (self.reject_combo and any(o.get("combo_type") == "MASTER" for o in orders)):
            return {"ok": False, "error": "rejected", "status": 400}
        if self.reject_stops and any(o.get("order_type") == "STOP_LOSS_LIMIT" for o in orders):
            return {"ok": False, "error": "stop rejected", "status": 400}
        for order in orders:
            self.orders[order["client_order_id"]] = {
                "client_order_id": order["client_order_id"],
                "status": "WORKING",
                "filled_qty": 0,
                "avg_price": None,
                "side": order["side"],
            }
            if self.autofill and order["side"] == "BUY" and order["order_type"] == "LIMIT":
                self._fill_buy(order)
        return {"ok": True, "status": 200}

    def _fill_buy(self, order):
        row = self.orders[order["client_order_id"]]
        row["status"] = "FILLED"
        row["filled_qty"] = 1
        row["avg_price"] = float(order["limit_price"])
        leg = order["legs"][0]
        self.holdings.append({
            "ticker": order["symbol"],
            "option_type": leg["option_type"],
            "strike": float(leg["strike_price"]),
            "expiration": leg["option_expire_date"],
            "qty": 1,
            "symbol": "",
        })

    def fill_exit(self, client_order_id, price):
        row = self.orders[client_order_id]
        row["status"] = "FILLED"
        row["filled_qty"] = 1
        row["avg_price"] = price
        self.holdings = []

    def cancel(self, client_order_id):
        self.cancelled.append(client_order_id)
        if client_order_id in self.orders:
            self.orders[client_order_id]["status"] = "CANCELLED"
        return {"ok": True}

    def snapshot_option(self, *_args):
        return {
            "bid": self.bid,
            "ask": self.ask,
            "quote_time": self.quote_time,
            "symbol": "SPY261218C00500000",
        }

    def snapshot_bid(self, *_args):
        return self.snapshot_option()["bid"]


class EngineCase(unittest.TestCase):
    def setUp(self):
        self._old_mode = os.environ.get("EXECUTION_MODE")
        os.environ["EXECUTION_MODE"] = "sandbox"
        self.addCleanup(self._restore_mode)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "exec.db")
        self.broker = FakeBroker()
        self.paper = []
        self.closes = {}
        self.env = root / ".env"
        self.env.write_text("EXECUTION_MODE=sandbox\nOTHER=keep\n", encoding="utf-8")
        self.halt = root / "halt"
        self.engine = Engine(
            self.broker,
            self.store,
            Notifier(self.store, opener=lambda *_a, **_k: None),
            lambda: list(self.paper),
            lambda trade_id: self.closes.get(trade_id),
            env_path=self.env,
            halt_path=self.halt,
        )
        self.day = date(2026, 12, 2)

    def _restore_mode(self):
        if self._old_mode is None:
            os.environ.pop("EXECUTION_MODE", None)
        else:
            os.environ["EXECUTION_MODE"] = self._old_mode

    def alerts(self):
        return [row["message"] for row in self.store.conn.execute("SELECT message FROM alerts")]

    def test_limits_and_mode_defaults(self):
        self.assertEqual(stop_limit_price(1.60), 1.47)
        self.assertIsNone(entry_block(ct(self.day, 10, 0)))
        self.assertEqual(entry_block(ct(self.day, 13, 45)), "after_1345")
        self.assertEqual(entry_block(ct(date(2026, 11, 27), 10, 0)), "early_close")

    def test_quote_compare_is_logged_and_the_limit_stays_the_paper_price(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.broker.autofill = True
        self.broker.bid = 1.70
        self.broker.ask = 2.55
        self.paper.append(paper(entry_bid=1.80))
        self.engine.tick(ct(self.day, 10, 0))
        buy = self.broker.placed[0][0][0]
        self.assertEqual(buy["limit_price"], "2.00")
        self.paper.clear()
        self.closes["t1"] = type("C", (), {"exit_bid": 2.40, "reason": "TIME_STOP"})()
        self.engine.tick(ct(self.day, 10, 5))
        sells = [
            call[0][0]
            for call in self.broker.placed
            if call[0][0]["side"] == "SELL" and call[0][0]["order_type"] == "LIMIT"
        ]
        self.assertEqual(sells[-1]["limit_price"], "2.40")
        rows = self.store.conn.execute(
            """
            SELECT side, yahoo_bid, yahoo_ask, webull_bid, webull_ask, quote_time
            FROM quote_compares
            ORDER BY id
            """
        ).fetchall()
        self.assertEqual(rows[0]["side"], "entry")
        self.assertEqual(rows[0]["yahoo_bid"], 1.80)
        self.assertEqual(rows[0]["yahoo_ask"], 2.0)
        self.assertEqual(rows[0]["webull_bid"], 1.70)
        self.assertEqual(rows[0]["webull_ask"], 2.55)
        self.assertEqual(rows[0]["quote_time"], "2026-12-02T16:00:00Z")
        self.assertEqual(rows[1]["side"], "exit")
        self.assertEqual(rows[1]["yahoo_bid"], 2.40)
        self.assertIsNone(rows[1]["yahoo_ask"])
        self.assertEqual(rows[1]["webull_bid"], 1.70)
        self.assertEqual(rows[1]["webull_ask"], 2.55)

    def test_combo_entry_uses_paper_ask_and_does_not_duplicate(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.broker.autofill = True
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        buy, stop = self.broker.placed[0][0]
        self.assertEqual(buy["combo_type"], "MASTER")
        self.assertEqual(buy["order_type"], "LIMIT")
        self.assertEqual(buy["limit_price"], "2.00")
        self.assertEqual(buy["time_in_force"], "DAY")
        self.assertEqual(buy["quantity"], "1")
        self.assertEqual(stop["combo_type"], "STOP_LOSS")
        self.assertEqual(stop["order_type"], "STOP_LOSS_LIMIT")
        self.assertEqual(stop["stop_price"], "1.60")
        self.assertEqual(stop["limit_price"], "1.47")
        self.assertEqual(stop["time_in_force"], "DAY")
        self.assertEqual(self.store.intent("t1")["state"], "protected")
        placed = len(self.broker.placed)
        self.engine.tick(ct(self.day, 10, 1))
        self.assertEqual(len(self.broker.placed), placed)

    def test_combo_reject_places_stop_after_fill(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.broker.reject_combo = True
        self.broker.autofill = True
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        self.assertGreaterEqual(len(self.broker.placed), 2)
        self.assertEqual(self.broker.placed[1][0][0]["combo_type"], "NORMAL")
        self.assertEqual(self.broker.placed[1][0][0]["order_type"], "LIMIT")
        stop = self.broker.placed[-1][0][0]
        self.assertEqual(stop["order_type"], "STOP_LOSS_LIMIT")
        self.assertEqual(stop["combo_type"], "NORMAL")
        self.assertEqual(stop["limit_price"], "1.47")
        self.assertEqual(self.store.intent("t1")["state"], "protected")

    def test_unfilled_entry_is_a_miss_at_60s(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        self.engine.tick(ct(self.day, 10, 1))
        self.assertEqual(self.store.intent("t1")["state"], "miss")
        self.assertEqual(self.store.intent("t1")["miss_reason"], "unfilled_60s")
        self.assertTrue(self.broker.cancelled)

    def test_skips(self):
        self.engine.boot(ct(self.day, 9, 0))
        cases = [
            paper(trade_id="low", entry_score=79.9),
            paper(trade_id="rich", entry_ask=7.51),
            paper(trade_id="other", ticker="IBM"),
        ]
        for row in cases:
            self.paper = [row]
            self.engine.tick(ct(self.day, 10, 0))
            self.assertEqual(self.store.intent(row.trade_id)["state"], "miss")
        self.assertEqual(self.broker.placed, [])
        self.paper = [paper(trade_id="ok", entry_ask=7.50)]
        self.broker.autofill = True
        self.engine.tick(ct(self.day, 10, 2))
        self.assertEqual(self.store.intent("ok")["state"], "protected")

    def test_no_entry_after_1345_or_on_early_close(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 13, 45))
        self.assertEqual(self.broker.placed, [])
        self.engine.tick(ct(date(2026, 11, 27), 10, 0))
        self.assertEqual(self.broker.placed, [])

    def test_max_three_open(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.broker.autofill = True
        self.paper = [paper(trade_id=f"p{i}") for i in range(4)]
        self.engine.tick(ct(self.day, 10, 0))
        states = [self.store.intent(f"p{i}")["state"] for i in range(4)]
        self.assertEqual(states[:3], ["protected", "protected", "protected"])
        self.assertEqual(states[3], "miss")
        self.assertEqual(self.store.intent("p3")["miss_reason"], "max_open")

    def test_rejected_order_is_not_retried(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.broker.reject_all = True
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        self.assertEqual(self.store.intent("t1")["state"], "rejected")
        placed = len(self.broker.placed)
        self.engine.tick(ct(self.day, 10, 1))
        self.assertEqual(len(self.broker.placed), placed)

    def test_partial_below_one_contract_is_a_miss(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        buy_id = self.store.intent("t1")["entry_client_order_id"]
        self.broker.orders[buy_id]["status"] = "PARTIAL"
        self.broker.orders[buy_id]["filled_qty"] = 0
        self.engine.tick(ct(self.day, 10, 0, ).replace(minute=0, second=10) if False else ct(self.day, 10, 0))
        # Same timestamp still sees the partial on the next call.
        self.engine.tick(ct(self.day, 10, 0))
        self.assertEqual(self.store.intent("t1")["miss_reason"], "partial")

    def test_exit_reprices_and_critical_at_two_minutes(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.broker.autofill = True
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        self.paper.clear()
        self.closes["t1"] = type("C", (), {"exit_bid": 2.40, "reason": "TIME_STOP"})()
        self.engine.tick(ct(self.day, 10, 5))
        sell = [call for call in self.broker.placed if call[0][0]["side"] == "SELL" and call[0][0]["order_type"] == "LIMIT"][-1][0][0]
        self.assertEqual(sell["limit_price"], "2.40")
        self.assertIn(self.store.intent("t1")["stop_client_order_id"], self.broker.cancelled)
        self.broker.bid = 2.10
        self.engine.tick(ct(self.day, 10, 6))
        repriced = [call for call in self.broker.placed if call[0][0]["order_type"] == "LIMIT" and call[0][0]["side"] == "SELL"][-1][0][0]
        self.assertEqual(repriced["limit_price"], "2.10")
        self.engine.tick(ct(self.day, 10, 7))
        self.assertTrue(any("still open 2 min" in msg for msg in self.alerts()))

    def test_flatten_and_sweep(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.broker.autofill = True
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        self.engine.tick(ct(self.day, 14, 45))
        self.assertEqual(self.store.intent("t1")["state"], "exiting")
        self.broker.holdings.clear()
        exit_id = self.store.intent("t1")["exit_client_order_id"]
        self.broker.fill_exit(exit_id, 1.80)
        self.engine.tick(ct(self.day, 14, 50))
        self.assertTrue(any(msg == "FLAT CONFIRMED (0 positions)" for msg in self.alerts()))

    def test_sweep_names_an_open_position(self):
        self.engine.boot(ct(self.day, 14, 40))
        self.broker.holdings.append({
            "ticker": "SPY", "option_type": "CALL", "strike": 500.0,
            "expiration": "2026-12-18", "qty": 1, "symbol": "",
        })
        self.engine.boot_clearing = False
        self.engine.tick(ct(self.day, 14, 50))
        self.assertTrue(any("sweep still open" in msg and "SPY" in msg for msg in self.alerts()))

    def test_boot_flattens_and_criticals(self):
        self.broker.holdings.append({
            "ticker": "QQQ", "option_type": "PUT", "strike": 480.0,
            "expiration": "2026-12-18", "qty": 1, "symbol": "",
        })
        self.broker.orders["old"] = {"client_order_id": "old", "status": "WORKING", "filled_qty": 0, "avg_price": None}
        self.engine.boot(ct(self.day, 9, 0))
        self.assertIn("old", self.broker.cancelled)
        self.assertTrue(any(order[0][0]["side"] == "SELL" for order in self.broker.placed))
        self.assertTrue(any("boot found" in msg and "QQQ" in msg for msg in self.alerts()))

    def test_reconcile_mismatch(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.broker.autofill = True
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        self.broker.holdings.clear()
        self.engine.tick(ct(self.day, 10, 2))
        self.assertTrue(any("without a broker position" in msg for msg in self.alerts()))

    def test_daily_stop_and_loss_cap(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.store.add_realized("old", self.day.isoformat(), -400.0, -3.1)
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        self.assertIsNone(self.store.intent("t1"))
        self.assertTrue(any("blocks new entries" in msg for msg in self.alerts()))
        self.store.add_realized("older", "2026-12-01", -600.0, -1.0)
        self.engine.tick(ct(self.day, 10, 1))
        self.assertEqual(self.store.get_control("disabled"), "1")
        self.assertEqual(self.env.read_text(encoding="utf-8").splitlines()[0], "EXECUTION_MODE=off")
        self.assertIn("OTHER=keep", self.env.read_text(encoding="utf-8"))
        self.assertTrue(self.halt.exists())

    def test_kill_switch(self):
        self.broker.orders["resting"] = {
            "client_order_id": "resting", "status": "WORKING", "filled_qty": 0, "avg_price": None,
        }
        self.broker.holdings.append({
            "ticker": "SPY", "option_type": "CALL", "strike": 500.0,
            "expiration": "2026-12-18", "qty": 1, "symbol": "",
        })
        shutdown(self.store, self.env, self.halt)
        flatten(self.broker, self.store, ct(self.day, 10, 0))
        self.assertEqual(self.store.get_control("killed"), "1")
        self.assertIn("EXECUTION_MODE=off", self.env.read_text(encoding="utf-8"))
        self.assertIn("OTHER=keep", self.env.read_text(encoding="utf-8"))
        self.assertIn("resting", self.broker.cancelled)
        self.assertTrue(any(order[0][0]["side"] == "SELL" for order in self.broker.placed))

    def test_heartbeat_skips_when_unset(self):
        calls = []
        notifier = Notifier(self.store, opener=lambda *_a, **_k: calls.append(1))
        notifier.heartbeat()
        self.assertEqual(calls, [])

    def test_client_id_is_stored_before_the_broker_call(self):
        self.engine.boot(ct(self.day, 9, 0))

        def boom(*_args, **_kwargs):
            raise RuntimeError("transport")

        self.broker.place = boom
        self.paper.append(paper())
        with self.assertRaises(RuntimeError):
            self.engine.tick(ct(self.day, 10, 0))
        rows = self.store.conn.execute("SELECT status FROM live_orders").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] == "pending" for row in rows))

    def test_restart_flattens_and_does_not_chase_the_open_paper_trade(self):
        self.engine.boot(ct(self.day, 9, 0))
        self.paper.append(paper())
        self.engine.tick(ct(self.day, 10, 0))
        buys = [call for call in self.broker.placed if call[0][0]["side"] == "BUY"]
        restarted = Engine(
            self.broker,
            self.store,
            Notifier(self.store, opener=lambda *_a, **_k: None),
            lambda: list(self.paper),
            lambda trade_id: self.closes.get(trade_id),
            env_path=self.env,
            halt_path=self.halt,
        )
        restarted.boot(ct(self.day, 10, 5))
        restarted.tick(ct(self.day, 10, 6))
        later = [call for call in self.broker.placed if call[0][0]["side"] == "BUY"]
        self.assertEqual(len(later), len(buys))
        self.assertTrue(self.broker.cancelled)

    def test_executor_does_not_import_the_paper_bot_or_sdk(self):
        paper_banned = BANNED - {"webull"}
        for path in (ROOT / "webull_exec").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                self.assertTrue(BANNED.isdisjoint(_imported(node)), path.name)
            for node in ast.walk(tree):
                self.assertTrue(paper_banned.isdisjoint(_imported(node)), path.name)
        for path in ROOT.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("import webull", text)
            self.assertNotIn("from webull", text)


class AccountChoice(unittest.TestCase):
    def test_single_margin_account_is_used_when_options_is_absent(self):
        chosen = choose_account([
            {"account_id": "cash", "account_class": "INDIVIDUAL_CASH", "options": None},
            {"account_id": "margin", "account_class": "INDIVIDUAL_MARGIN", "options": None},
            {"account_id": "fut", "account_class": "FUTURES", "options": None},
        ])
        self.assertEqual(chosen, "margin")

    def test_two_margin_accounts_are_not_guessed(self):
        with self.assertRaises(BrokerError):
            choose_account([
                {"account_id": "a", "account_class": "INDIVIDUAL_MARGIN", "options": None},
                {"account_id": "b", "account_class": "INDIVIDUAL_MARGIN", "options": None},
            ])


class OrderShape(unittest.TestCase):
    def test_attached_stop_is_single_leg(self):
        orders = entry_with_stop(
            buy_id="wb", stop_id="ws", ticker="SPY", strike=500, expiration="2026-12-18",
            option_type="CALL", entry_ask=2, stop_price=1.6, stop_limit=1.47,
        )
        self.assertEqual(len(orders[0]["legs"]), 1)
        self.assertEqual(orders[1]["legs"][0]["option_type"], "CALL")
        self.assertLessEqual(len(orders[0]["client_order_id"]), 32)


class ModeFile(unittest.TestCase):
    def test_set_mode_off_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("FOO=abc\n", encoding="utf-8")
            set_mode_off(path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("FOO=abc", text)
            self.assertEqual(read_mode(path), "off")


if __name__ == "__main__":
    unittest.main()
