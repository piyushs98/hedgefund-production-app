import time
import yfinance as yf
from news_memory import save_innovation_data
from yf_client import SESSION, TICKER_PACING_SECONDS, crumb_backoff_remaining

_INDEX_ETFS = ("SPY", "QQQ", "IWM")


def earnings_work_tickers(tickers):
    """Single names. Index ETFs have no corporate earnings print."""
    return [t for t in tickers if t not in _INDEX_ETFS]


def _dates_from_calendar(cal):
    """Return (status, dates).

    status is ``ok`` when a print date was parsed, ``failed`` when the
    payload is missing or empty (quoteSummary 401 / no crumb degrades to
    ``{}``), and ``no_date`` when Yahoo returned a calendar with no
    earnings date. ``no_date`` still must not erase a last-known date.
    """
    if cal is None:
        return "failed", []
    dates = []
    if isinstance(cal, dict):
        if len(cal) == 0:
            return "failed", []
        dates_val = cal.get("Earnings Date", None)
        if isinstance(dates_val, list):
            dates = dates_val
        elif dates_val:
            dates = [dates_val]
        else:
            return "no_date", []
    elif hasattr(cal, "empty") and cal.empty:
        return "failed", []
    elif cal is not None and hasattr(cal, "empty"):
        if "Earnings Date" in getattr(cal, "index", []):
            dates = list(cal.loc["Earnings Date"].values)
        else:
            return "no_date", []
    else:
        return "failed", []
    dates = [d for d in dates if d is not None]
    if not dates:
        return "no_date", []
    return "ok", dates


def scrape_earnings_calendar(tickers):
    """Write print dates. A failure leaves the last saved date alone.

    Returns ticker -> ``ok`` | ``failed`` | ``no_date``.
    """
    print("[Innovation Hub] 📅 Scraping Corporate Earnings Calendar...")
    work_tickers = earnings_work_tickers(tickers)
    outcomes = {}
    for i, ticker in enumerate(work_tickers):
        remaining = crumb_backoff_remaining()
        if remaining > 0:
            print(
                f"[Yahoo] skipping earnings calendar — crumb backoff "
                f"{remaining:.0f}s remaining"
            )
            for rest in work_tickers[i:]:
                outcomes[rest] = "failed"
            break
        try:
            stock = yf.Ticker(ticker, session=SESSION)
            status, dates = _dates_from_calendar(stock.calendar)
            if status != "ok":
                outcomes[ticker] = status
                print(
                    f"  -> Earnings calendar for {ticker}: {status}. "
                    "Leaving the last-known date in place."
                )
            else:
                dt = dates[0]
                iso = None
                try:
                    from earnings_blackout import parse_print_date
                    parsed = parse_print_date(dt)
                    if parsed is not None:
                        iso = parsed.isoformat()
                except Exception as parse_err:
                    print(
                        f"[Earnings] parse failed ticker={ticker} "
                        f"raw={dt!r}: {parse_err}"
                    )
                if iso is None:
                    outcomes[ticker] = "failed"
                    print(
                        f"[Earnings] parse failed ticker={ticker} "
                        f"raw={dt!r}: no YYYY-MM-DD — not writing"
                    )
                else:
                    earnings_str = f"Corporate Earnings Scheduled for {iso}"
                    save_innovation_data(ticker, "EARNINGS", earnings_str)
                    outcomes[ticker] = "ok"
                    print(f"  -> Saved EARNINGS calendar data for {ticker} ({iso}).")
        except Exception as e:
            outcomes[ticker] = "failed"
            print(f"  -> Failed to fetch earnings calendar for {ticker}: {e}")
        if i < len(work_tickers) - 1 and crumb_backoff_remaining() <= 0:
            time.sleep(TICKER_PACING_SECONDS)
    return outcomes

if __name__ == "__main__":
    test_tickers = ["NVDA", "SPY"]
    scrape_earnings_calendar(test_tickers)
