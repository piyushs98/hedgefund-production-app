"""Staged First Squawk text for the shadow log.

The score does not read this. master_bot stages the lines a scan would
have used. score_ticker takes them, logs the S they would have added,
and leaves them out of the number that clears 70.
"""

from __future__ import annotations

_staged: dict[str, str] = {}


def stage(ticker: str, text: str) -> None:
    """Replace this ticker's staged lines. An empty string is a real stage."""
    _staged[str(ticker or "").upper()] = text or ""


def take(ticker: str) -> str:
    """Return this ticker's staged lines and clear them."""
    return _staged.pop(str(ticker or "").upper(), "")
