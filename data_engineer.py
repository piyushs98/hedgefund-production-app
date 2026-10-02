import os
import json
import threading
from datetime import datetime

import yfinance as yf
from yf_client import SESSION

try:
    import config as _cfg
except Exception:  # pragma: no cover
    _cfg = None

# One Ticker per symbol per Chicago date. yfinance stores the expiration
# list on the instance, so a new Ticker re-downloads it. Cookie and crumb
# already live on the shared YfData singleton; this cache is only the list.
_TICKER_CACHE: dict[str, tuple[str, object]] = {}
_ticker_lock = threading.Lock()

_CHAIN_COLUMNS = (
    "strike",
    "lastPrice",
    "bid",
    "ask",
    "volume",
    "openInterest",
    "impliedVolatility",
    "delta",
)


def _chicago_day() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def reset_yahoo_ticker_cache_for_tests() -> None:
    with _ticker_lock:
        _TICKER_CACHE.clear()


def _yahoo_ticker(symbol: str):
    key = str(symbol or "").upper().strip()
    day = _chicago_day()
    with _ticker_lock:
        hit = _TICKER_CACHE.get(key)
        if hit is not None and hit[0] == day:
            return hit[1]
        stale = [k for k, (cached_day, _) in _TICKER_CACHE.items() if cached_day != day]
        for k in stale:
            _TICKER_CACHE.pop(k, None)
        inst = yf.Ticker(key, session=SESSION)
        _TICKER_CACHE[key] = (day, inst)
        return inst


def _max_calendar_dte() -> int:
    if _cfg is not None:
        return int(getattr(_cfg, "MAX_EXPIRY_CALENDAR_DTE", 10))
    return int(os.environ.get("MAX_EXPIRY_CALENDAR_DTE", "10") or 10)


def _min_calendar_dte() -> int:
    if _cfg is not None:
        return int(getattr(_cfg, "MIN_DTE", 1))
    return int(os.environ.get("MIN_DTE", "1") or 1)


def _calendar_dte(exp_str: str, asof: datetime | None = None) -> int:
    asof = asof or datetime.now()
    exp = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d")
    return (exp.date() - asof.date()).days


def _clean_option_frame(df) -> list:
    if df is None or getattr(df, "empty", True):
        return []
    present = [c for c in _CHAIN_COLUMNS if c in df.columns]
    if not present:
        return []
    return df[present].fillna(0).to_dict(orient="records")


def _record_chain(chain) -> dict:
    return {
        "calls": _clean_option_frame(getattr(chain, "calls", None)),
        "puts": _clean_option_frame(getattr(chain, "puts", None)),
    }


def _note_chain_ok() -> None:
    try:
        import fill_accounting
        fill_accounting.note_chain_ok()
    except Exception:
        pass


