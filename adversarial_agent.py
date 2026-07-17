import os
import json
from google import genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

class DevilsAdvocate:
    """
    Hyper-detailed Adversarial Agent acting as a cold, cynical short-seller.
    Evaluates trades before execution to find hidden risks.
    """
    def __init__(self, api_key=None):
        self.api_key = api_key or GEMINI_API_KEY
        self.client = genai.Client(api_key=self.api_key)

    def evaluate_trade(self, payload):
        """
        Receives payload dictionary: 
        {'ticker': symbol, 'liquidity_score': L, 'tech_score': T, 'sentiment_score': S, 'cos_justification': text}
        """
        ticker = payload.get("ticker", "UNKNOWN")
        print(f"[{ticker}] 👹 Devil's Advocate (AI): Executing adversarial risk intercept...")
        
        prompt = f"""
You are the Devil's Advocate, an ultra-cynical, hyper-detailed short-seller risk manager.
Your sole purpose is to destroy the thesis of this proposed options trade and protect capital.

Evaluate the following trade payload:
{json.dumps(payload, indent=2)}

You must attempt to invalidate this trade by computing three hidden risk vectors:
1. IV Crush Exposure: Is implied volatility too high to support a call/put purchase?
2. Institutional Distribution Blockades: Are large blocks of capital liquidating near this strike?
3. Counter-Trend Divergence: Is the broad market cross-current moving directly against this trade?

You MUST output your final decision purely as a JSON dictionary matching this exact structure:
{{"veto_triggered": true/false, "risk_confidence": 0.0-1.0, "reason": "string"}}

Only output the raw JSON. Do not include markdown code blocks or any other text.
"""
        try:
            response = self.client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            raw_text = response.text.strip()
            
            # Remove potential markdown block formatting
            if raw_text.startswith("```json"):
                raw_text = raw_text.replace("```json", "", 1).strip()
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3].strip()
                
            result = json.loads(raw_text)
            return result
        except Exception as e:
            print(f"[{ticker}] 👹 Devil's Advocate: Warning: Gemini API failed ({e}). Defaulting to fallback Veto assessment.")
            # Local fallback
            if payload.get("liquidity_score", 0) < 20 or payload.get("sentiment_score", 0) == 0:
                return {"veto_triggered": True, "risk_confidence": 0.85, "reason": "Fallback: Weak liquidity or hostile sentiment flagged as high risk."}
            else:
                return {"veto_triggered": False, "risk_confidence": 0.20, "reason": "Fallback: Setup appears structurally stable."}
