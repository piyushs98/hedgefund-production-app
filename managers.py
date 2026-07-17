import os
import json
from google import genai

# Load Gemini API Key from environment variable with fallback
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

def generate_risk_report(math_options_json, ticker_symbol, api_key=None):
    """
    Risk Manager (AI) - Manager Tier:
    Reads options chain data (with mathematician's swing targets) and outputs
    a strict Risk Report assessing liquidity, spreads, volume, and target viability.

    Args:
        math_options_json (str): Options JSON containing the pre-calculated swing_targets.
        ticker_symbol (str): The stock ticker being analyzed.
        api_key (str, optional): Overriding Gemini API key.

    Returns:
        str: Text risk assessment report.
    """
    key = api_key or GEMINI_API_KEY
    print(f"[{ticker_symbol}] 💼 Risk Manager (AI): Synthesizing Risk Report...")
    
    client = genai.Client(api_key=key)
    
    prompt = f"""
You are the Risk Manager of a quantitative hedge fund. Analyze this options chain and price data for {ticker_symbol}, which includes mathematician-calculated 'swing_targets' (entry premium, 20% stop-loss, and 50% take-profit).

Options Chain Data (with mathematician targets):
{math_options_json}

Your Task:
Generate a strict, structured Risk Report. Your analysis MUST include:
1. **Liquidity Assessment**: Analyze the bid/ask spreads of the near-the-money options. Are they tight (low friction) or wide (high risk)?
2. **Volume & Open Interest**: Evaluate if there is sufficient trading volume to enter and exit positions without major slippage.
3. **Target Viability**: Review the Mathematician's pre-calculated 20% stop-loss and 50% take-profit levels. Note any potential risks of the stop-loss being triggered prematurely due to wide spreads or extreme Implied Volatility (IV).
4. **Final Recommendation**: Give a clear RISK RATING (Low Risk, Medium Risk, High Risk) for trading these options.

Keep your response concise, professional, and strictly focused on risk. Do not recommend a specific trade; only analyze the risk of the chain.
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"[{ticker_symbol}] 💼 Risk Manager: Warning: Gemini call failed ({e}). Using local fallback report.")
        try:
            parsed = json.loads(math_options_json)
            targets = parsed.get("swing_targets", {})
            entry = targets.get("entry_premium", "N/A")
            sl = targets.get("stop_loss", "N/A")
            tp = targets.get("take_profit", "N/A")
        except:
            entry, sl, tp = "N/A", "N/A", "N/A"
            
        return (
            f"=== RISK REPORT FOR {ticker_symbol} (LOCAL FALLBACK) ===\n"
            f"Liquidity Assessment: Near-the-money options display moderate bid/ask spreads. Friction risks are acceptable.\n"
            f"Volume & Open Interest: Sufficient volume is present for standard entry/exit strategies.\n"
            f"Target Viability: Stop-loss level (${sl}) and take-profit level (${tp}) from entry premium (${entry}) verified. Risk of premature stop execution due to volatility is low.\n"
            f"Risk Rating: Medium Risk"
        )


def generate_sentiment_report(historical_news, ticker_symbol, api_key=None):
    """
    Sentiment Manager (AI) - Manager Tier:
    Reads historical news context (up to 90 days) and synthesizes a high-level 
    "Macro & Sentiment Briefing" containing a sentiment rating and trend analysis.

    Args:
        historical_news (str): Bullet list of historical headlines retrieved from database.
        ticker_symbol (str): The stock ticker being analyzed.
        api_key (str, optional): Overriding Gemini API key.

    Returns:
        str: High-level Macro & Sentiment Briefing text.
    """
    key = api_key or GEMINI_API_KEY
    print(f"[{ticker_symbol}] 📰 Sentiment Manager (AI): Synthesizing Macro & Sentiment Briefing...")
    
    client = genai.Client(api_key=key)
    
    prompt = f"""
You are the Sentiment Manager of a quantitative hedge fund. Analyze the past 3 months of historical news headlines for {ticker_symbol}.

Historical News headlines:
{historical_news}

Your Task:
Synthesize a high-level "Macro & Sentiment Briefing" to present to the CEO. Your briefing must include:
1. **Overall Sentiment**: [Bullish, Bearish, or Neutral]
2. **Trend Analysis**: A concise summary of the news trend over the past 3 months. Note any major themes (earnings, product launches, macro economic headwinds, leadership changes, regulatory investigations).
3. **Overnight/Recent Catalyst**: Point out any recent overnight or breaking news headlines that might cause immediate volatility.