def fetch_options_data(ticker_symbol):
    """
    Fetch option chains for strike selection.

    Loads Yahoo expiries with calendar DTE in
    [MIN_DTE, MAX_EXPIRY_CALENDAR_DTE] (defaults 1..10). The no-date
    option_chain call is the expiration list and the nearest chain in one
    response. That nearest chain is kept, and not downloaded again, when
    its calendar DTE is inside 0..MAX — including DTE below MIN_DTE. At
    MIN_DTE=1 the kept chain is today's expiry, which the old window
    loaded anyway. The selector still rejects calendar DTE below MIN_DTE.
    Expiries past MAX stay out of this payload. The selector ranks every
    loaded expiry, so a farther chain would be a new entry. The 5-minute
    exit pass already quotes the one held expiry on its own.
    """
    print(f"Fetching options data for {ticker_symbol}...")
    stock = _yahoo_ticker(ticker_symbol)

    # One HTTP: expiration list plus the nearest chain. .options is then free.
    nearest_chain = stock.option_chain()
    expirations = list(stock.options or [])
    if not expirations:
        return json.dumps({"error": f"No options data found for {ticker_symbol}."})

    try:
        current_price = round(stock.history(period="1d")["Close"].iloc[-1], 2)
    except IndexError:
        current_price = "N/A"

    min_dte = _min_calendar_dte()
    max_dte = _max_calendar_dte()
    now = datetime.now()
    dte_by_exp: dict[str, int] = {}
    for exp_date in expirations:
        try:
            dte_by_exp[exp_date] = _calendar_dte(exp_date, now)
        except ValueError:
            continue

    target_expirations = [
        exp for exp, dte in ((e, dte_by_exp.get(e)) for e in expirations)
        if dte is not None and min_dte <= dte <= max_dte
    ]

    # Safety: if filter emptied (holiday / calendar glitch), fall back to first two
    if not target_expirations:
        target_expirations = expirations[:2]
        print(
            f"[{ticker_symbol}] WARNING: no expiries in DTE {min_dte}..{max_dte}; "
            f"falling back to nearest two {target_expirations}"
        )

    nearest_date = min(expirations)
    prefetched = {}
    if nearest_chain is not None and nearest_date in dte_by_exp:
        prefetched[nearest_date] = nearest_chain

    options_dict = {
        "ticker": ticker_symbol,
        "current_price": current_price,
        "chains": {},
        "expiries_loaded": list(target_expirations),
    }

    def _chain_for(exp_date):
        if exp_date in prefetched:
            return prefetched[exp_date]
        return stock.option_chain(exp_date)

    for exp_date in target_expirations:
        options_dict["chains"][exp_date] = _record_chain(_chain_for(exp_date))

    # List call already paid for the nearest chain. Keep it in the scored
    # set when it sits in the old 0..MAX window (today, when MIN_DTE is 1)
    # so liquidity sees the same expiries as before. Do not request it again.
    nearest_dte = dte_by_exp.get(nearest_date, -1)
    if (
        nearest_date not in options_dict["chains"]
        and nearest_date in prefetched
        and 0 <= nearest_dte <= max_dte
    ):
        options_dict["chains"][nearest_date] = _record_chain(prefetched[nearest_date])
        if nearest_date not in options_dict["expiries_loaded"]:
            options_dict["expiries_loaded"].append(nearest_date)

    _note_chain_ok()
    return json.dumps(options_dict, indent=2)


def fetch_spot(ticker_symbol: str) -> float | None:
    """
    Underlying last only — history / fast_info, never option_chain.

    Yahoo's chart endpoint often keeps working while v7/finance/options
    is throttled. Used for UNDERLYING_STOP when the chain is dark.
    """
    ticker_symbol = str(ticker_symbol or "").upper().strip()
    if not ticker_symbol:
        return None
    stock = _yahoo_ticker(ticker_symbol)
    try:
        hist = stock.history(period="1d")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            px = float(hist["Close"].iloc[-1])
            if px == px and px > 0:
                return round(px, 2)
    except Exception:
        pass
    try:
        fi = getattr(stock, "fast_info", None)
        if fi is not None:
            raw = fi.get("lastPrice") if hasattr(fi, "get") else fi["lastPrice"]
            px = float(raw)
            if px == px and px > 0:
                return round(px, 2)
    except Exception:
        pass
    return None


def _spot_or_na(stock) -> float | str:
    try:
        current_price = round(stock.history(period="1d")["Close"].iloc[-1], 2)
        return current_price
    except Exception:
        try:
            fi = getattr(stock, "fast_info", None)
            return round(float(fi["lastPrice"]), 2) if fi else "N/A"
        except Exception:
            return "N/A"


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
    stock = _yahoo_ticker(ticker_symbol)
    current_price = _spot_or_na(stock)

    def _err(msg: str) -> str:
        body = {"error": msg, "ticker": ticker_symbol, "current_price": current_price}
        return json.dumps(body)

    # Resolve expiry string to a listed date (Yahoo sometimes uses exact YYYY-MM-DD)
    try:
        listed = list(stock.options or [])
    except Exception as e:
        return _err(f"options list failed: {e}")
    if exp not in listed:
        # nearest listed match by prefix / equality on date
        matches = [e for e in listed if str(e)[:10] == exp]
        if not matches:
            return _err(
                f"expiration {exp} not in Yahoo options list for {ticker_symbol}"
            )
        exp = matches[0]

    try:
        chain = stock.option_chain(exp)
    except Exception as e:
        return _err(f"option_chain({exp}) failed: {e}")

    df = chain.puts if side == "puts" else chain.calls
    if df is None or getattr(df, "empty", True):
        return _err(f"empty {side} chain for {ticker_symbol} {exp}")

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
        return _err("chain missing strike columns")

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
        return _err(f"strike {strike_f} not found on {ticker_symbol} {exp} {side}")

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
    _note_chain_ok()
    return json.dumps(options_dict)


if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    options_json_string = fetch_options_data("AAPL")
    file_path = "data/options_data.json"
    with open(file_path, "w") as file:
        file.write(options_json_string)
    print(f"\nSuccess! The file was created at {file_path}")
