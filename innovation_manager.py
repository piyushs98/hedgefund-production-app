import os

import llm_chain
from news_memory import get_innovation_context

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LLM_TIMEOUT_S = int(os.environ.get("LLM_CALL_TIMEOUT_S", "20"))


def generate_macro_catalyst_vector_local(ticker, innovation_data=None):
    """
    Local keyword macro vector (no Gemini). Used by midday delta path and as
    offline fallback so scoring tags stay available without API burn.
    """
    if innovation_data is None:
        innovation_data = get_innovation_context(ticker, days=7) or ""
    if not (innovation_data or "").strip():
        return "No specific macro or supply-chain catalysts identified for this ticker."
    kept = [
        ln for ln in (innovation_data or "").splitlines()
        if "[CHINA_MACRO]" not in ln.upper()
        and "[GOV_POLICY]" not in ln.upper()
    ]
    innovation_data = "\n".join(kept)
    if not innovation_data.strip():
        return "No specific macro or supply-chain catalysts identified for this ticker."
    low = innovation_data.lower()
    if "supply-chain bottlenecks" in low or "bottleneck" in low:
        return "SUPPLY_CHAIN_BOTTLENECK: Detected critical hardware component delays from Shenzhen."
    if "rate cut" in low or "subsidize" in low or "subsidy" in low:
        return "EXPANSIONARY_TAILWIND: Federal Reserve signals accommodative policy."
    try:
        import earnings_blackout
        if earnings_blackout.is_earnings_imminent(ticker):
            return "EARNINGS_IMMINENT: Print is inside the bounded earnings window."
    except Exception:
        pass
    return "Neutral macroeconomic backdrop. No critical tailwinds or bottlenecks detected."


def generate_macro_catalyst_vector(ticker, api_key=None, *, allow_llm=True):
    """
    Innovation Manager Agent:
    Acts as an elite macro-analyst. Extracts raw database rows from the
    innovation scrapers (Gov, China, Earnings) and outputs a high-impact,
    ticker-specific 'Macro Catalyst Vector'.

    LLM path: Gemini first only when allow_llm=True (pre-market / explicit full scan).
    Midday path must call with allow_llm=False or use generate_macro_catalyst_vector_local.
    """
    print(f"[{ticker}] 🔬 Innovation Manager: Synthesizing Macro Catalyst Vector...")

    innovation_data = get_innovation_context(ticker, days=7)

    if not innovation_data.strip():
        return "No specific macro or supply-chain catalysts identified for this ticker."

    if not allow_llm:
        return generate_macro_catalyst_vector_local(ticker, innovation_data)

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
        return llm_chain.generate_text(
            prompt,
            primary="deepseek",
            step=f"macro:{ticker}",
            timeout_s=LLM_TIMEOUT_S,
        )
    except Exception as e:
        print(
            f"[{ticker}] 🔬 Innovation Manager: Warning: LLM chain failed ({e}). "
            "Using local fallback vector."
        )
        return generate_macro_catalyst_vector_local(ticker, innovation_data)


if __name__ == "__main__":
    vector = generate_macro_catalyst_vector("AAPL")
    print(f"\n--- MACRO CATALYST VECTOR ---\n{vector}")
