import yfinance as yf
from yf_client import SESSION

def fetch_news(ticker_symbol):
    print(f"[{ticker_symbol}] News Agent: Scanning the internet for headlines...")
    stock = yf.Ticker(ticker_symbol, session=SESSION)
    
    # yfinance returns a list of dictionaries containing recent articles
    news_data = stock.news
    
    if not news_data:
        return "No recent news found."
        
    headlines = []
    
    # Grab the top 3 most recent news articles
    for article in news_data[:3]:
        # Try nested 'content' first (new yfinance structure)
        content = article.get("content", {})
        if content:
            title = content.get("title")
            provider_info = content.get("provider", {})
            publisher = provider_info.get("displayName") or provider_info.get("name") or content.get("publisher")
        else:
            # Fallback to legacy top-level keys
            title = article.get("title")
            publisher = article.get("publisher") or article.get("provider")
            if isinstance(publisher, dict):
                publisher = publisher.get("displayName") or publisher.get("name")
                
        title = title or "Unknown Title"
        publisher = publisher or "Unknown Publisher"
        
        # Format it nicely as a bulleted list
        headlines.append(f"- {title} ({publisher})")
        
    # Join the list together into one readable block of text
    return "\n".join(headlines)

# ==========================================
# 🧪 TEST THE AGENT
# ==========================================
if __name__ == "__main__":
    # Let's test it on Tesla since they always have crazy news
    test_ticker = "TSLA"
    
    latest_news = fetch_news(test_ticker)
    
    print(f"\n--- TOP 3 LATEST HEADLINES FOR {test_ticker} ---")
    print(latest_news)
