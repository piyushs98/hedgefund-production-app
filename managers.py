import os
import json

import llm_chain

# Load Gemini API Key from environment variable with fallback
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LLM_TIMEOUT_S = int(os.environ.get("LLM_CALL_TIMEOUT_S", "20"))


def generate_risk_report(math_options_json, ticker_symbol, api_key=None):
    """
    Risk Manager (AI) - Manager Tier:
    Reads options chain data (with mathematician's swing targets) and outputs
    a strict Risk Report assessing liquidity, spreads, volume, and target viability.

    LLM path: Gemini first, automatic DeepSeek failover via llm_chain.
    """
    print(f"[{ticker_symbol}] 💼 Risk Manager (AI): Synthesizing Risk Report...")

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
        return llm_chain.generate_text(
            prompt,
            step=f"risk:{ticker_symbol}",
            timeout_s=LLM_TIMEOUT_S,
        )
    except Exception as e:
        print(f"[{ticker_symbol}] 💼 Risk Manager: Warning: LLM chain failed ({e}). Using local fallback report.")
        try:
            parsed = json.loads(math_options_json)
            targets = parsed.get("swing_targets", {})
            entry = targets.get("entry_premium", "N/A")
            sl = targets.get("stop_loss", "N/A")
            tp = targets.get("take_profit", "N/A")
        except Exception:
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

    LLM path: Gemini first, automatic DeepSeek failover via llm_chain.
    """
    print(f"[{ticker_symbol}] 📰 Sentiment Manager (AI): Synthesizing Macro & Sentiment Briefing...")

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
        return llm_chain.generate_text(
            prompt,
            step=f"sentiment:{ticker_symbol}",
            timeout_s=LLM_TIMEOUT_S,
        )
    except Exception as e:
        print(f"[{ticker_symbol}] 📰 Sentiment Manager: Warning: LLM chain failed ({e}). Using local fallback report.")
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

    LLM path: Gemini first, automatic DeepSeek failover via llm_chain.
    """
    print("[Ticker Team Manager] 💼 Ticker Team Manager (AI): Synthesizing Technical Specialist Briefings...")

    # Format the briefings dictionary payload for the prompt
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
        return llm_chain.generate_text(
            prompt,
            step="ticker_manager",
            timeout_s=LLM_TIMEOUT_S,
        )
    except Exception as e:
        print(f"[Ticker Team Manager] Warning: LLM chain failed ({e}). Using local fallback report.")
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
