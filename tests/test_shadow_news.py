"""Shadow news store. No network. The scorer is not pointed at these tables."""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import first_squawk
import llm_chain
import pre_market_meeting
import shadow_news

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc

_RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
<channel>
<title>fixture</title>
<item>
  <title>CPI release</title>
  <link>https://www.bls.gov/example/cpi</link>
  <pubDate>Fri, 13 Nov 2026 13:30:00 GMT</pubDate>
  <description>&lt;p&gt;Index &lt;b&gt;0.3%&lt;/b&gt;. Actual 1.0 (Forecast 2.0, Prior 3.0)&lt;/p&gt;</description>
</item>
<item>
  <title>No date</title>
  <link>https://www.bls.gov/example/nodate</link>
  <description>skip me</description>
</item>
<item>
  <title>No link</title>
  <pubDate>Fri, 13 Nov 2026 13:30:00 GMT</pubDate>
  <description>skip me</description>
</item>
</channel>
</rss>
"""


def _ct(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CT).astimezone(UTC)


def _search_body(tweet_id, created, text, count=1):
    return json.dumps(
        {
            "data": [{"id": tweet_id, "created_at": created, "text": text}],
            "meta": {"result_count": count},
        }
    )


class RssTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _fetch(self, url):
        self.urls.append(url)
        if "federalreserve" in url:
            raise TimeoutError("slow")
        if "bls.gov" in url:
            return 200, _RSS
        raise AssertionError(url)

    def test_one_feed_failing_does_not_store_and_skips_undated_items(self):
        self.urls = []
        shadow_news.poll_rss(
            _ct(2026, 11, 16, 7, 0),
            set(),
            db_path=self.db,
            fetch=self._fetch,
        )
        self.assertEqual(len(self.urls), 2)
        with sqlite3.connect(self.db) as conn:
            items = conn.execute(
                "SELECT source, url, raw_text, actual, forecast, prior FROM shadow_items"
            ).fetchall()
            health = conn.execute(
                "SELECT source, status, http_status, rows_new, detail FROM source_health ORDER BY id"
            ).fetchall()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], "bls")
        self.assertEqual(items[0][1], "https://www.bls.gov/example/cpi")
        self.assertEqual(items[0][2], "Index 0.3%. Actual 1.0 (Forecast 2.0, Prior 3.0)")
        self.assertEqual(items[0][3:], (None, None, None))
        by_source = {row[0]: row for row in health}
        self.assertEqual(by_source["fed"][1], "fail")
        self.assertEqual(by_source["fed"][4], "TimeoutError")
        self.assertEqual(by_source["bls"][1], "ok")
        self.assertEqual(by_source["bls"][3], 1)
        self.assertNotIn("treasury", by_source)

    def test_non_rss_and_http_error_store_nothing(self):
        def fetch(url):
            if "bls.gov" in url:
                return 503, b""
            return 200, b"<html>not a feed</html>"

        shadow_news.poll_rss(_ct(2026, 11, 16, 8), set(), db_path=self.db, fetch=fetch)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shadow_items").fetchone()[0], 0)
            rows = {
                row[0]: row
                for row in conn.execute(
                    "SELECT source, status, http_status, detail, rows_new FROM source_health"
                )
            }
        self.assertEqual(rows["fed"][1], "fail")
        self.assertEqual(rows["fed"][3], "not rss")
        self.assertEqual(rows["bls"][1], "fail")
        self.assertEqual(rows["bls"][2], 503)
        self.assertEqual(rows["bls"][4], 0)

    def test_same_item_is_not_inserted_twice(self):
        calls = {"n": 0}

        empty = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>e</title></channel></rss>"""

        def fetch(url):
            calls["n"] += 1
            if "bls.gov" in url:
                return 200, _RSS
            return 200, empty

        start = _ct(2026, 11, 16, 7, 0)
        shadow_news.poll_rss(start, set(), db_path=self.db, fetch=fetch)
        shadow_news.poll_rss(start + timedelta(minutes=4), set(), db_path=self.db, fetch=fetch)
        shadow_news.poll_rss(start + timedelta(minutes=5), set(), db_path=self.db, fetch=fetch)
        self.assertEqual(calls["n"], 4)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM shadow_items").fetchone()[0], 1
            )
            bls = conn.execute(
                "SELECT rows_new FROM source_health WHERE source='bls' ORDER BY id"
            ).fetchall()
        self.assertEqual([row[0] for row in bls], [1, 0])

    def test_window_is_trading_days_from_07_through_15_ct(self):
        holidays = {"2026-11-26"}
        self.assertFalse(shadow_news.in_rss_window(_ct(2026, 11, 14, 12), holidays))
        self.assertFalse(shadow_news.in_rss_window(_ct(2026, 11, 26, 12), holidays))
        self.assertFalse(shadow_news.in_rss_window(_ct(2026, 11, 16, 6, 59), set()))
        self.assertTrue(shadow_news.in_rss_window(_ct(2026, 11, 16, 7, 0), set()))
        self.assertTrue(shadow_news.in_rss_window(_ct(2026, 11, 16, 15, 0), set()))
        self.assertFalse(shadow_news.in_rss_window(_ct(2026, 11, 16, 15, 1), set()))

    def test_a_bom_does_not_drop_the_item(self):
        items = shadow_news.parse_rss_items(b"\xef\xbb\xbf" + _RSS, "fed")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "CPI release")
        self.assertEqual(items[0]["source"], "fed")

        def fetch(_url):
            raise AssertionError("polled outside the window")

        shadow_news.poll_rss(_ct(2026, 11, 16, 6, 59), set(), db_path=self.db, fetch=fetch)
        self.assertFalse(os.path.exists(self.db))

    def test_headlines_table_is_not_written(self):
        shadow_news.ensure_schema(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "CREATE TABLE headlines (id INTEGER PRIMARY KEY, title TEXT)"
            )
            conn.execute("INSERT INTO headlines (title) VALUES ('kept')")
        shadow_news.poll_rss(
            _ct(2026, 11, 16, 9),
            set(),
            db_path=self.db,
            fetch=lambda url: (200, _RSS),
        )
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT title FROM headlines").fetchall(), [("kept",)]
            )


class SquawkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")
        shadow_news.ensure_schema(self.db)
        shadow_news._bad_release_logged.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, event_id="cpi", name="CPI", release="2026-11-16T19:00:00Z",
               keywords=None, kind=None, ticker=None):
        if keywords is None:
            keywords = ["CPI", "inflation"]
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """INSERT INTO calendar_events
                   (event_id, name, release_at, keywords, kind, ticker)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    name,
                    release,
                    json.dumps(keywords) if keywords is not None else None,
                    kind,
                    ticker,
                ),
            )

    def test_stores_a_verbatim_post_and_does_not_retry(self):
        self._event()
        calls = []

        def client(params, token):
            calls.append(params)
            self.assertEqual(token, "test-key")
            self.assertEqual(params["max_results"], 10)
            self.assertEqual(params["tweet.fields"], "created_at")
            return 200, _search_body(
                "10",
                "2026-11-16T19:02:00Z",
                "CPI Actual 0.3% (Forecast 0.2%, Prior 0.1%)",
            ), 30

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db,
            key="test-key",
            client=client,
        )
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 8, tzinfo=UTC),
            db_path=self.db,
            key="test-key",
            client=client,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["query"], "from:FirstSquawk (CPI OR inflation) -is:retweet")
        self.assertEqual(calls[0]["start_time"], "2026-11-16T18:59:00Z")
        self.assertEqual(calls[0]["end_time"], "2026-11-16T19:05:00Z")
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                """SELECT source, actual, forecast, prior, raw_text, event_id, url, published_at
                   FROM shadow_items"""
            ).fetchone()
            health = conn.execute(
                """SELECT status, posts_returned, prompt_tokens, estimated_cost_usd,
                          rows_new, http_status
                   FROM source_health"""
            ).fetchone()
        self.assertEqual(row[0], "firstsquawk")
        self.assertEqual(row[1:4], ("0.3%", "0.2%", "0.1%"))
        self.assertIn("Actual 0.3%", row[4])
        self.assertEqual(row[5], "cpi")
        self.assertEqual(row[6], "https://x.com/FirstSquawk/status/10")
        self.assertEqual(row[7], "2026-11-16T19:02:00Z")
        self.assertEqual(health[0], "ok")
        self.assertEqual(health[1], 1)
        self.assertIsNone(health[2])
        self.assertAlmostEqual(health[3], 0.005)
        self.assertEqual(health[4], 1)
        self.assertEqual(health[5], 200)

    def test_ppi_post_stores_forecast_prior_and_component(self):
        self._event(event_id="ppi", name="Producer Price Index", keywords=["PPI"])
        text = "US CORE PPI (MOM) (OCT)ACTUAL: 0.3% VS 0.7% PREVIOUS; EST 0.3%"

        def client(params, token):
            return 200, _search_body("21", "2026-11-16T19:02:00Z", text), 12

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db,
            key="test-key",
            client=client,
        )
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT actual, forecast, prior, component FROM shadow_items"
            ).fetchone()
        self.assertEqual(row, ("0.3%", "0.3%", "0.7%", "core_mom"))

    def test_empty_call_retries_once_then_records_a_miss(self):
        self._event()
        calls = []

        def client(params, token):
            calls.append(params)
            return 200, json.dumps({"meta": {"result_count": 0}}), 10

        primary = datetime(2026, 11, 16, 19, 3, tzinfo=UTC)
        shadow_news.consider_squawk(primary, db_path=self.db, key="k", client=client)
        shadow_news.consider_squawk(
            primary + timedelta(minutes=4), db_path=self.db, key="k", client=client
        )
        self.assertEqual(len(calls), 1)
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 8, tzinfo=UTC),
            db_path=self.db,
            key="k",
            client=client,
        )
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 9, tzinfo=UTC),
            db_path=self.db,
            key="k",
            client=client,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["start_time"], "2026-11-16T18:59:00Z")
        self.assertEqual(calls[0]["end_time"], "2026-11-16T19:05:00Z")
        self.assertEqual(calls[1]["start_time"], calls[0]["start_time"])
        self.assertEqual(calls[1]["end_time"], calls[0]["end_time"])
        with sqlite3.connect(self.db) as conn:
            statuses = [
                row[0]
                for row in conn.execute(
                    "SELECT status FROM source_health ORDER BY id"
                )
            ]
            state = conn.execute("SELECT status FROM squawk_calls").fetchone()[0]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM shadow_items").fetchone()[0], 0)
        self.assertEqual(statuses, ["ok", "miss"])
        self.assertEqual(state, "miss")

    def test_a_late_wake_records_a_miss_without_calling(self):
        self._event()

        def client(params, token):
            raise AssertionError("called late")

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 20, tzinfo=UTC),
            db_path=self.db,
            key="k",
            client=client,
        )
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT status, http_status, detail FROM source_health"
            ).fetchone()
        self.assertEqual(row, ("miss", None, "window missed"))

    def test_missing_token_skips_without_a_retry_or_a_fallback(self):
        self._event()

        def client(params, token):
            raise AssertionError("called without a token")

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db,
            key="",
            client=client,
        )
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 8, tzinfo=UTC),
            db_path=self.db,
            key="",
            client=client,
        )
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("SELECT status FROM source_health").fetchall()
            state = conn.execute("SELECT status FROM squawk_calls").fetchone()[0]
        self.assertEqual(rows, [("disabled: no token",)])
        self.assertEqual(state, "skipped")

    def test_fomc_calls_the_statement_and_the_1335_ct_presser(self):
        self._event(
            event_id="fomc",
            name="FOMC",
            keywords=["Fed", "FOMC", "rate"],
            kind="fomc",
        )
        calls = []

        def client(params, token):
            calls.append(params)
            if len(calls) == 1:
                self.assertEqual(params["start_time"], "2026-11-16T18:59:00Z")
                self.assertEqual(params["end_time"], "2026-11-16T19:05:00Z")
                created = "2026-11-16T19:01:00Z"
                text = "FED HOLDS"
            else:
                self.assertEqual(params["start_time"], "2026-11-16T19:31:00Z")
                self.assertEqual(params["end_time"], "2026-11-16T19:37:00Z")
                created = "2026-11-16T19:33:00Z"
                text = "POWELL SPEAKS Fed"
            return 200, _search_body(str(len(calls)), created, text), 12

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 35, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        self.assertEqual(len(calls), 2)
        slots = [spec.name for spec in shadow_news.slots_for(
            datetime(2026, 11, 16, 19, 0, tzinfo=UTC), "fomc", "FOMC"
        )]
        self.assertEqual(slots, ["statement", "presser"])
        presser = shadow_news.slots_for(
            datetime(2026, 11, 16, 19, 0, tzinfo=UTC), "fomc", "FOMC"
        )[1]
        self.assertEqual(presser.call_at.astimezone(CT).strftime("%H:%M"), "13:35")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM shadow_items").fetchone()[0], 2
            )

    def test_no_keywords_does_not_call(self):
        self._event(keywords=[])

        def client(params, token):
            raise AssertionError("called")

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        with sqlite3.connect(self.db) as conn:
            detail = conn.execute("SELECT detail FROM source_health").fetchone()[0]
        self.assertEqual(detail, "no keywords")

    def test_empty_calendar_makes_no_call(self):
        def client(params, token):
            raise AssertionError("called")

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0], 0
            )

    def test_http_429_stops_until_a_later_event(self):
        self._event()
        self._event(
            event_id="nfp",
            name="NFP",
            keywords=["payrolls", "nonfarm", "unemployment"],
        )
        calls = []

        def client(params, token):
            calls.append(params["query"])
            if len(calls) == 1:
                return 429, json.dumps({
                    "title": "Too Many Requests",
                    "detail": "Rate limit exceeded",
                }), 4
            return 200, _search_body("55", "2026-11-16T19:02:00Z", "payrolls 1"), 4

        at_release = datetime(2026, 11, 16, 19, 3, tzinfo=UTC)
        shadow_news.consider_squawk(at_release, db_path=self.db, key="k", client=client)
        self.assertEqual(calls, ["from:FirstSquawk (CPI OR inflation) -is:retweet"])
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 4, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        self.assertEqual(len(calls), 2)
        self.assertIn("payrolls", calls[1])
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 8, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        self.assertEqual(len(calls), 2)
        with sqlite3.connect(self.db) as conn:
            cpi = conn.execute(
                """SELECT status, http_status, detail, estimated_cost_usd
                   FROM source_health WHERE event_id='cpi'"""
            ).fetchone()
            state = conn.execute(
                "SELECT status FROM squawk_calls WHERE event_id='cpi'"
            ).fetchone()[0]
        self.assertEqual(cpi[0], "fail")
        self.assertEqual(cpi[1], 429)
        self.assertIn("HTTP 429", cpi[2])
        self.assertAlmostEqual(cpi[3], 0.0)
        self.assertEqual(state, "skipped")

    def test_fomc_statement_429_skips_the_presser(self):
        self._event(
            event_id="fomc",
            name="FOMC",
            keywords=["Fed", "FOMC", "rate"],
            kind="fomc",
        )
        calls = []

        def client(params, token):
            calls.append(params["query"])
            return 429, "{}", 3

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 35, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        self.assertEqual(len(calls), 1)
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT slot, status FROM squawk_calls ORDER BY slot"
            ).fetchall()
            presser = conn.execute(
                """SELECT http_status, detail FROM source_health
                   WHERE slot='presser'"""
            ).fetchone()
        self.assertEqual(rows, [("presser", "skipped"), ("statement", "skipped")])
        self.assertIsNone(presser[0])
        self.assertIn("HTTP 429", presser[1])

    def test_spend_cap_does_not_retry(self):
        self._event()
        calls = []

        def client(params, token):
            calls.append(params)
            return 402, json.dumps({
                "title": "CreditsDepleted",
                "detail": "credits depleted",
            }), 3

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 8, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        self.assertEqual(len(calls), 1)
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT status, detail FROM source_health"
            ).fetchone()
            state = conn.execute("SELECT status FROM squawk_calls").fetchone()[0]
        self.assertEqual(row[0], "fail")
        self.assertIn("spend cap", row[1])
        self.assertEqual(state, "skipped")

    def test_http_500_retries_once_then_misses(self):
        self._event()
        calls = []

        def client(params, token):
            calls.append(params["start_time"])
            return 500, json.dumps({"title": "Internal Server Error"}), 3

        primary = datetime(2026, 11, 16, 19, 3, tzinfo=UTC)
        shadow_news.consider_squawk(primary, db_path=self.db, key="k", client=client)
        shadow_news.consider_squawk(
            primary + timedelta(minutes=4), db_path=self.db, key="k", client=client
        )
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 8, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 9, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        self.assertEqual(len(calls), 2)
        with sqlite3.connect(self.db) as conn:
            statuses = [
                row[0]
                for row in conn.execute("SELECT status FROM source_health ORDER BY id")
            ]
            state = conn.execute("SELECT status FROM squawk_calls").fetchone()[0]
        self.assertEqual(statuses, ["fail", "fail"])
        self.assertEqual(state, "miss")

    def test_a_transport_error_has_no_cost(self):
        self._event()

        def client(params, token):
            raise TimeoutError("slow")

        shadow_news.consider_squawk(
            datetime(2026, 11, 16, 19, 3, tzinfo=UTC),
            db_path=self.db, key="k", client=client,
        )
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                """SELECT status, http_status, posts_returned, estimated_cost_usd, detail
                   FROM source_health"""
            ).fetchone()
        self.assertEqual(row[0], "fail")
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertEqual(row[4], "TimeoutError")


class BriefAndSpendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_brief_line_names_the_feeds_and_both_clocks(self):
        shadow_news.ensure_schema(self.db)
        now = datetime(2026, 11, 16, 14, 15, tzinfo=UTC)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """INSERT INTO source_health
                   (source, status, http_status, rows_new, fetched_at)
                   VALUES ('fed', 'ok', 200, 2, ?),
                          ('bls', 'ok', 200, 0, ?),
                          ('yahoo', 'fail', NULL, 0, ?),
                          ('yahoo_shadow', 'fail', NULL, 0, ?)""",
                (shadow_news.utc_iso(now),) * 4,
            )
            conn.execute(
                """INSERT INTO calendar_events (event_id, name, release_at, keywords)
                   VALUES ('cpi', 'CPI', '2026-11-16T13:30:00Z', '["CPI"]')"""
            )
        line = shadow_news.brief_line(now, db_path=self.db)
        self.assertEqual(
            line,
            "Shadow feeds: fed ok 2 new, bls ok 0 new. "
            "Yahoo failures: 1. Events: CPI 08:30 ET / 07:30 CT.",
        )
        empty = shadow_news.brief_line(now, db_path=os.path.join(self.tmp.name, "missing.db"))
        self.assertIn("unavailable", empty)

    def test_no_events_says_none_scheduled(self):
        shadow_news.ensure_schema(self.db)
        line = shadow_news.brief_line(
            datetime(2026, 11, 16, 14, 15, tzinfo=UTC), db_path=self.db
        )
        self.assertIn("fed no fetch", line)
        self.assertIn("bls no fetch", line)
        self.assertNotIn("treasury", line)
        self.assertIn("Yahoo failures: 0.", line)
        self.assertIn("Events: none scheduled.", line)

    def test_week_spend_sums_this_chicago_week_only(self):
        shadow_news.ensure_schema(self.db)
        inside = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
        before = datetime(2026, 11, 16, 5, 59, tzinfo=UTC)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """INSERT INTO source_health
                   (source, status, http_status, rows_new, posts_returned,
                    prompt_tokens, completion_tokens, posts_fetched, users_fetched,
                    estimated_cost_usd, fetched_at)
                   VALUES ('firstsquawk', 'ok', 200, 1, 1, 1000, 50, 4, 0, 0.10, ?),
                          ('firstsquawk', 'ok', 200, 1, 1, 9, 9, 9, 0, 9.0, ?),
                          ('firstsquawk', 'miss', NULL, 0, 0, NULL, NULL, NULL, NULL, NULL, ?)""",
                (
                    shadow_news.utc_iso(inside),
                    shadow_news.utc_iso(before),
                    shadow_news.utc_iso(inside + timedelta(hours=1)),
                ),
            )
        lines = shadow_news.x_spend_lines(
            self.db, datetime(2026, 11, 18, 15, 0, tzinfo=CT)
        )
        text = "\n".join(lines)
        self.assertIn("X SPEND (this Chicago week; posts × $0.005)", text)
        self.assertIn("calls: 1", text)
        self.assertIn("posts returned: 1", text)
        self.assertIn("$0.005", text)
        self.assertIn("estimated cost: $0.100000", text)
        self.assertNotIn("grok", text)
        self.assertNotIn("prompt tokens", text)
        self.assertNotIn("9.0", text)
        missing = "\n".join(shadow_news.x_spend_lines(os.path.join(self.tmp.name, "nope.db")))
        self.assertIn("not on this disk yet", missing)
        self.assertIn("estimated cost: $0.000000", missing)

    def test_morning_brief_appends_the_line_after_the_model_text(self):
        line = (
            "Shadow feeds: fed ok 0 new, bls ok 0 new. "
            "Yahoo failures: 0. Events: none scheduled."
        )
        with mock.patch.object(pre_market_meeting, "get_overnight_data", return_value="tape"):
            with mock.patch.object(llm_chain, "generate_text", return_value="MODEL TEXT"):
                with mock.patch("broadcaster.send_discord_alert", return_value=True) as send:
                    with mock.patch("midday_delta.store_morning_briefing") as store:
                        with mock.patch("shadow_news.brief_line", return_value=line):
                            text = pre_market_meeting.generate_morning_briefing()
        self.assertLess(text.index("MODEL TEXT"), text.index("Shadow feeds:"))
        sent = send.call_args[0][0]
        self.assertLess(sent.index("MODEL TEXT"), sent.index("Shadow feeds:"))
        self.assertIn("Shadow feeds:", store.call_args[0][0])

    def test_scorer_does_not_read_the_shadow_store(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        def _text(name):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                return handle.read()

        for name in ("scoring_engine.py", "news_memory.py", "squawk_shadow.py"):
            text = _text(name)
            self.assertNotIn("shadow_news", text)
            self.assertNotIn("shadow_items", text)
            self.assertNotIn("firstsquawk_unverified", text)
        shadow = _text("shadow_news.py")
        page = _text("first_squawk.py")
        bot = _text("master_bot.py")
        report = _text("ops/report.py")
        self.assertNotIn("news_memory", shadow)
        self.assertNotIn("scoring_engine", shadow)
        self.assertNotIn("save_headline", shadow)
        self.assertNotIn("XAI_API_KEY", shadow)
        self.assertNotIn("api.x.ai", shadow)
        self.assertNotIn("firstsquawk_unverified", shadow)
        self.assertNotIn("posts_from_html", page)
        self.assertNotIn("fetch_first_squawk", page)
        self.assertNotIn("api.x.ai", page)
        self.assertIn('https://x.com/{HANDLE}/status/', page)
        self.assertIn('HANDLE = "FirstSquawk"', page)
        self.assertNotIn("pull_first_squawk", bot)
        self.assertNotIn("x.com", bot)
        self.assertNotIn("xai_spend_lines", report)
        self.assertNotIn("XAI SPEND", report)


def _yield_frame(moments, closes):
    return pd.DataFrame({"Close": closes}, index=pd.DatetimeIndex(moments))


class YieldAndYahooHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "news.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, event_id, release_at):
        shadow_news.ensure_schema(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """INSERT INTO calendar_events (event_id, name, release_at, keywords)
                   VALUES (?, ?, ?, '["CPI"]')""",
                (event_id, event_id, release_at),
            )

    def test_empty_calendar_does_not_call_yahoo(self):
        def history(start, end):
            raise AssertionError("yield fetch")

        shadow_news.consider_yields(
            datetime(2026, 11, 16, 14, 0, tzinfo=UTC),
            db_path=self.db,
            history=history,
        )

    def test_one_call_after_the_window_stores_percent_and_does_not_repeat(self):
        self._event("cpi", "2026-11-16T13:30:00Z")
        calls = []

        def history(start, end):
            calls.append((start, end))
            return {
                "frame": _yield_frame(
                    [
                        datetime(2026, 11, 16, 12, 59, tzinfo=UTC),
                        datetime(2026, 11, 16, 13, 0, tzinfo=UTC),
                        datetime(2026, 11, 16, 13, 45, tzinfo=UTC),
                    ],
                    [9.99, 5.10, 5.154],
                ),
                "error": None,
                "latency_ms": 12,
                "crumb429": False,
            }

        early = datetime(2026, 11, 16, 13, 44, tzinfo=UTC)
        shadow_news.consider_yields(early, db_path=self.db, history=history)
        self.assertEqual(calls, [])
        due = datetime(2026, 11, 16, 13, 45, tzinfo=UTC)
        shadow_news.consider_yields(due, db_path=self.db, history=history)
        shadow_news.consider_yields(
            datetime(2026, 11, 16, 14, 0, tzinfo=UTC),
            db_path=self.db,
            history=history,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], datetime(2026, 11, 16, 13, 0, tzinfo=UTC))
        self.assertEqual(calls[0][1], datetime(2026, 11, 16, 13, 45, tzinfo=UTC))
        with sqlite3.connect(self.db) as conn:
            bars = conn.execute(
                "SELECT bar_at, yield_pct FROM yield_bars ORDER BY bar_at"
            ).fetchall()
            health = conn.execute(
                "SELECT source, status, rows_new, detail FROM source_health"
            ).fetchone()
        self.assertEqual(
            [(row[0], row[1]) for row in bars],
            [
                ("2026-11-16T13:00:00Z", 5.10),
                ("2026-11-16T13:45:00Z", 5.154),
            ],
        )
        self.assertEqual(health[0], "yahoo_shadow")
        self.assertEqual(health[1], "ok")
        self.assertEqual(health[2], 2)
        self.assertIn("crumb429=no", health[3])

    def test_a_failed_yield_stores_null_and_is_not_retried(self):
        self._event("cpi", "2026-11-16T13:30:00Z")
        calls = {"n": 0}

        def history(start, end):
            calls["n"] += 1
            raise TimeoutError("slow")

        when = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)
        shadow_news.consider_yields(when, db_path=self.db, history=history)
        shadow_news.consider_yields(when, db_path=self.db, history=history)
        self.assertEqual(calls["n"], 1)
        with sqlite3.connect(self.db) as conn:
            bar = conn.execute(
                "SELECT bar_at, yield_pct FROM yield_bars"
            ).fetchone()
            health = conn.execute(
                "SELECT status, detail FROM source_health"
            ).fetchone()
        self.assertEqual(bar[0], "2026-11-16T13:45:00Z")
        self.assertIsNone(bar[1])
        self.assertEqual(health[0], "fail")
        self.assertIn("TimeoutError", health[1])

    def test_an_old_event_is_not_fetched(self):
        self._event("old", "2026-10-01T13:30:00Z")

        def history(start, end):
            raise AssertionError("yield fetch")

        shadow_news.consider_yields(
            datetime(2026, 11, 16, 14, 0, tzinfo=UTC),
            db_path=self.db,
            history=history,
        )

    def test_yahoo_failures_ignore_the_shadow_rows(self):
        shadow_news.ensure_schema(self.db)
        now = datetime(2026, 11, 18, 15, 0, tzinfo=CT)
        inside = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
        before = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """INSERT INTO source_health
                   (source, status, http_status, rows_new, fetched_at)
                   VALUES ('yahoo', 'fail', 429, 0, ?),
                          ('yahoo', 'ok', 200, 4, ?),
                          ('yahoo', 'fail', 429, 0, ?),
                          ('yahoo_shadow', 'fail', NULL, 0, ?)""",
                (
                    shadow_news.utc_iso(inside),
                    shadow_news.utc_iso(inside),
                    shadow_news.utc_iso(before),
                    shadow_news.utc_iso(inside),
                ),
            )
        text = "\n".join(shadow_news.yahoo_week_lines(self.db, now))
        self.assertIn("YAHOO (this Chicago week)", text)
        self.assertIn("live chain/quote failures: 1", text)
        self.assertIn("shadow ^TNX calls: 1 failures: 1", text)
        missing = "\n".join(
            shadow_news.yahoo_week_lines(os.path.join(self.tmp.name, "nope.db"), now)
        )
        self.assertIn("unavailable", missing)

    def test_health_write_does_not_create_a_database(self):
        missing = os.path.join(self.tmp.name, "absent.db")
        shadow_news.note_yahoo_health(
            kind="quote",
            symbol="SPY",
            status="fail",
            http_status=None,
            rows=0,
            latency_ms=3,
            detail="SPY quote crumb429=yes",
            db_path=missing,
        )
        self.assertFalse(os.path.exists(missing))

    def test_chain_fetch_logs_yahoo_and_a_write_error_still_returns(self):
        import config
        import fill_accounting as fa
        from data_engineer import fetch_options_data, reset_yahoo_ticker_cache_for_tests

        shadow_news.ensure_schema(self.db)
        fa.reset_session_for_tests()
        reset_yahoo_ticker_cache_for_tests()

        class _Chain:
            def __init__(self):
                frame = pd.DataFrame(
                    [
                        {
                            "strike": 500.0,
                            "bid": 1.0,
                            "ask": 1.1,
                            "lastPrice": 1.05,
                            "volume": 10.0,
                            "openInterest": 20,
                            "impliedVolatility": 0.2,
                        }
                    ]
                )
                self.calls = frame
                self.puts = frame.iloc[0:0]

        class _Stock:
            options = ["2026-11-20"]

            def option_chain(self, exp=None):
                return _Chain()

            def history(self, period="1d"):
                return pd.DataFrame({"Close": [500.0]})

        with mock.patch.object(config, "NEWS_DB_PATH", self.db):
            with mock.patch("data_engineer.yf.Ticker", return_value=_Stock()):
                raw = fetch_options_data("SPY")
        self.assertNotIn("error", json.loads(raw))
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT source, status, slot, detail FROM source_health"
            ).fetchone()
        self.assertEqual(row[0], "yahoo")
        self.assertEqual(row[1], "ok")
        self.assertEqual(row[2], "chain")
        self.assertIn("crumb429=no", row[3])
        self.assertIn("SPY chain", row[3])

        reset_yahoo_ticker_cache_for_tests()

        def _boom(**_kwargs):
            raise RuntimeError("locked")

        with mock.patch("shadow_news.note_yahoo_health", side_effect=_boom):
            with mock.patch("data_engineer.yf.Ticker", return_value=_Stock()):
                again = fetch_options_data("SPY")
        self.assertNotIn("error", json.loads(again))
        fa.reset_session_for_tests()
        reset_yahoo_ticker_cache_for_tests()


if __name__ == "__main__":
    unittest.main()
