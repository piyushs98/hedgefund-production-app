def scrape_china_macro(tickers):
    """
    DISABLED. This module used to synthesize Shenzhen/tariff/semiconductor
    headlines with random.choice and write them to innovation_data as if
    they were observations. That nudged live S via SUPPLY_CHAIN_BOTTLENECK
    / EXPANSIONARY_TAILWIND.

    Writes nothing. Never synthesize a headline.
    """
    _ = tickers
    print(
        "[Innovation Hub] 🇨🇳 China macro scraper DISABLED — "
        "it synthesized headlines (Shenzhen/tariff/semiconductor). "
        "No writes to innovation_data. Never synthesize a headline."
    )
    return

if __name__ == "__main__":
    test_tickers = ["AAPL", "MSFT"]
    scrape_china_macro(test_tickers)
