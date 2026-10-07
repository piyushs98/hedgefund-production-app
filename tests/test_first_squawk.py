"""First Squawk page parser. No network."""

import unittest
from datetime import datetime, timezone

from first_squawk import posts_from_html, route


def _page(ms: int, text: str) -> str:
    return (
        f'created_at_ms:{ms},display_text_range:[0,10],'
        f'full_text:"{text}",hashtag_entities:[]'
    )


class FirstSquawkParseTests(unittest.TestCase):
    def test_keeps_a_fresh_post_and_drops_the_account_birthday(self):
        now = datetime(2026, 10, 6, 23, 30, tzinfo=timezone.utc)
        fresh_ms = int(now.timestamp() * 1000) - 60_000
        html = (
            "created_at_ms:1437921619100,screen_name:FirstSquawk "
            + _page(fresh_ms, "NVIDIA SHARES CLIMB 1.3% TO ALL-TIME HIGH")
        )
        posts = posts_from_html(html, now=now)
        self.assertEqual(posts, ["NVIDIA SHARES CLIMB 1.3% TO ALL-TIME HIGH"])

    def test_named_line_stays_on_that_ticker(self):
        self.assertEqual(
            route("NVIDIA SHARES CLIMB 1.3% TO ALL-TIME HIGH"),
            ["NVDA"],
        )
        self.assertEqual(
            route("THE S&P 500 ROSE 0.6% TO A RECORD HIGH"),
            ["SPY"],
        )

    def test_fed_line_with_no_name_goes_to_fed_only(self):
        self.assertEqual(
            route("FED'S DALY SEES LINGERING INFLATION RISKS FROM AI"),
            ["FED"],
        )

    def test_unrelated_line_is_dropped(self):
        self.assertEqual(
            route("U.S. VICE PRESIDENT VANCE SAYS HE IS UNDECIDED ABOUT A RUN"),
            [],
        )

    def test_two_names_keep_both(self):
        self.assertEqual(
            route("APPLE AND NVIDIA SIGN A SUPPLY DEAL"),
            ["AAPL", "NVDA"],
        )


if __name__ == "__main__":
    unittest.main()
