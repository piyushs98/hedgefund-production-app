from keep_alive import keep_alive
keep_alive()

import os
import time
import yfinance as yf
from news_memory import save_headline, clear_expired_news

# Configuration variables
BYPASS_SCRAPER_WAIT = os.environ.get("BYPASS_SCRAPER_WAIT", "false").lower() == "true"

def extract_article_info(article):
    """
    Robust helper to extract the news headline title and publisher from yfinance.
    Handles both legacy (flat) and new (nested 'content' key) yfinance news structures.
    """
    if not isinstance(article, dict):
        return "Unknown Title", "Unknown Publisher"
        
    content = article.get("content", {})
    if content:
        title = content.get("title")
        provider_info = content.get("provider", {})
        publisher = provider_info.get("displayName") or provider_info.get("name") or content.get("publisher")
        if title:
            return title, publisher or "Unknown Publisher"
            
    # Fallback to legacy top-level keys
    title = article.get("title")
    publisher = article.get("publisher") or article.get("provider")
    if isinstance(publisher, dict):
        publisher = publisher.get("displayName") or publisher.get("name")
        
    return title or "Unknown Title", publisher or "Unknown Publisher"

def scrape_tech_sector():
    """
    Employee Tier - Tech Scraper:
    Scrapes live news for AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA and saves it.
    """
    tickers = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"]
    print("[Employee] Tech Scraper: Scanning tech sector news...")
    
    count = 0
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            articles = stock.news
            if articles:
                for article in articles:
                    title, publisher = extract_article_info(article)
                    if title and title != "Unknown Title":
                        saved = save_headline(ticker, "Tech", publisher, title)
                        if saved:
                            count += 1
        except Exception as e:
            print(f"❌ [Employee] Tech Scraper: Error scraping {ticker} ({e})")
            
    print(f"[Employee] Tech Scraper: Saved {count} new tech headlines.")

def scrape_macro_finance():
    """
    Employee Tier - Macro Scraper:
    Scrapes broad index ETFs (SPY, QQQ, IWM) for macroeconomic news.
    """
    tickers = ["SPY", "QQQ", "IWM"]
    print("[Employee] Macro Scraper: Scanning macroeconomic index news...")
    
    count = 0
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            articles = stock.news
            if articles:
                for article in articles:
                    title, publisher = extract_article_info(article)
                    if title and title != "Unknown Title":
                        saved = save_headline(ticker, "Macro", publisher, title)
                        if saved:
                            count += 1
        except Exception as e:
            print(f"❌ [Employee] Macro Scraper: Error scraping {ticker} ({e})")
            
    print(f"[Employee] Macro Scraper: Saved {count} new macro headlines.")

def scrape_politics_government():
    """
    Employee Tier - Politics/Gov Scraper:
    Scrapes major economic policy announcements and Federal Reserve news.
    Uses broad index feeds (^GSPC, ^TNX) and filters for policy/macro economic keywords.
    """
    print("[Employee] Politics/Gov Scraper: Scanning broad feeds for economic policy & Fed news...")
    source_tickers = ["^GSPC", "^TNX"]
    keywords = ["fed", "federal reserve", "powell", "yellen", "tariff", "policy", "rate", "inflation", "economic", "treasury", "government", "white house", "congress"]
    
    count = 0
    for source in source_tickers:
        try:
            stock = yf.Ticker(source)
            articles = stock.news
            if articles:
                for article in articles:
                    title, publisher = extract_article_info(article)
                    if title and title != "Unknown Title":
                        title_lower = title.lower()
                        # Verify if the headline matches macro policy keywords
                        if any(kw in title_lower for kw in keywords):
                            # Save under a unified "FED" symbol in the Politics sector
                            saved = save_headline("FED", "Politics", publisher, title)
                            if saved:
                                count += 1
        except Exception as e:
            print(f"❌ [Employee] Politics Scraper: Error scraping macro source {source} ({e})")
            
    print(f"[Employee] Politics Scraper: Saved {count} new economic policy headlines.")

def fetch_overnight_futures():
    """
    Employee Tier - Futures Scraper:
    Fetches pre-market percentage changes for S&P 500 E-mini futures (ES=F)
    and Nasdaq 100 E-mini futures (NQ=F) via yfinance.
    Saves percentage change and pricing details to the memory database.
    """
    print("[Employee] Futures Scraper: Scraping overnight global futures...")
    futures = {
        "ES=F": "S&P 500 E-mini Futures",
        "NQ=F": "Nasdaq 100 E-mini Futures"
    }
    
    count = 0
    for symbol, name in futures.items():
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            pct_change = info.get("regularMarketChangePercent")
            current_price = info.get("regularMarketPrice")
            
            # If regularMarketChangePercent is missing, try calculating it from close
            if pct_change is None:
                prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
                if current_price and prev_close:
                    pct_change = ((current_price - prev_close) / prev_close) * 100.0
                    
            # Fallback to history calculations
            if pct_change is None:
                hist = ticker.history(period="1d")
                if not hist.empty:
                    current_price = hist["Close"].iloc[-1]
                    prev_close = ticker.info.get("previousClose") or current_price
                    pct_change = ((current_price - prev_close) / prev_close) * 100.0
                    
            pct_change = float(pct_change) if pct_change is not None else 0.0
            current_price = float(current_price) if current_price is not None else 0.0
            
            # Format headline summary
            direction = "UP" if pct_change >= 0 else "DOWN"
            title = f"{name} is trending {direction} by {pct_change:+.2f}% (Price: ${current_price:,.2f})"
            publisher = "Yahoo Finance"
            
            # Save into the SQLite headlines table
            saved = save_headline(symbol, "Macro", publisher, title, sentiment_score=pct_change)
            if saved:
                count += 1
                print(f"[Employee] Futures Scraper: Saved {name} pre-market headline.")
        except Exception as e:
            print(f"❌ [Employee] Futures Scraper: Error scraping {symbol} ({e})")
            
    print(f"[Employee] Futures Scraper: Saved {count} futures data points.")


# ==========================================
# 🚀 SCRAPER DAEMON LOOP
# ==========================================
if __name__ == "__main__":
    print("\n--- INITIATING NEWS HARVESTING DAEMON LOOP ---")
    
    # Run once daily or scan interval configurations
    sleep_interval = 2 if BYPASS_SCRAPER_WAIT else 1200 # 20 minutes sleep standard
    
    while True:
        start_time = time.time()
        print(f"\n[System] Starting news harvest cycle at {time.strftime('%Y-%m-%d %H:%M:%S')}...")
        
        # Execute employee scrapers
        scrape_tech_sector()
        scrape_macro_finance()
        scrape_politics_government()
        fetch_overnight_futures()
        
        # Run expired cleanup (older than 90 days)
        clear_expired_news()
        
        duration = time.time() - start_time
        print(f"[System] News harvest completed in {duration:.2f} seconds.")
        
        # Exit if test bypass variable is true
        if BYPASS_SCRAPER_WAIT:
            print("[System] Test bypass activated. Exiting daemon cycle.")
            break
            
        print(f"Sleeping {sleep_interval // 60} minutes until next cycle...")
        time.sleep(sleep_interval)
