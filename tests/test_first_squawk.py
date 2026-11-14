"""First Squawk verbatim parser. No network."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from first_squawk import (
    build_query,
    error_detail,
    estimate_cost_usd,
    keyword_hit,
    parse_release_numbers,
    posts_from_search,
    release_component,
    search_params,
    stop_reason,
)


class ReleaseNumberTests(unittest.TestCase):
    def test_full_parenthetical_keeps_the_printed_tokens(self):
        actual, forecast, prior = parse_release_numbers(
            "CPI Actual 0.3% (Forecast 0.2%, Prior 0.1%)"
        )
        self.assertEqual((actual, forecast, prior), ("0.3%", "0.2%", "0.1%"))

    def test_suffix_is_not_expanded(self):
        actual, forecast, prior = parse_release_numbers(
            "Actual 256K (Forecast 180K, Prior 150K)"
        )
        self.assertEqual((actual, forecast, prior), ("256K", "180K", "150K"))

    def test_missing_number_stays_none(self):
        actual, forecast, prior = parse_release_numbers("CPI Actual 3.2%")
        self.assertEqual(actual, "3.2%")
        self.assertIsNone(forecast)
        self.assertIsNone(prior)
        self.assertEqual(parse_release_numbers("CPI rose 0.3 percent"), (None, None, None))
        self.assertEqual(
            parse_release_numbers("Forecast 1.0 and then a pause"),
            (None, "1.0", None),
        )

    def test_rate_does_not_match_inside_strategy(self):
        self.assertFalse(keyword_hit("The strategy is unchanged", ["rate"]))
        self.assertTrue(keyword_hit("FED HOLDS THE RATE", ["Fed", "rate"]))

    def test_est_and_previous_map_without_taking_a_clock(self):
        actual, forecast, prior = parse_release_numbers(
            "US PPI (MOM) (OCT)ACTUAL: 0.3% VS 1.3% PREVIOUS; EST 0.4%"
        )
        self.assertEqual((actual, forecast, prior), ("0.3%", "0.4%", "1.3%"))
        self.assertEqual(
            parse_release_numbers("Actual 1 EXP 2 PREV 3"),
            ("1", "2", "3"),
        )
        self.assertEqual(
            parse_release_numbers("CONSENSUS: 4.0%"),
            (None, "4.0%", None),
        )
        self.assertEqual(
            parse_release_numbers("The print is at 8:30 EST"),
            (None, None, None),
        )

    def test_ppi_components_stay_on_their_own_series(self):
        posts = (
            (
                "US PPI (MOM) (OCT)ACTUAL: 0.3% VS 1.3% PREVIOUS; EST 0.4%",
                "headline_mom",
                ("0.3%", "0.4%", "1.3%"),
            ),
            (
                "US PPI (YOY) (OCT)ACTUAL: 6.5% VS 6.3% PREVIOUS; EST 6.6%",
                "headline_yoy",
                ("6.5%", "6.6%", "6.3%"),
            ),
            (
                "US CORE PPI (MOM) (OCT)ACTUAL: 0.3% VS 0.7% PREVIOUS; EST 0.3%",
                "core_mom",
                ("0.3%", "0.3%", "0.7%"),
            ),
            (
                "US CORE PPI (YOY) (OCT)ACTUAL: 5.0% VS 5.0% PREVIOUS; EST 5.0%",
                "core_yoy",
                ("5.0%", "5.0%", "5.0%"),
            ),
        )
        for text, component, numbers in posts:
            self.assertEqual(parse_release_numbers(text), numbers)
            self.assertEqual(release_component(text), component)
        self.assertIsNone(release_component("US PPI (MOM) and (YOY) ACTUAL: 0.3%"))


class WindowAndCostTests(unittest.TestCase):
    def test_query_leaves_bare_words_unquoted(self):
        self.assertEqual(
            build_query(["CPI", "inflation"]),
            "from:FirstSquawk (CPI OR inflation) -is:retweet",
        )
        self.assertEqual(
            build_query(["payrolls", "nonfarm", "unemployment"]),
            "from:FirstSquawk (payrolls OR nonfarm OR unemployment) -is:retweet",
        )
        self.assertEqual(
            build_query(["Fed", "FOMC", "rate"]),
            "from:FirstSquawk (Fed OR FOMC OR rate) -is:retweet",
        )
        self.assertEqual(
            build_query(['core CPI']),
            'from:FirstSquawk ("core CPI") -is:retweet',
        )

    def test_search_params_are_the_release_window(self):
        start = datetime(2026, 11, 16, 18, 59, tzinfo=timezone.utc)
        end = datetime(2026, 11, 16, 19, 5, tzinfo=timezone.utc)
        params = search_params(["CPI", "inflation"], start, end)
        self.assertEqual(params["query"], "from:FirstSquawk (CPI OR inflation) -is:retweet")
        self.assertEqual(params["start_time"], "2026-11-16T18:59:00Z")
        self.assertEqual(params["end_time"], "2026-11-16T19:05:00Z")
        self.assertEqual(params["max_results"], 10)
        self.assertEqual(params["tweet.fields"], "created_at")

    def test_cost_is_half_a_cent_per_post_returned(self):
        self.assertEqual(estimate_cost_usd(3), 0.015)
        self.assertEqual(estimate_cost_usd(10), 0.05)
        self.assertEqual(estimate_cost_usd(0), 0.0)
        self.assertIsNone(estimate_cost_usd(None))

    def test_429_and_spend_cap_stop_and_a_plain_500_does_not(self):
        self.assertEqual(stop_reason(429, ""), "rate")
        self.assertEqual(stop_reason(429, '{"title":"Usage Capped"}'), "rate")
        self.assertEqual(
            stop_reason(402, '{"title":"CreditsDepleted","detail":"credits depleted"}'),
            "spend",
        )
        self.assertEqual(
            stop_reason(403, "Your account has reached its billing cycle spend cap."),
            "spend",
        )
        self.assertIsNone(stop_reason(500, '{"title":"Internal Server Error"}'))
        self.assertIsNone(stop_reason(403, '{"title":"Forbidden","detail":"Not allowed"}'))
        self.assertIsNone(stop_reason(200, "this post says spend cap"))
        self.assertIn("HTTP 429", error_detail(429, '{"title":"Too Many Requests"}'))
        self.assertIn(
            "spend cap",
            error_detail(402, '{"detail":"credits depleted"}'),
        )

    def test_drops_a_post_without_id_or_time_and_keeps_the_api_text(self):
        start = datetime(2026, 11, 16, 18, 59, tzinfo=timezone.utc)
        end = datetime(2026, 11, 16, 19, 5, tzinfo=timezone.utc)
        inside = "2026-11-16T19:02:00.000Z"
        payload = {
            "data": [
                {
                    "id": "1",
                    "created_at": inside,
                    "text": "CPI unchanged",
                    "actual": "9.9",
                },
                {"created_at": inside, "text": "CPI Actual 1.0%"},
                {"id": "2", "text": "CPI Actual 1.0%"},
                {
                    "id": "3",
                    "created_at": "2026-11-16T19:06:00Z",
                    "text": "CPI Actual 1.0%",
                },
                {
                    "id": "4",
                    "created_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "text": "CPI Actual 1.2% (Forecast 1.1%)",
                },
                {
                    "id": "5",
                    "created_at": inside,
                    "text": "the strategy review",
                },
                {
                    "id": "../evil",
                    "created_at": inside,
                    "text": "CPI Actual 1.0%",
                },
                {
                    "id": "6",
                    "created_at": "2026-11-16T19:02:00",
                    "text": "CPI Actual 1.0%",
                },
            ],
            "meta": {"result_count": 8},
        }
        kept = posts_from_search(payload, start, end)
        self.assertEqual([row["url"] for row in kept], [
            "https://x.com/FirstSquawk/status/1",
            "https://x.com/FirstSquawk/status/4",
            "https://x.com/FirstSquawk/status/5",
        ])
        self.assertIsNone(kept[0]["actual"])
        self.assertEqual(kept[1]["actual"], "1.2%")
        self.assertEqual(kept[1]["forecast"], "1.1%")
        self.assertIsNone(kept[1]["prior"])
        self.assertEqual(kept[2]["text"], "the strategy review")


class ProbeCommandTests(unittest.TestCase):
    def test_missing_token_prints_disabled_and_does_not_call(self):
        import first_squawk

        env = os.environ.copy()
        env.pop("X_BEARER_TOKEN", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("first_squawk.search_recent") as search:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = first_squawk.main()
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().strip(), "disabled: no token")
        search.assert_not_called()

    def test_probe_requests_the_last_ten_and_hides_the_token(self):
        import first_squawk

        payload = {
            "data": [
                {
                    "id": "99",
                    "text": "CPI 1",
                    "created_at": "2026-11-16T19:02:00Z",
                }
            ],
            "meta": {"result_count": 1},
        }
        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "secret-token"}):
            with mock.patch(
                "first_squawk.search_recent",
                return_value=(200, json.dumps(payload), 4),
            ) as search:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = first_squawk.main()
        self.assertEqual(code, 0)
        params, token = search.call_args[0][:2]
        self.assertEqual(token, "secret-token")
        self.assertEqual(params["query"], "from:FirstSquawk -is:retweet")
        self.assertEqual(params["max_results"], 10)
        self.assertEqual(params["tweet.fields"], "created_at")
        self.assertNotIn("start_time", params)
        self.assertNotIn("end_time", params)
        text = buf.getvalue()
        self.assertIn("https://x.com/FirstSquawk/status/99", text)
        self.assertIn("CPI 1", text)
        self.assertIn("posts=1 estimated cost=$0.005000", text)
        self.assertNotIn("secret-token", text)

    def test_probe_logs_only_the_exception_type(self):
        import first_squawk

        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "secret-token"}):
            with mock.patch(
                "first_squawk.search_recent",
                side_effect=RuntimeError("Bearer secret-token"),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = first_squawk.main()
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", buf.getvalue())
        self.assertNotIn("secret-token", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
