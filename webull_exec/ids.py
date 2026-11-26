"""client_order_id values. Max 32 characters, stable for one intent."""

from __future__ import annotations


def make_id(day: str, seq: int, kind: str) -> str:
    """``w`` + YYYYMMDD + counter + kind. Kind is one character."""
    text = f"w{day}{int(seq):04d}{kind[:1]}"
    if len(text) > 32:
        raise ValueError("client_order_id longer than 32")
    return text


def occ_symbol(ticker: str, expiration: str, option_type: str, strike: float) -> str:
    year, month, day = expiration[:10].split("-")
    cp = "C" if str(option_type).upper().startswith("C") else "P"
    strike_i = int(round(float(strike) * 1000))
    return f"{ticker.upper()}{year[2:]}{month}{day}{cp}{strike_i:08d}"
