"""Lots left unmarked through the 2026-10-05 and 2026-10-06 sessions.

The Mac was asleep, so those four outcomes are not part of the
measurement. Later trades of the same tickers are measured. This module
names the trade ids and does not import the trading loop.
"""

from __future__ import annotations

EXCLUDED_TRADE_IDS = frozenset({
    "3d68662e-d8a6-4db7-b7ee-d227371da7c7",
    "fd2313eb-8d02-4c8d-b626-f76875d180a4",
    "51a8b17a-e684-43b6-b555-a74823373c4d",
    "9d590ab8-05cf-481e-8989-d3b7fe4bb179",
})


def is_excluded(trade_id) -> bool:
    return str(trade_id or "") in EXCLUDED_TRADE_IDS
