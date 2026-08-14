"""Failed 30-min Discord posts must be loud — same class as mark failures."""

from __future__ import annotations

import unittest
from unittest import mock

import write_guard
from midday_delta import _note_scan_discord_result


class TestScanDiscordGuard(unittest.TestCase):
    def setUp(self):
        write_guard.record_write_ok("discord_scan")

    def tearDown(self):
        write_guard.record_write_ok("discord_scan")

    def test_success_resets_and_sends_nothing(self):
        with mock.patch("broadcaster.send_discord_alert") as send:
            _note_scan_discord_result(
                True, scan_id="s1", clock="09:32", n_admits=0
            )
            send.assert_not_called()
        self.assertEqual(write_guard.snapshot().get("discord_scan", 0), 0)

    def test_first_miss_without_admits_is_quiet(self):
        with mock.patch("broadcaster.send_discord_alert") as send:
            _note_scan_discord_result(
                False, scan_id="s1", clock="09:32", n_admits=0
            )
            send.assert_not_called()
        self.assertEqual(write_guard.snapshot().get("discord_scan"), 1)

    def test_second_consecutive_miss_is_critical(self):
        with mock.patch("broadcaster.send_discord_alert", return_value=True) as send:
            _note_scan_discord_result(
                False, scan_id="s1", clock="09:32", n_admits=0
            )
            _note_scan_discord_result(
                False, scan_id="s2", clock="10:03", n_admits=0,
                gate_summary="GATE [0/10 open] ADMIT none",
            )
            self.assertEqual(send.call_count, 1)
            text = send.call_args[0][0]
            self.assertIn("CRITICAL: SCAN DISCORD FAILED", text)
            self.assertIn("10:03", text)

    def test_admit_on_first_miss_is_critical(self):
        with mock.patch("broadcaster.send_discord_alert", return_value=True) as send:
            _note_scan_discord_result(
                False, scan_id="s1", clock="09:32", n_admits=2,
                gate_summary="GATE [2/10 open] ADMIT SPY:C@87",
            )
            self.assertEqual(send.call_count, 1)
            text = send.call_args[0][0]
            self.assertIn("CRITICAL: SCAN DISCORD FAILED", text)
            self.assertIn("2 position(s) admitted", text)


if __name__ == "__main__":
    unittest.main()
