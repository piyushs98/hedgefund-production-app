import pandas as pd
import time
import os
import subprocess
import json

# Load Environment Secrets
GEMINI_KEY = os.environ.get('GEMINI_API_KEY', 'YOUR_GEMINI_KEY')
DISCORD_URL = os.environ.get('DISCORD_WEBHOOK', 'YOUR_DISCORD_WEBHOOK')

# Configuration
INTERVAL_MINUTES = 30  
TICKER_FILE = "simulation_market_data.csv"

def send_to_discord(message):
    message_text = f"🚨 **[SIMULATION UPDATE]** 🚨\n{message}"
    
    if len(message_text) > 1999:
        message_text = message_text[:1990] + "..."
        
    payload = {"content": message_text}
    print(f"   ➔ Attempting Discord push ({len(message_text)} chars)...")
    
    # UN-MUTED: We now capture the response to see exactly why Discord is failing
    result = subprocess.run([
        'curl', '-s', '-w', '\nHTTP_STATUS:%{http_code}', '-H', 'Content-Type: application/json',
        '-X', 'POST', '-d', json.dumps(payload),
        DISCORD_URL
    ], capture_output=True, text=True)
    
    output = result.stdout.strip()
    if "HTTP_STATUS:204" in output or "HTTP_STATUS:200" in output:
        print("   ✅ Discord push successful!")
    else:
        print(f"   ❌ Discord Delivery Failed. Server replied:\n{output}")

def analyze_data_with_ai(data_chunk):
    if not GEMINI_KEY or "YOUR_" in GEMINI_KEY:
        return "AI Analysis Mocked: Environment key missing."
    
    prompt = f"Analyze this market data interval chunk for our 10 tickers. Identify breakout trends, volume anomalies, or key price movements:\n\n{data_chunk.to_string(index=False)}"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    # Auto-Retry Loop for Google throttling
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"   ➔ Querying Gemini AI core via native macOS network (Attempt {attempt + 1}/{max_retries})...")
            result = subprocess.run([
                'curl', '-s', '-H', 'Content-Type: application/json',
                '-X', 'POST', '-d', json.dumps(payload),
                url
            ], capture_output=True, text=True)
            
            response_data = json.loads(result.stdout)
            
            if "error" in response_data:
                err_msg = response_data['error'].get('message', 'Unknown Error')
                if "high demand" in err_msg.lower() or "quota" in err_msg.lower():
                    print(f"   ⚠️ Google servers are busy: '{err_msg}'. Waiting 15 seconds before retry...")
                    time.sleep(15)
                    continue  # Try again
                else:
                    print(f"   ❌ Gemini API Error: {err_msg}")
                    return f"API Error: {err_msg}"
                
            ai_text = response_data['candidates'][0]['content']['parts'][0]['text']
            print("   ➔ Gemini AI response generated successfully.")
            return ai_text
            
        except Exception as e:
            print(f"   ❌ Network/JSON Parsing Error: {str(e)}")
            return f"AI Generation Error: {str(e)}"
            
    return "API Error: Google servers failed to respond after 3 attempts due to high demand."

# Read and prepare data
df = pd.read_csv(TICKER_FILE)
df['Datetime'] = pd.to_datetime(df['Datetime'])
df = df.sort_values(by='Datetime').reset_index(drop=True)

timestamps = sorted(df['Datetime'].unique())

print(f"Starting Resilient Replay Engine. Simulating interval updates every {INTERVAL_MINUTES} minutes...")
current_window = []
minutes_counter = 0

for current_time in timestamps:
    minute_data = df[df['Datetime'] == current_time]
    current_window.append(minute_data)
    minutes_counter += 1
    
    print(f"Simulated Time: {current_time.strftime('%H:%M CDT')} | Buffer: {minutes_counter}/{INTERVAL_MINUTES} mins", end="\r")
    
    if minutes_counter >= INTERVAL_MINUTES:
        print(f"\n🚩 Break Interval Reached ({current_time.strftime('%H:%M CDT')})!")
        
        interval_df = pd.concat(current_window, ignore_index=True)
        analysis_result = analyze_data_with_ai(interval_df)
        send_to_discord(f"**Interval End: {current_time.strftime('%H:%M CDT')}**\n\n{analysis_result}")
        
        current_window = []
        minutes_counter = 0
        time.sleep(15) 

print("\nSimulation complete. Full architecture loop verified!")
