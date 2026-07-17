import yfinance as yf
from datetime import datetime
from news_memory import save_innovation_data

def scrape_earnings_calendar(tickers):
    print("[Innovation Hub] 📅 Scraping Corporate Earnings Calendar...")
    for ticker in tickers:
        if ticker in ["SPY", "QQQ", "IWM"]:
            continue
        try:
            stock = yf.Ticker(ticker)
            cal = stock.calendar
            dates = []
            if isinstance(cal, dict):
                dates_val = cal.get("Earnings Date", [])
                if isinstance(dates_val, list):
                    dates = dates_val
                elif dates_val:
                    dates = [dates_val]
            elif cal is not None and hasattr(cal, 'empty') and not cal.empty:
                if "Earnings Date" in cal.index:
                    dates = cal.loc["Earnings Date"].values
                else:
                    dates = [cal.iloc[0, 0]] if len(cal) > 0 else []
            if len(dates) > 0 and dates[0] is not None:
                try:
                    dt = dates[0]
                    earnings_str = f"Corporate Earnings Scheduled for {dt}"
                    save_innovation_data(ticker, "EARNINGS", earnings_str)
                    print(f"  -> Saved EARNINGS calendar data for {ticker}.")
                except Exception as date_err:
                    pass
        except Exception as e:
            print(f"  -> Failed to fetch earnings calendar for {ticker}: {e}")

if __name__ == "__main__":
    test_tickers = ["NVDA", "SPY"]
    scrape_earnings_calendar(test_tickers)
