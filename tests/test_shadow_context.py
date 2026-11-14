"""Shadow calendar rows and trade tags. No network and no live ledger."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import shadow_calendar
import shadow_context
import shadow_news


def _at(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class CalendarTests(unittest.TestCase):
    def test_confirmed_rows_are_timezone_aware_and_not_fomc_names(self):
        ids = [row["event_id"] for row in shadow_calendar.CONFIRMED]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("bls-ppi-2026-11-13", ids)
        self.assertNotIn("bls-injuries-2026-11-18", ids)
        self.assertNotIn("bls-laus-2026-11-20", ids)
        self.assertNotIn("bls-tfp-2026-11-20", ids)
        self.assertNotIn("dol-claims-2026-11-25", ids)
        expected = {
            "fed-warsh-2026-11-16": (12, 0, 2, 30),
            "census-retail-2026-11-17": (8, 30, 1, None),
            "bls-mxp-2026-11-17": (8, 30, 2, None),
            "fed-g17-2026-11-17": (9, 15, 2, None),
            "census-starts-2026-11-18": (8, 30, 2, None),
            "fed-minutes-2026-11-18": (14, 0, 1, None),
            "dol-claims-2026-11-19": (8, 30, 1, None),
            "bea-gdp-2026-11-25": (8, 30, 1, None),
            "bea-income-2026-11-25": (8, 30, 1, None),
            "census-durable-2026-11-25": (8, 30, 1, None),
            "census-homes-2026-11-25": (10, 0, 2, None),
            "fed-beige-2026-11-25": (14, 0, 2, None),
        }
        self.assertEqual(set(ids), set(expected))
        for row in shadow_calendar.CONFIRMED:
            self.assertNotIn("fomc", row["name"].lower())
            self.assertNotEqual((row["kind"] or "").lower(), "fomc")
            self.assertIn(row["tier"], (1, 2))
            self.assertTrue(row["tz_source"])
            release = shadow_news.first_squawk.parse_time(row["release_at"])
            self.assertIsNotNone(release)
            local = release.astimezone(shadow_calendar.EAST)
            hour, minute, tier, window = expected[row["event_id"]]
            self.assertEqual((local.hour, local.minute, row["tier"]), (hour, minute, tier))
            self.assertEqual(row["window_after_min"], window)
        claims = next(
            row for row in shadow_calendar.CONFIRMED
            if row["event_id"] == "dol-claims-2026-11-19"
        )
        self.assertEqual(claims["tz_source"], "DOL weekly schedule")
        homes = next(
            row for row in shadow_calendar.CONFIRMED
            if row["event_id"] == "census-homes-2026-11-25"
        )
        self.assertEqual(homes["keywords"], ["home sales", "new home"])
        self.assertEqual(
            next(
                row["tz_source"] for row in shadow_calendar.CONFIRMED
                if row["event_id"] == "bls-mxp-2026-11-17"
            ),
            "page: Eastern Time",
        )

    def test_seed_inserts_once_and_drops_untiered_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "news.db")
            shadow_news.ensure_schema(path)
            conn = shadow_news.connect(path)
            try:
                conn.execute(
                    """
                    INSERT INTO calendar_events
                        (event_id, name, release_at, keywords, kind, ticker)
                    VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        "bls-injuries-2026-11-18",
                        "Employer-Reported Workplace Injuries and Illnesses for 2025",
                        "2026-11-18T15:00:00Z",
                        '["injuries"]',
                        None,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            first = shadow_calendar.seed_confirmed(path)
            second = shadow_calendar.seed_confirmed(path)
            conn = shadow_news.connect(path)
            try:
                stored = [
                    row[0]
                    for row in conn.execute(
                        "SELECT event_id FROM calendar_events ORDER BY event_id"
                    )
                ]
            finally:
                conn.close()
        self.assertEqual(first, len(shadow_calendar.CONFIRMED) - 1)
        self.assertEqual(second, 0)
        self.assertEqual(stored, sorted(row["event_id"] for row in shadow_calendar.CONFIRMED))
        self.assertNotIn("bls-injuries-2026-11-18", stored)

    def test_macro_row_does_not_arm_a_presser_slot(self):
        row = next(
            item for item in shadow_calendar.CONFIRMED
            if item["event_id"] == "fed-minutes-2026-11-18"
        )
        release = shadow_news.first_squawk.parse_time(row["release_at"])
        names = [
            spec.name
            for spec in shadow_news.slots_for(
                release, row["kind"], row["name"], row["window_after_min"]
            )
        ]
        self.assertEqual(names, ["statement"])
        self.assertEqual(row["keywords"], ["minutes", "FOMC", "Fed"])

    def test_warsh_window_closes_before_the_call(self):
        row = next(
            item for item in shadow_calendar.CONFIRMED
            if item["event_id"] == "fed-warsh-2026-11-16"
        )
        release = shadow_news.first_squawk.parse_time(row["release_at"])
        spec = shadow_news.slots_for(
            release, row["kind"], row["name"], row["window_after_min"]
        )[0]
        self.assertEqual(spec.window_start, release - timedelta(minutes=1))
        self.assertEqual(spec.window_end, release + timedelta(minutes=30))
        self.assertGreater(spec.call_at, spec.window_end)
        self.assertEqual(spec.retry_at - spec.call_at, timedelta(minutes=5))
        self.assertEqual(row["keywords"], ["Warsh", "Fed"])
        self.assertEqual(row["tz_source"], "agency convention")


