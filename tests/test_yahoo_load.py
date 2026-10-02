"""Yahoo request shape: selection window, shared ticker, request pacing.

No network. Daily history is intentionally not cached across a session —
pct_change and ATR use the developing bar.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from unittest import mock

import pandas as pd

import fill_accounting as fa
import yf_client
from data_engineer import (
    fetch_contract_quote,
    fetch_options_data,
    reset_yahoo_ticker_cache_for_tests,
)


def _frame(strike: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "strike": float(strike),
                "bid": 1.0,
                "ask": 1.08,
                "lastPrice": 1.04,
                "volume": 10,
                "openInterest": 20,
                "impliedVolatility": 0.2,
                "delta": 0.4,
            }
        ]
    )


class _Chain:
    def __init__(self, strike: float):
        self.calls = _frame(strike)
        self.puts = _frame(strike)


class _Stock:
    def __init__(self):
        self.option_calls: list = []

    @property
    def options(self):
        return ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-16"]

    def history(self, period="1d"):
        return pd.DataFrame({"Close": [100.0]})

    def option_chain(self, date=None):
        self.option_calls.append(date)
        strike = {
            None: 30.0,
            "2026-09-30": 30.0,
            "2026-10-01": 101.0,
            "2026-10-02": 102.0,
            "2026-10-16": 116.0,
        }[date]
        return _Chain(strike)


def _dte_from(asof: date):
    def _dte(exp, now=None):
        exp_d = datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
        return (exp_d - asof).days

    return _dte


class TestOptionsWindow(unittest.TestCase):
    def setUp(self):
        reset_yahoo_ticker_cache_for_tests()
        fa.reset_session_for_tests()
        self.stock = _Stock()
        self.ticker = mock.patch("data_engineer.yf.Ticker", return_value=self.stock)
        self.ticker.start()

    def tearDown(self):
        self.ticker.stop()
        reset_yahoo_ticker_cache_for_tests()
        fa.reset_session_for_tests()

    def _fetch(self, asof: date):
        with mock.patch("data_engineer._calendar_dte", _dte_from(asof)):
            raw = fetch_options_data("SPY")
        return json.loads(raw)

    def test_reuses_nearest_chain_and_skips_the_second_download(self):
        # Wednesday 2026-09-30. Today is DTE 0. Oct 16 is past MAX (10).
        payload = self._fetch(date(2026, 9, 30))
        self.assertNotIn("error", payload)
        self.assertEqual(
            self.stock.option_calls,
            [None, "2026-10-01", "2026-10-02"],
        )
        self.assertEqual(
            set(payload["chains"]),
            {"2026-09-30", "2026-10-01", "2026-10-02"},
        )
        # Today's chain is the list payload, not a second option_chain("2026-09-30").
        self.assertEqual(payload["chains"]["2026-09-30"]["calls"][0]["strike"], 30.0)
        self.assertNotIn("2026-10-16", payload["chains"])
        self.assertNotIn("mark_chains", payload)
        self.assertTrue(fa.session_snapshot()["chain_ok"])

    def test_nearest_inside_the_window_is_not_fetched_twice(self):
        # 2026-09-29: Sep 30 is DTE 1, already inside MIN_DTE..MAX.
        payload = self._fetch(date(2026, 9, 29))
        self.assertEqual(
            self.stock.option_calls,
            [None, "2026-10-01", "2026-10-02"],
        )
        self.assertNotIn("2026-09-30", self.stock.option_calls)
        self.assertEqual(payload["chains"]["2026-09-30"]["calls"][0]["strike"], 30.0)
        self.assertIn("2026-10-01", payload["chains"])
        self.assertIn("2026-10-02", payload["chains"])

    def test_one_ticker_object_per_symbol(self):
        built: list = []

        class _Quote:
            options = ["2026-10-02"]

            def history(self, period="1d"):
                return pd.DataFrame({"Close": [50.0]})

            def option_chain(self, exp):
                return _Chain(50.0)

        def factory(*args, **kwargs):
            built.append(args[0] if args else kwargs.get("symbol"))
            return _Quote()

        reset_yahoo_ticker_cache_for_tests()
        with mock.patch("data_engineer.yf.Ticker", side_effect=factory):
            first = json.loads(fetch_contract_quote("MSFT", "2026-10-02", 50, "CALL"))
            second = json.loads(fetch_contract_quote("MSFT", "2026-10-02", 50, "CALL"))
        self.assertEqual(built, ["MSFT"])
        self.assertEqual(first["quote_mode"], "single_contract")
        self.assertEqual(second["quote_mode"], "single_contract")
        self.assertNotIn("error", first)
        self.assertNotIn("error", second)


class TestYahooPace(unittest.TestCase):
    def setUp(self):
        yf_client.reset_crumb_backoff_for_tests()
        yf_client.reset_request_pace_for_tests()

    def tearDown(self):
        yf_client.reset_crumb_backoff_for_tests()
        yf_client.reset_request_pace_for_tests()

    def test_real_sends_are_spaced(self):
        def _ok(method, url, *args, **kwargs):
            response = mock.Mock()
            response.status_code = 200
            response.url = url
            response.text = "ok"
            return response

        with mock.patch.object(yf_client, "_original_session_request", _ok):
            with mock.patch.object(yf_client.time, "sleep") as slept:
                yf_client.SESSION.get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
                )
                yf_client.SESSION.get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/QQQ"
                )
        waits = [call.args[0] for call in slept.call_args_list if call.args]
        self.assertTrue(waits)
        self.assertAlmostEqual(waits[0], 0.5, places=2)

    def test_suppressed_crumb_does_not_pace(self):
        yf_client.note_crumb_429()

        def _boom(method, url, *args, **kwargs):
            raise AssertionError(url)

        with mock.patch.object(yf_client, "_original_session_request", _boom):
            with mock.patch.object(yf_client.time, "sleep") as slept:
                response = yf_client.SESSION.get(
                    "https://query1.finance.yahoo.com/v1/test/getcrumb"
                )
        slept.assert_not_called()
        self.assertEqual(response.status_code, 429)


class TestSharedYahooSession(unittest.TestCase):
    def test_crumb_holder_is_a_process_singleton(self):
        from yfinance.data import YfData

        first = YfData(session=yf_client.SESSION)
        second = YfData(session=yf_client.SESSION)
        self.assertIs(first, second)
        self.assertIs(first._session, yf_client.SESSION)
