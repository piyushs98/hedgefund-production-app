"""
quant_agent.py — Quant Analyst (Employee tier).

Rewritten: the previous version ran its file-read and Gemini call at
MODULE IMPORT TIME with a hardcoded API key — importing it anywhere would
fire a live API request (or crash if data/options_data.json was missing).
It is now a normal importable function with the key sourced from env.

Note: deterministic contract selection lives in strike_selector.py; this
agent supplies narrative color only.
"""

import os
import json

from google import genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


def analyze_options(options_json, ticker_symbol="AAPL", api_key=None):
    """Return a short LLM read on the most interesting contract in the chain."""
    key = api_key or GEMINI_API_KEY
    if not key:
        return "Quant Agent offline: GEMINI_API_KEY not set."
    client = genai.Client(api_key=key)
    prompt = f"""
You are an expert Quant Analyst reviewing live options chain data for {ticker_symbol}.
Look at the strike prices, implied volatility, and volume.
Identify ONE potential options trade (Call or Put) that looks interesting and
explain why in 3 sentences or less, citing the exact strike, IV, and volume.

Here is the data:
{options_json}
"""
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text.strip()
    except Exception as e:
        return f"Quant Agent LLM call failed ({e})."


if __name__ == "__main__":
    path = "data/options_data.json"
    if os.path.exists(path):
        with open(path, "r") as f:
            data = f.read()
        print(analyze_options(data))
    else:
        print(f"{path} not found — run data_engineer.py first.")
