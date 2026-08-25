"""Disabled generators and futures: write nothing when the fetch is empty."""

from __future__ import annotations

import unittest
from unittest import mock

import sector_scrapers


class TestFuturesNoZeroWrite(unittest.TestCase):
    def test_writes_nothing_when_pct_and_price_missing(self):
        fake = mock.Mock()
        fake.info = {}
        fake.history.return_value = mock.Mock(empty=True)
        with mock.patch("sector_scrapers.yf.Ticker", return_value=fake):
            with mock.patch("sector_scrapers.save_headline") as save:
                with mock.patch("sector_scrapers.time.sleep"):
                    sector_scrapers.fetch_overnight_futures()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
