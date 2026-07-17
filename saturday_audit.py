import sqlite3
import json
import os
from google import genai
import broadcaster

DB_PATH = "data/news_room.db"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

def run_saturday_audit():
    """
    Saturday Performance Audit Loop
    Reads the trade_logs table and synthesizes the metrics portfolio.
    Outputs a weight adjustment JSON for the upcoming week.
    """
    print("📊 Initiating Saturday Performance Audit Loop...")
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ticker, outcome, profit_loss, duration_hours, agent_prediction FROM trade_logs")
            logs = cursor.fetchall()
            
        # For testing purposes, mock a log if the database is empty
        if not logs:
            print("No trades logged this week to audit. Generating simulated audit data...")
            logs = [
                ("AAPL", "WIN", 450.50, 4.2, "BULLISH_BREAKOUT"),
                ("TSLA", "LOSS", -120.00, 2.1, "SUPPORT_BOUNCE"),
                ("SPY", "WIN", 210.00, 1.5, "MOMENTUM_CONTINUATION")
            ]
            
        # Synthesize Metrics Portfolio
        total_trades = len(logs)
        wins = sum(1 for log in logs if log[2] > 0)
        win_loss_ratio = wins / total_trades if total_trades > 0 else 0
        avg_duration = sum(log[3] for log in logs) / total_trades if total_trades > 0 else 0
        total_pnl = sum(log[2] for log in logs)
        
        metrics_payload = {
            "total_weekly_capital_deployed": total_trades * 1000,
            "gross_win_loss_ratio": win_loss_ratio,
            "average_trade_duration_hours": avg_duration,
            "total_pnl": total_pnl,
            "trade_sample_size": total_trades
        }
        
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
You are an advanced meta-optimization instance for a quantitative hedge fund.
Evaluate the following weekly portfolio metrics and determine if the market shifted regimes (e.g., from high-momentum breakout to sideways mean-reverting).

Metrics:
{json.dumps(metrics_payload, indent=2)}

Based on this performance, recommend exact payload updates for the 100-point system weights for the upcoming week.
You must output a strictly formatted JSON object mirroring this EXACT structure:
{{"recommended_weights": {{"liquidity": 30, "technical": 40, "sentiment": 30}}}}

Ensure the total sum equals exactly 100.
Do not output anything other than raw JSON.
"""
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        raw_text = response.text.strip()
        
        if raw_text.startswith("```json"):
            raw_text = raw_text.replace("```json", "", 1).strip()
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3].strip()
            
        try:
            weights = json.loads(raw_text)
        except json.JSONDecodeError:
            print("Failed to decode LLM JSON. Using fallback weights.")
            weights = {"recommended_weights": {"liquidity": 30, "technical": 40, "sentiment": 30}}

        # NEW: persist the recommendation so the live scoring engine actually
        # uses it next week (previously it was broadcast and discarded).
        try:
            import config
            config.save_weights(weights.get("recommended_weights", {}))
        except Exception as w_err:
            print(f"Could not persist recommended weights ({w_err}); engine keeps prior weights.")
        
        report = f"""# 📊 Saturday Performance Audit
**Total PnL:** ${total_pnl:.2f}
**Win/Loss Ratio:** {win_loss_ratio*100:.1f}%
**Average Duration:** {avg_duration:.1f} hours

**Weight Adjustment Output:**
```json
{json.dumps(weights, indent=2)}
```
"""
        print(report)
        broadcaster.send_discord_alert(report)
        
    except sqlite3.Error as e:
        print(f"SQLite Error in Saturday Audit: {e}")
    except Exception as e:
        print(f"Saturday Audit Error: {e}")

if __name__ == "__main__":
    print("[Saturday Audit] Module loaded successfully.")
    # Uncomment to test execution
    # run_saturday_audit()
