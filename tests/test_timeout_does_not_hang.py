"""_call_with_timeout must not join a hung worker after the deadline."""

from __future__ import annotations

import time
import unittest

from master_bot import MasterBotScanError, _call_with_timeout


class TestCallTimeout(unittest.TestCase):
    def test_hung_worker_returns_within_timeout(self):
        def hang():
            time.sleep(30)

        t0 = time.monotonic()
        with self.assertRaises(MasterBotScanError) as ctx:
            _call_with_timeout(hang, timeout_s=1, step="test_hang")
        elapsed = time.monotonic() - t0
        self.assertTrue(ctx.exception.is_timeout)
        self.assertLess(elapsed, 3.0, f"timeout waited {elapsed:.1f}s for a hung worker")

    def test_fast_return_still_works(self):
        self.assertEqual(
            _call_with_timeout(lambda: 7, timeout_s=2, step="test_ok"),
            7,
        )


if __name__ == "__main__":
    unittest.main()
