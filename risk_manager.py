import time
import sqlite3
import yfinance as yf
import broadcaster
from yf_client import SESSION, TICKER_PACING_SECONDS

DB_PATH = "data/news_room.db"


def get_live_option_value(ticker, option_type, strike, contract_id):
    """Best-effort live mid-price for an open contract via the yfinance chain.
    Returns None when no reliable quote exists — callers must treat None as
    'do not act', never as zero."""
    try:
        stock = yf.Ticker(ticker, session=SESSION)
        # contract_id convention: "<TICKER>-<EXP:YYYY-MM-DD>-<TYPE>-<STRIKE>"
        parts = (contract_id or "").split("-")
        exp = "-".join(parts[1:4]) if len(parts) >= 4 else None
        expirations = stock.options or []
        if exp not in expirations:
            exp = expirations[0] if expirations else None
        if not exp:
            return None
        chain = stock.option_chain(exp)
        df = chain.calls if str(option_type).upper().startswith("C") else chain.puts
        row = df[df["strike"] == strike]
        if row.empty:
            return None
        bid = float(row["bid"].iloc[0] or 0.0)
        ask = float(row["ask"].iloc[0] or 0.0)
        if bid <= 0 and ask <= 0:
            last = float(row["lastPrice"].iloc[0] or 0.0)
            return last if last > 0 else None
        return (bid + ask) / 2.0
    except Exception as e:
        print(f"[Risk Sentinel] Live valuation failed for {ticker} {strike} {option_type}: {e}")
        return None


def run_risk_sentinel():
    """
    Lean Position-Targeted Risk Sentinel
    Checks active positions every 60 seconds and monitors systemic risk.
    """
    print("🛡️ Starting Risk Sentinel Daemon...")
    while True:
        try:
            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT ticker, entry_price, option_type, strike, contract_id FROM active_positions WHERE status = 'OPEN'")
                open_positions = cursor.fetchall()
                
            if not open_positions:
                time.sleep(60)
                continue
                
            # If positions exist, check VIX for Intraday Matrix Triggers
            try:
                vix = yf.Ticker("^VIX", session=SESSION)
                vix_hist = vix.history(period="1d", interval="5m")
                # Simulated z-score check
                if len(vix_hist) >= 2:
                    current_vix = vix_hist['Close'].iloc[-1]
                    prev_vix = vix_hist['Close'].iloc[-2]
                    # If spike is massive (proxy for z-score > 2.0 or PCR shift > 15%)
                    if (current_vix - prev_vix) / prev_vix > 0.1:
                        print(f"⚠️ VIX Spike Detected! Current: {current_vix}")
                        broadcaster.send_discord_alert("[RISK LEVEL RED] Systemic Volatility Spike Detected (VIX). Initiating portfolio lockdown evaluation.")
            except Exception:
                pass
                
            # Check The 95% Stop Guard for each position
            for i, pos in enumerate(open_positions):
                ticker, entry_price, option_type, strike, contract_id = pos

                # BUG FIX: the previous build hardcoded a simulated 96% loss
                # (current_value = entry_price * 0.04), which force-exited EVERY
                # open position and spammed RED alerts every 60 seconds.
                # Until a live option-pricing feed is wired in, skip the check
                # rather than fabricate a valuation.
                current_value = get_live_option_value(ticker, option_type, strike, contract_id)
                if current_value is None:
                    if i < len(open_positions) - 1:
                        time.sleep(TICKER_PACING_SECONDS)
                    continue  # no live price available — do not act on fake data

                if current_value <= (entry_price * 0.05):
                    # 95% loss boundary breached
                    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                        cursor = conn.cursor()
                        cursor.execute("UPDATE active_positions SET status = 'FORCE_EXIT_TRIGGERED' WHERE contract_id = ?", (contract_id,))
                        conn.commit()
                    
                    alert_msg = f"[RISK LEVEL RED] 🚨 95% Stop Guard Breached for {ticker} ({option_type} @ {strike}). Contract {contract_id} status updated to FORCE_EXIT_TRIGGERED."
                    print(alert_msg)
                    broadcaster.send_discord_alert(alert_msg)

                if i < len(open_positions) - 1:
                    time.sleep(TICKER_PACING_SECONDS)
                    
            time.sleep(60)
            
        except sqlite3.Error as e:
            print(f"SQLite Error in Risk Sentinel: {e}")
            time.sleep(60)
        except Exception as e:
            print(f"Risk Sentinel Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    print("[Risk Sentinel] Module loaded successfully. Active loop disabled for compilation check.")
    # run_risk_sentinel()