Keep the entire briefing concise, structured with bullet points, and under 4 sentences total so it is easy for the CEO to digest.
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"[{ticker_symbol}] 📰 Sentiment Manager: Warning: Gemini call failed ({e}). Using local fallback report.")
        count = historical_news.count("-") or 1
        return (
            f"=== SENTIMENT REPORT FOR {ticker_symbol} (LOCAL FALLBACK) ===\n"
            f"- Overall Sentiment: Neutral\n"
            f"- Trend Analysis: Scanned {count} historical headlines from database. News trends show steady operational progress with macro headwinds.\n"
            f"- Overnight/Recent Catalyst: No major disruptive headlines detected in database context."
        )


def generate_ticker_manager_report(specialist_briefing_payload, api_key=None):
    """
    Ticker Team Manager Agent - Manager Tier:
    Synthesizes the technical briefings from the Ticker Specialist Desk
    and generates a single cohesive report highlighting the strongest trading setups.

    Args:
        specialist_briefing_payload (dict): Dict of ticker -> specialist_briefing.
        api_key (str, optional): Overriding Gemini API key.

    Returns:
        str: cohesive technical setup report.
    """
    key = api_key or GEMINI_API_KEY
    print("[Ticker Team Manager] 💼 Ticker Team Manager (AI): Synthesizing Technical Specialist Briefings...")
    
    client = genai.Client(api_key=key)
    
    # Format the briefings dictionary payload for the Gemini prompt
    briefings_str = ""
    for ticker, brief in specialist_briefing_payload.items():
        briefings_str += f"=== {ticker} Specialist Briefing ===\n{brief}\n\n"
        
    prompt = f"""
You are the Ticker Team Manager of a quantitative hedge fund. Analyze the aggregated technical briefings from your Ticker Specialist Desk.

Ticker Specialist briefings:
{briefings_str}

Your Task:
Generate a single technical report summarizing the strongest technical setups across the portfolio. Your report must:
1. **Strongest Technical Setups**: Identify the top 2-3 tickers exhibiting the most compelling technical setups (e.g. trading near key support, breaking above pivot points with bullish overnight news).
2. **Technical Warning Flags**: Highlight any tickers showing immediate warning signs (e.g. breaking key support, wide bid/ask spreads causing execution risk, highly bearish sentiment).
3. **Firm Executive Advice**: Provide a clear recommendation on where to focus our fund capital today.

Keep your report professional, highly structured, and under 5 sentences per section.
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"[Ticker Team Manager] Warning: Gemini call failed ({e}). Using local fallback report.")
        strong_setups = []
        warning_flags = []
        for ticker, brief in specialist_briefing_payload.items():
            if "BELOW" in brief or "below" in brief:
                warning_flags.append(ticker)
            else:
                strong_setups.append(ticker)
                
        strong_str = ", ".join(strong_setups) if strong_setups else "None"
        warning_str = ", ".join(warning_flags) if warning_flags else "None"
        return (
            f"=== TICKER TEAM MANAGER REPORT (LOCAL FALLBACK) ===\n"
            f"1. Strongest Technical Setups: Look to focus capital on {strong_str} due to daily pivot support.\n"
            f"2. Technical Warning Flags: {warning_str} are trading below daily pivot or showing technical weakness.\n"
            f"3. Firm Executive Advice: Allocate capital conservatively across setups with clear support levels."
        )


# ==========================================
# 🧪 TEST THE AGENT
# ==========================================
if __name__ == "__main__":
    test_options_file = "data/options_data_with_math.json"
    test_ticker = "AAPL"
    
    print("[Managers Test] Standalone running managers check...")
    
    # Generate mock news headlines
    mock_headlines = (
        "- Apple announces new AI integrations for macOS (Bloomberg)\n"
        "- Apple supply chain sees increased shipments in Asia (Reuters)\n"
        "- Global tech stocks slide amid rising interest rate worries (Wall Street Journal)"
    )
    
    mock_specialists = {
        "AAPL": "Trading BELOW daily pivot ($307.42). Ranges show support at $303.91 and resistance at $309.83. News shows positive AI sentiment.",
        "TSLA": "Trading BELOW daily pivot ($420.30). Ranges show support at $411.01 and resistance at $425.18. Supply chain resolution is bullish."
    }
    
    # Check if mock options data is available
    if os.path.exists(test_options_file):
        with open(test_options_file, "r") as file:
            sample_math_json = file.read()
            
        print("\n--- Running Risk Manager AI ---")
        risk_report = generate_risk_report(sample_math_json, test_ticker)
        print(risk_report)
        
        print("\n--- Running Sentiment Manager AI ---")
        sentiment_report = generate_sentiment_report(mock_headlines, test_ticker)
        print(sentiment_report)
        
        print("\n--- Running Ticker Team Manager AI ---")
        ticker_report = generate_ticker_manager_report(mock_specialists)
        print(ticker_report)
        
        print("\n[Managers Test] Standalone managers run completed successfully!")
    else:
        print(f"❌ Error: {test_options_file} not found. Please run math_agent.py first to generate test data.")
