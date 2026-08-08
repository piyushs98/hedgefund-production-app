import os
import json
import time
from datetime import datetime

import yfinance as yf
from yf_client import SESSION

try:
    import config as _cfg
except Exception:  # pragma: no cover
    _cfg = None


def _max_calendar_dte() -> int:
    if _cfg is not None:
        return int(getattr(_cfg, "MAX_EXPIRY_CALENDAR_DTE", 10))
    return int(os.environ.get("MAX_EXPIRY_CALENDAR_DTE", "10") or 10)


def _calendar_dte(exp_str: str, asof: datetime | None = None) -> int:
    asof = asof or datetime.now()
    exp = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d")
    return (exp.date() - asof.date()).days


def fetch_options_data(ticker_symbol):
    """
    Fetch option chains for strike selection / marks.

    Stage 4 C-A: load every Yahoo expiry with calendar DTE in
    [0, MAX_EXPIRY_CALENDAR_DTE] (default 10), not merely the nearest two.
    MIN_DTE filtering happens in strike_selector so marks for open 0DTE
    positions can still resolve after MIN_DTE was env-lowered historically.
    """
    print(f"Fetching options data for {ticker_symbol}...")
    stock = yf.Ticker(ticker_symbol, session=SESSION)

    expirations = list(stock.options or [])
    if not expirations:
        return json.dumps({"error": f"No options data found for {ticker_symbol}."})

    try:
        current_price = round(stock.history(period="1d")["Close"].iloc[-1], 2)
    except IndexError:
        current_price = "N/A"

    max_dte = _max_calendar_dte()
    now = datetime.now()
    target_expirations = []
    for exp_date in expirations:
        try:
            dte = _calendar_dte(exp_date, now)
        except ValueError:
            continue
        if 0 <= dte <= max_dte:
            target_expirations.append(exp_date)

    # Safety: if filter emptied (holiday / calendar glitch), fall back to first two
    if not target_expirations:
        target_expirations = expirations[:2]
        print(
            f"[{ticker_symbol}] WARNING: no expiries in DTE 0..{max_dte}; "
            f"falling back to nearest two {target_expirations}"
        )

    options_dict = {
        "ticker": ticker_symbol,
        "current_price": current_price,
        "chains": {},
        "expiries_loaded": list(target_expirations),
    }

    for i, exp_date in enumerate(target_expirations):
        chain = stock.option_chain(exp_date)

        def clean_chain(df):
            columns_to_keep = [
                "strike",
                "lastPrice",
                "bid",
                "ask",
                "volume",
                "openInterest",
                "impliedVolatility",
                "delta",
            ]
            present = [c for c in columns_to_keep if c in df.columns]
            if not present:
                return []
            return df[present].fillna(0).to_dict(orient="records")

        options_dict["chains"][exp_date] = {
            "calls": clean_chain(chain.calls),
            "puts": clean_chain(chain.puts),
        }
        # Light pacing when pulling many expiries (free-tier Yahoo)
        if i < len(target_expirations) - 1:
            time.sleep(0.15)

    return json.dumps(options_dict, indent=2)


def fetch_contract_quote(
    ticker_symbol: str,
    expiration: str,
    strike: float | int,
    direction: str = "CALL",
) -> str:
    """
    Lightweight mark path for the off-cycle exit pass.

    Pulls only the known expiry's option_chain and keeps the single strike
    row (plus spot). Does NOT fan out to every DTE ≤ 10.

    Returns the same options_dict JSON shape as fetch_options_data so
    position_exits.lookup_option_mark works unchanged.
    """
    ticker_symbol = str(ticker_symbol or "").upper().strip()
    exp = str(expiration or "")[:10]
    side = "puts" if "PUT" in str(direction or "").upper() else "calls"
    try:
        strike_f = float(strike)
    except (TypeError, ValueError):
        return json.dumps({"error": f"invalid strike {strike!r}"})

    if not ticker_symbol or not exp:
        return json.dumps({"error": "ticker and expiration required for contract quote"})

    print(
        f"[quote] {ticker_symbol} {side[:-1]} {strike_f:g} exp={exp} (single-expiry)"
    )
    stock = yf.Ticker(ticker_symbol, session=SESSION)

    try:
        current_price = round(stock.history(period="1d")["Close"].iloc[-1], 2)
    except Exception:
        try:
            fi = getattr(stock, "fast_info", None)
            current_price = round(float(fi["lastPrice"]), 2) if fi else "N/A"
        except Exception:
            current_price = "N/A"

    # Resolve expiry string to a listed date (Yahoo sometimes uses exact YYYY-MM-DD)
    listed = list(stock.options or [])
    if exp not in listed:
        # nearest listed match by prefix / equality on date
        matches = [e for e in listed if str(e)[:10] == exp]
        if not matches:
            return json.dumps(
                {
                    "error": (
                        f"expiration {exp} not in Yahoo options list for {ticker_symbol}"
                    )
                }
            )
        exp = matches[0]

    try:
        chain = stock.option_chain(exp)
    except Exception as e:
        return json.dumps({"error": f"option_chain({exp}) failed: {e}"})

    df = chain.puts if side == "puts" else chain.calls
    if df is None or getattr(df, "empty", True):
        return json.dumps({"error": f"empty {side} chain for {ticker_symbol} {exp}"})

    columns_to_keep = [
        "strike",
        "lastPrice",
        "bid",
        "ask",
        "volume",
        "openInterest",
        "impliedVolatility",
        "delta",
    ]
    present = [c for c in columns_to_keep if c in df.columns]
    if not present or "strike" not in present:
        return json.dumps({"error": "chain missing strike columns"})

    rows = df[present].fillna(0).to_dict(orient="records")
    best = None
    best_dist = 1e18
    for r in rows:
        try:
            cs = float(r.get("strike"))
        except (TypeError, ValueError):
            continue
        dist = abs(cs - strike_f)
        if dist < best_dist and dist <= 0.051:
            best_dist = dist
            best = r
    if best is None:
        return json.dumps(
            {
                "error": (
                    f"strike {strike_f} not found on {ticker_symbol} {exp} {side}"
                )
            }
        )

    options_dict = {
        "ticker": ticker_symbol,
        "current_price": current_price,
        "chains": {
            str(exp)[:10]: {
                "calls": [best] if side == "calls" else [],
                "puts": [best] if side == "puts" else [],
            }
        },
        "expiries_loaded": [str(exp)[:10]],
        "quote_mode": "single_contract",
    }
    return json.dumps(options_dict)


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    options_json_string = fetch_options_data("AAPL")
    file_path = "data/options_data.json"
    with open(file_path, "w") as file:
        file.write(options_json_string)
    print(f"\nSuccess! The file was created at {file_path}")