class TagTests(unittest.TestCase):
    def _ppi(self, actual, forecast, prior="0.2%"):
        release = _at(2026, 11, 13, 13, 30)
        events = [
            {
                "event_id": "bls-ppi-2026-11-13",
                "name": "Producer Price Index for October 2026",
                "release_at": release,
                "kind": "macro",
                "tier": 1,
            }
        ]
        items = [
            {
                "event_id": "bls-ppi-2026-11-13",
                "actual": actual,
                "forecast": forecast,
                "prior": prior,
                "published_at": "2026-11-13T13:31:00Z",
            }
        ]
        return events, items, release

    def test_hot_ppi_call_is_minus_eight_and_can_flip(self):
        events, items, release = self._ppi("0.5%", "0.2%")
        entry = release + timedelta(hours=2)
        tag = shadow_context.tag_trade(
            direction="CALL",
            score=72,
            entry_at=entry,
            events=events,
            items=items,
        )
        self.assertEqual(tag["label"], "HOT")
        self.assertEqual(tag["bias"], "bearish")
        self.assertEqual(tag["context_adj"], -8)
        self.assertTrue(tag["would_flip"])
        self.assertFalse(tag["would_flip_80"])
        self.assertIsNone(tag["core_mom"])
        self.assertEqual(tag["score"], 72)
        self.assertFalse(tag["would_block"])
        self.assertEqual(tag["actual"], "0.5%")
        self.assertEqual(tag["forecast"], "0.2%")

    def test_hot_ppi_put_is_plus_eight(self):
        events, items, release = self._ppi("0.5%", "0.2%")
        tag = shadow_context.tag_trade(
            direction="PUT",
            score=64,
            entry_at=release + timedelta(hours=1),
            events=events,
            items=items,
        )
        self.assertEqual(tag["context_adj"], 8)
        self.assertTrue(tag["would_flip"])

    def test_missing_forecast_is_not_in_line(self):
        events, items, release = self._ppi("0.3%", None)
        tag = shadow_context.tag_trade(
            direction="CALL",
            score=81.2,
            entry_at=release + timedelta(hours=1),
            events=events,
            items=items,
        )
        self.assertIsNone(tag["label"])
        self.assertIsNone(tag["bias"])
        self.assertIsNone(tag["context_adj"])
        self.assertIsNone(tag["would_flip"])

    def test_in_line_inflation_does_not_flip(self):
        events, items, release = self._ppi("0.2%", "0.2%")
        tag = shadow_context.tag_trade(
            direction="CALL",
            score=81.2,
            entry_at=release + timedelta(hours=1),
            events=events,
            items=items,
        )
        self.assertEqual(tag["label"], "IN LINE")
        self.assertIsNone(tag["bias"])
        self.assertEqual(tag["context_adj"], 0)
        self.assertFalse(tag["would_flip"])

    def test_gdp_hot_has_no_bias(self):
        release = _at(2026, 11, 25, 13, 30)
        events = [
            {
                "event_id": "bea-gdp-2026-11-25",
                "name": "GDP (Second Estimate) and Corporate Profits, 3rd Quarter 2026",
                "release_at": release,
                "kind": "macro",
            }
        ]
        items = [
            {
                "event_id": "bea-gdp-2026-11-25",
                "actual": "3.4%",
                "forecast": "2.2%",
                "prior": None,
                "published_at": "2026-11-25T13:31:00Z",
            }
        ]
        tag = shadow_context.tag_trade(
            direction="CALL",
            score=80,
            entry_at=release + timedelta(hours=2),
            events=events,
            items=items,
        )
        self.assertEqual(tag["label"], "HOT")
        self.assertIsNone(tag["bias"])
        self.assertIsNone(tag["context_adj"])
        self.assertIsNone(tag["would_flip_80"])

    def test_would_block_only_for_tier_1_inside_the_window(self):
        release = _at(2026, 11, 18, 19, 0)
        events = [
            {
                "event_id": "fed-minutes-2026-11-18",
                "name": "Minutes of the October 27-28 meeting",
                "release_at": release,
                "kind": "fed",
                "tier": 1,
            },
            {
                "event_id": "bls-mxp-2026-11-17",
                "name": "U.S. Import and Export Price Indexes for October 2026",
                "release_at": _at(2026, 11, 17, 13, 30),
                "kind": "macro",
                "tier": 2,
            },
        ]
        inside = shadow_context.would_block(release - timedelta(minutes=10), events)
        outside = shadow_context.would_block(release + timedelta(minutes=20), events)
        on_import = shadow_context.would_block(
            _at(2026, 11, 17, 13, 30) + timedelta(minutes=10), events
        )
        self.assertTrue(inside)
        self.assertFalse(outside)
        self.assertFalse(on_import)
        self.assertIsNone(shadow_context.would_block(None, events))

    def test_headline_mom_and_core_mom_are_not_summed(self):
        release = _at(2026, 11, 13, 13, 30)
        events = [
            {
                "event_id": "bls-ppi-2026-11-13",
                "name": "Producer Price Index for October 2026",
                "release_at": release,
                "kind": "macro",
                "tier": 1,
            }
        ]
        items = [
            {
                "event_id": "bls-ppi-2026-11-13",
                "actual": "6.5%",
                "forecast": "6.6%",
                "prior": "6.3%",
                "published_at": "2026-11-13T13:33:05Z",
                "raw_text": "US PPI (YOY) (OCT)ACTUAL: 6.5% VS 6.3% PREVIOUS; EST 6.6%",
            },
            {
                "event_id": "bls-ppi-2026-11-13",
                "actual": "0.3%",
                "forecast": "0.4%",
                "prior": "1.3%",
                "published_at": "2026-11-13T13:32:59Z",
                "raw_text": "US PPI (MOM) (OCT)ACTUAL: 0.3% VS 1.3% PREVIOUS; EST 0.4%",
            },
            {
                "event_id": "bls-ppi-2026-11-13",
                "actual": "0.3%",
                "forecast": "0.3%",
                "prior": "0.7%",
                "published_at": "2026-11-13T13:33:20Z",
                "component": "core_mom",
                "raw_text": "US CORE PPI (MOM) (OCT)ACTUAL: 0.3% VS 0.7% PREVIOUS; EST 0.3%",
            },
        ]
        tag = shadow_context.tag_trade(
            direction="CALL",
            score=76,
            entry_at=release + timedelta(hours=1),
            events=events,
            items=items,
        )
        self.assertEqual(tag["actual"], "0.3%")
        self.assertEqual(tag["forecast"], "0.4%")
        self.assertEqual(tag["prior"], "1.3%")
        self.assertEqual(tag["label"], "COLD")
        self.assertEqual(tag["bias"], "bullish")
        self.assertEqual(tag["context_adj"], 8)
        self.assertFalse(tag["would_flip"])
        self.assertTrue(tag["would_flip_80"])
        self.assertEqual(tag["score_plus_context"], 84)
        core = tag["core_mom"]
        self.assertEqual(core["label"], "IN LINE")
        self.assertEqual(core["context_adj"], 0)
        self.assertFalse(core["would_flip"])
        self.assertFalse(core["would_flip_80"])

    def test_entry_before_the_print_does_not_use_it(self):
        events, items, release = self._ppi("0.5%", "0.2%")
        tag = shadow_context.tag_trade(
            direction="CALL",
            score=80,
            entry_at=release - timedelta(minutes=5),
            events=events,
            items=items,
        )
        self.assertIsNone(tag["event_id"])
        self.assertIsNone(tag["label"])
        self.assertTrue(tag["would_block"])


