import os
import json
import yfinance as yf

def fetch_options_data(ticker_symbol):
    print(f"Fetching options data for {ticker_symbol}...")
    stock = yf.Ticker(ticker_symbol)
    
    # 1. Get available expiration dates
    expirations = stock.options
    if not expirations:
        return json.dumps({"error": f"No options data found for {ticker_symbol}."})
    
    # 2. Get current stock price
    try:
        current_price = round(stock.history(period="1d")["Close"].iloc[-1], 2)
    except IndexError:
        current_price = "N/A"
    
    # Grab the first two nearest expiration dates to save space
    target_expirations = expirations[:2]
    
    options_dict = {
        "ticker": ticker_symbol,
        "current_price": current_price,
        "chains": {}
    }
    
    for exp_date in target_expirations:
        chain = stock.option_chain(exp_date)
        
        def clean_chain(df):
            # Keep only what the AI needs: Strike, Bid, Ask, Volume, Open Interest, Implied Volatility.
            # yfinance column sets vary by version — select only columns that exist
            # instead of raising KeyError and killing the whole ticker.
            columns_to_keep = ['strike', 'lastPrice', 'bid', 'ask', 'volume', 'openInterest', 'impliedVolatility']
            present = [c for c in columns_to_keep if c in df.columns]
            if not present:
                return []
            return df[present].fillna(0).to_dict(orient="records")
            
        options_dict["chains"][exp_date] = {
            "calls": clean_chain(chain.calls),
            "puts": clean_chain(chain.puts)
        }
        
    return json.dumps(options_dict, indent=2)

# --- This part runs automatically ---
if __name__ == "__main__":
    # Make sure the data folder gets created automatically
    os.makedirs("data", exist_ok=True)
    
    # Fetch data for Apple (AAPL)
    options_json_string = fetch_options_data("AAPL")
    
    # Define where to save it
    file_path = "data/options_data.json"
    
    # Save the file
    with open(file_path, "w") as file:
        file.write(options_json_string)
        
    print(f"\nSuccess! The file was created at: {file_path}")
    print("Go look inside your folder to see it!")
