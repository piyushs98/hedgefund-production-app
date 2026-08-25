"""Midday DeepSeek-primary routing, 60s timeout, dual-fail CRITICAL once/session."""

from __future__ import annotations

import unittest
from unittest import mock

import config
import llm_chain
import midday_delta
import pre_market_meeting


class TestFormatProviderErr(unittest.TestCase):
    def test_timeout(self):
        err = llm_chain.LLMChainError("Timed out after 20s", is_timeout=True)
        self.assertEqual(llm_chain.format_provider_err(err), "timeout 20s")

    def test_http_401(self):
        self.assertEqual(
            llm_chain.format_provider_err(RuntimeError("DeepSeek HTTP 401: nope")),
            "401 unauthorized",
        )

    def test_key_missing(self):
        self.assertEqual(
            llm_chain.format_provider_err(RuntimeError("gemini key missing")),
            "key missing",
        )

    def test_none(self):
        self.assertEqual(llm_chain.format_provider_err(None), "n/a")


class TestMiddayRouting(unittest.TestCase):
    def setUp(self):
        llm_chain.reset_dual_fail_alerts_for_tests()

    def tearDown(self):
        llm_chain.reset_dual_fail_alerts_for_tests()

    def test_primary_is_deepseek_timeout_default_60(self):
        captured = {}

        def _fake(prompt, **kwargs):
            captured.update(kwargs)
            return "**📊 MIDDAY MACRO & NEWS UPDATE (11:00 CDT)**\nok"

        with mock.patch.object(llm_chain, "generate_text", side_effect=_fake):
            text = midday_delta.run_midday_macro_meeting(
                morning_excerpt="brief",
                rows=[{"ticker": "SPY", "action_flag": "PASS", "total_score": 40}],
            )
        self.assertEqual(captured.get("primary"), "deepseek")
        self.assertEqual(captured.get("step"), "midday_macro_meeting")
        self.assertEqual(captured.get("timeout_s"), 60)
        self.assertIn("MIDDAY MACRO", text)

    def test_timeout_override_kwarg(self):
        captured = {}

        def _fake(prompt, **kwargs):
            captured.update(kwargs)
            return "ok"

        with mock.patch.object(llm_chain, "generate_text", side_effect=_fake):
            midday_delta.run_midday_macro_meeting(timeout_s=90)
        self.assertEqual(captured.get("timeout_s"), 90)

    def test_dual_fail_crits_once_and_drops_offline_wording(self):
        alerts = []
        exc = llm_chain.LLMChainError(
            "chain exhausted",
            step="midday_macro_meeting",
            gemini_error=llm_chain.LLMChainError("Timed out after 20s", is_timeout=True),
            deepseek_error=RuntimeError("DeepSeek HTTP 401: bad key"),
        )
        with mock.patch.object(llm_chain, "generate_text", side_effect=exc):
            with mock.patch("broadcaster.send_discord_alert", side_effect=lambda m: alerts.append(m) or True):
                a = midday_delta.run_midday_macro_meeting()
                b = midday_delta.run_midday_macro_meeting()
        self.assertNotIn("LLM offline", a)
        self.assertIn("Deterministic fallback", a)
        self.assertIn("dual llm fail", a.lower())
        self.assertEqual(len(alerts), 1)
        self.assertIn("CRITICAL: LLM DUAL FAIL (midday_macro_meeting)", alerts[0])
        self.assertIn("gemini=timeout 20s", alerts[0])
        self.assertIn("deepseek=401 unauthorized", alerts[0])
        self.assertIn("Deterministic fallback", b)


class TestPreMarketDualFail(unittest.TestCase):
    def setUp(self):
        llm_chain.reset_dual_fail_alerts_for_tests()

    def tearDown(self):
        llm_chain.reset_dual_fail_alerts_for_tests()

    def test_dual_fail_alerts_and_still_returns_overnight_brief(self):
        alerts = []
        exc = llm_chain.LLMChainError(
            "chain exhausted",
            step="pre_market_cos",
            gemini_error=RuntimeError("Gemini REST HTTP 429: resource"),
            deepseek_error=RuntimeError("DEEPSEEK_API_KEY not set"),
        )
        with mock.patch.object(
            pre_market_meeting, "get_overnight_data", return_value="- [t] hello (Reuters)"
        ):
            with mock.patch.object(llm_chain, "generate_text", side_effect=exc):
                with mock.patch(
                    "broadcaster.send_discord_alert",
                    side_effect=lambda m: alerts.append(m) or True,
                ):
                    with mock.patch(
                        "midday_delta.store_morning_briefing", return_value=None
                    ):
                        text = pre_market_meeting.generate_morning_briefing()
        self.assertTrue(any("CRITICAL: LLM DUAL FAIL (pre_market_cos)" in m for m in alerts))
        crit = next(m for m in alerts if "DUAL FAIL" in m)
        self.assertIn("gemini=429 quota", crit)
        self.assertIn("deepseek=key missing", crit)
        self.assertIn("MORNING HEDGE FUND BRIEFING", text)
        # overnight-data brief still posted (plus the CRITICAL)
        self.assertGreaterEqual(len(alerts), 2)

    def test_empty_success_also_alerts(self):
        alerts = []
        with mock.patch.object(
            pre_market_meeting, "get_overnight_data", return_value="- [t] hello (Reuters)"
        ):
            with mock.patch.object(llm_chain, "generate_text", return_value=""):
                with mock.patch(
                    "broadcaster.send_discord_alert",
                    side_effect=lambda m: alerts.append(m) or True,
                ):
                    with mock.patch(
                        "midday_delta.store_morning_briefing", return_value=None
                    ):
                        pre_market_meeting.generate_morning_briefing()
        self.assertTrue(any("DUAL FAIL (pre_market_cos)" in m for m in alerts))


class TestConfigMiddayTimeout(unittest.TestCase):
    def test_default_60(self):
        self.assertEqual(config.MIDDAY_LLM_TIMEOUT_S, 60)


if __name__ == "__main__":
    unittest.main()
