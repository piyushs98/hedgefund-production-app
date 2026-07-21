import time
import yfinance as yf
import pandas as pd
from datetime import datetime
from yf_client import SESSION, TICKER_PACING_SECONDS

# Define your 10 tickers here
TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX", "QCOM"]

print(f"Downloading today's 1-minute intraday data for: {TICKERS}")

all_data = []
for i, ticker in enumerate(TICKERS):
    print(f"Fetching {ticker}...")
    df = yf.download(
        tickers=ticker,
        period="1d",
        interval="1m",
        progress=False,
        session=SESSION,
    )
    
    if not df.empty:
        # yfinance recently updated to return MultiIndex columns. This flattens them back to normal.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        # Ensure the index is named 'Datetime' before resetting
        df.index.name = 'Datetime'
        
        # Move index to a standard column
        df = df.reset_index()
        df['Ticker'] = ticker
        
        all_data.append(df)

    if i < len(TICKERS) - 1:
        time.sleep(TICKER_PACING_SECONDS)

if all_data:
    combined_df = pd.concat(all_data, ignore_index=True)
    # Ensure standard string format for timestamps to make parsing easy
    combined_df['Datetime'] = combined_df['Datetime'].dt.strftime('%Y-%m-%d %H:%M:%S')
    combined_df.to_csv("simulation_market_data.csv", index=False)
    print(f"\nSuccess! Saved {len(combined_df)} rows of 1-minute data to simulation_market_data.csv")
else:
    print("Error: No data retrieved. Ensure the market was open today.")