class ReactionStoreTests(unittest.TestCase):
    def test_spy_close_is_stored_once(self):
        import pandas as pd

        release = _at(2026, 11, 13, 13, 30)
        start = release - timedelta(minutes=30)
        end = release + timedelta(minutes=15)
        moment = start + timedelta(minutes=10)
        frame = pd.DataFrame(
            {"Close": [500.5]},
            index=pd.DatetimeIndex([moment]),
        )
        calls = []

        def history(symbol, window_start, window_end):
            calls.append(symbol)
            self.assertEqual(window_start, start)
            self.assertEqual(window_end, end)
            if symbol == "SPY":
                return {
                    "frame": frame,
                    "error": None,
                    "latency_ms": 1,
                    "crumb429": False,
                }
            return {
                "frame": pd.DataFrame({"Close": []}),
                "error": None,
                "latency_ms": 1,
                "crumb429": False,
            }

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "news.db")
            shadow_news.ensure_schema(path)
            conn = shadow_news.connect(path)
            try:
                conn.execute(
                    """
                    INSERT INTO calendar_events
                        (event_id, name, release_at, keywords, kind, ticker)
                    VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        "bls-ppi-2026-11-13",
                        "Producer Price Index for October 2026",
                        "2026-11-13T13:30:00Z",
                        '["PPI"]',
                        "macro",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            shadow_news.consider_reactions(
                end + timedelta(minutes=1),
                db_path=path,
                history=history,
            )
            shadow_news.consider_reactions(
                end + timedelta(minutes=1),
                db_path=path,
                history=history,
            )
            conn = shadow_news.connect(path)
            try:
                rows = conn.execute(
                    "SELECT symbol, close FROM reaction_bars ORDER BY symbol"
                ).fetchall()
            finally:
                conn.close()
        self.assertEqual(calls, ["SPY", "QQQ"])
        self.assertEqual(
            [(row["symbol"], row["close"]) for row in rows],
            [("QQQ", None), ("SPY", 500.5)],
        )


if __name__ == "__main__":
    unittest.main()
