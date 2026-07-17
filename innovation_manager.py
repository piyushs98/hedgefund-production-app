import os
from google import genai
from news_memory import get_innovation_context

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

def generate_macro_catalyst_vector(ticker, api_key=None):
    """
    Innovation Manager Agent:
    Acts as an elite macro-analyst. Extracts raw database rows from the 
    innovation scrapers (Gov, China, Earnings) and outputs a high-impact, 
    ticker-specific 'Macro Catalyst Vector'.
    """
    key = api_key or GEMINI_API_KEY
    print(f"[{ticker}] 🔬 Innovation Manager (AI): Synthesizing Macro Catalyst Vector...")
    
    innovation_data = get_innovation_context(ticker, days=7)
    
    if not innovation_data.strip():
        return "No specific macro or supply-chain catalysts identified for this ticker."
        
    client = genai.Client(api_key=key)
    
    prompt = f"""
You are an elite macro-analyst working for an aggressive quantitative hedge fund.
Review the following specialized data harvested over the last 7 days for the ticker {ticker}, focusing on Federal Policy, China Supply Chain/Geopolitics, and Corporate Earnings:

[INNOVATION HUB DATA]
{innovation_data}

Your task is to output a single, high-impact "Macro Catalyst Vector".
You MUST evaluate the data and explicitly include ONE of the following system trigger keywords if the data warrants it:
1. `EARNINGS_IMMINENT` (if earnings are reported to be within 48 hours or extremely soon)
2. `SUPPLY_CHAIN_BOTTLENECK` (if severe friction, tariff issues, or port delays are highlighted)
3. `EXPANSIONARY_TAILWIND` (if there are rate cuts, subsidies, or positive macro injections)

If none of those severe conditions apply, omit the keywords.

Output a 2-3 sentence summary detailing explicit tailwinds or headwinds for the portfolio based on the data provided.
"""
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"[{ticker}] 🔬 Innovation Manager: Warning: Gemini API failed ({e}). Using local fallback vector.")
        
        # Local fallback simulation
        if "supply-chain bottlenecks" in innovation_data.lower():
            return "SUPPLY_CHAIN_BOTTLENECK: Detected critical hardware component delays from Shenzhen."
        elif "rate cut" in innovation_data.lower() or "subsidize" in innovation_data.lower():
            return "EXPANSIONARY_TAILWIND: Federal Reserve signals accommodative policy."
        elif "earnings scheduled" in innovation_data.lower():
            return "EARNINGS_IMMINENT: Catalyst event pending shortly."
            
        return "Neutral macroeconomic backdrop. No critical tailwinds or bottlenecks detected."

if __name__ == "__main__":
    vector = generate_macro_catalyst_vector("AAPL")
    print(f"\n--- MACRO CATALYST VECTOR ---\n{vector}")
