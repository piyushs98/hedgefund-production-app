"""A chain outage is its own GATE reason, and pages once."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

import config
import earnings_blackout
import fill_accounting
import midday_delta
import scoring_engine
import signal_gate
from circuit_breaker import CircuitBreaker


def _outage_messages(sent: list[str]) -> list[str]:
    return [m for m in sent if "NO MARKET DATA" in m]


class DataUnavailableScanTests(unittest.TestCase):
    def setUp(self):
        self._session = copy.deepcopy(fill_accounting._session)
        midday_delta.reset_market_data_alert_for_tests()
        signal_gate.reset_gate_for_tests(
            signal_gate.GateConfig(threshold=70.0, persist_cycles=1, max_concurrent=10)
        )
        earnings_blackout.set_calendar_for_tests({})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent: list[str] = []
        self._send_ok = True

        def _send(message):
            text = str(message)
            self.sent.append(text)
            if "NO MARKET DATA" in text:
                return self._send_ok
            return True

        self._send = _send

    def tearDown(self):
        midday_delta.reset_market_data_alert_for_tests()
        earnings_blackout.reset_for_tests()
        signal_gate.reset_gate_for_tests()
        fill_accounting._session.clear()
        fill_accounting._session.update(self._session)

    def _scan(self, tickers, *, fetch, threshold=50, extra_patches=()):
        db_path = os.path.join(self.tmp.name, f"cb-{threshold}-{len(tickers)}.db")
        breaker = CircuitBreaker(
            failure_threshold=threshold,
            cooldown_seconds=900,
            db_path=db_path,
        )
        baseline = os.path.join(self.tmp.name, "baseline.json")
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(midday_delta, "BASELINE_PATH", baseline)
        )
        stack.enter_context(
            mock.patch.object(midday_delta, "fetch_options_data", side_effect=fetch)
        )
        stack.enter_context(
            mock.patch.object(midday_delta, "get_innovation_context", return_value="")
        )
        stack.enter_context(
            mock.patch("master_bot.get_latest_futures_pct", return_value=None)
        )
        stack.enter_context(
            mock.patch("tracker_agent.load_active_trades", return_value=[])
        )
        stack.enter_context(
            mock.patch("position_exits.carry_review_already_done", return_value=True)
        )
        stack.enter_context(
            mock.patch("telemetry.log_scan_result", return_value=None)
        )
        stack.enter_context(
            mock.patch("broadcaster.send_discord_alert", side_effect=self._send)
        )
        for patcher in extra_patches:
            stack.enter_context(patcher)
        self.addCleanup(stack.close)
        with stack:
            result = midday_delta.run_thirty_min_scan(
                breaker,
                tickers=list(tickers),
                inter_ticker_sleep=0,
            )
        return result, breaker

    def test_zero_chains_label_and_one_critical(self):
        names = list(config.TICKERS)
        self.assertEqual(len(names), 10)

        def fetch(_ticker):
            return json.dumps({"error": "HTTP 429"})

        result, breaker = self._scan(names, fetch=fetch)
        summary = result["gate_summary"]
        self.assertIn("data_unavailable×10", summary)
        self.assertNotIn("below_thr", summary)
        self.assertEqual(result["chains_fetched"], 0)
        pages = _outage_messages(self.sent)
        self.assertEqual(len(pages), 1)
        self.assertIn(
            "NO MARKET DATA — 0/10 tickers fetched, trading suspended.",
            pages[0],
        )
        self.assertIn("CRITICAL", pages[0])
        self.assertEqual(breaker.status()["failures"], 10)

        result2, _breaker2 = self._scan(names, fetch=fetch)
        self.assertIn("data_unavailable×10", result2["gate_summary"])
        self.assertEqual(len(_outage_messages(self.sent)), 1)

    def test_undelivered_page_retries_after_the_breaker_opens(self):
        names = list(config.TICKERS)
        self._send_ok = False

        def fetch(_ticker):
            return json.dumps({"error": "HTTP 429"})

        self._scan(names, fetch=fetch, threshold=5)
        self.assertEqual(len(_outage_messages(self.sent)), 1)
        self.assertIsNotNone(midday_delta._market_data_alert_pending)

        self._send_ok = True
        result, breaker = self._scan(names, fetch=fetch, threshold=5)
        self.assertTrue(result["circuit_breaker_open"])
        self.assertEqual(len(_outage_messages(self.sent)), 2)
        self.assertIsNone(midday_delta._market_data_alert_pending)
        self.assertEqual(breaker.status()["state"], "OPEN")

        self._scan(names, fetch=fetch, threshold=5)
        self.assertEqual(len(_outage_messages(self.sent)), 2)

    def test_partial_fetch_keeps_pivot_failure_and_does_not_page(self):
        def fetch(ticker):
            if ticker == "SPY":
                return json.dumps({"error": "HTTP 429"})
            return json.dumps(
                {"ticker": ticker, "current_price": 100.0, "chains": {}}
            )

        def _score(ticker, *_a, **_k):
            return scoring_engine.data_fail_card(ticker, "no_pivot_data")

        extra = (
            mock.patch.object(midday_delta, "fetch_pivot_data", return_value={}),
            mock.patch("master_bot.fetch_atr", return_value=(1.0, 1.0)),
            mock.patch("master_bot.ensure_news_context", return_value=""),
            mock.patch("master_bot.fetch_intraday_drift", return_value=0.0),
            mock.patch.object(scoring_engine, "score_ticker", side_effect=_score),
        )
        result, breaker = self._scan(["SPY", "QQQ"], fetch=fetch, extra_patches=extra)
        summary = result["gate_summary"]
        self.assertIn("data_unavailable×1", summary)
        self.assertIn("no_pivot_data×1", summary)
        self.assertNotIn("below_thr", summary)
        self.assertEqual(result["chains_fetched"], 1)
        self.assertEqual(_outage_messages(self.sent), [])
        self.assertEqual(breaker.status()["failures"], 0)
