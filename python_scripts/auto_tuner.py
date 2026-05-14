import yfinance as yf
import pandas as pd
import json
import os
import concurrent.futures

TICKERS = ["SOUN", "PLTR", "LCID", "TSLA", "NVDA", "AMD"] # Sample tickers for tuning
DAYS = 7 # Tune based on the last 7 days of market sentiment

def evaluate_params(roc_thresh, vol_thresh):
    total_pnl = 0.0
    for ticker in TICKERS:
        try:
            df = yf.Ticker(ticker).history(period=f'{DAYS}d', interval='15m')
            if df.empty or len(df) < 25:
                continue
            
            df['EMA9'] = df['Close'].ewm(span=9, adjust=False).mean()
            df['EMA21'] = df['Close'].ewm(span=21, adjust=False).mean()
            df['Vol_SMA'] = df['Volume'].rolling(window=20).mean()
            df = df.dropna()
            
            pos = None
            
            for i in range(1, len(df)):
                prev_row = df.iloc[i-1]
                row = df.iloc[i]
                
                price = round(row['Close'], 2)
                vol = row['Volume']
                avg_vol = row['Vol_SMA']
                prev_price = round(prev_row['Close'], 2)
                roc = ((price - prev_price) / prev_price) * 100 if prev_price > 0 else 0
                
                if pos is None:
                    is_vol_spike = vol > 50000 and vol > (avg_vol * vol_thresh)
                    if is_vol_spike and abs(roc) > roc_thresh:
                        # Emulate entry
                        pos = {
                            "entry": price,
                            "highest": price,
                            "qty": 100 # arbitrary fixed size for relative PnL comparison
                        }
                else:
                    if price > pos["highest"]:
                        pos["highest"] = price
                    
                    unrealized = (pos["highest"] - pos["entry"]) * pos["qty"]
                    current = (price - pos["entry"]) * pos["qty"]
                    
                    if price >= pos["entry"] * 1.08: # 8% Hard Take Profit
                        pnl = (price - pos["entry"]) * pos["qty"]
                        total_pnl += pnl
                        pos = None
                    elif unrealized > 15 and current < unrealized * 0.66:
                        pnl = (price - pos["entry"]) * pos["qty"]
                        total_pnl += pnl
                        pos = None
                    elif price <= pos["highest"] * 0.955: # 4.5% trailing stop
                        pnl = (price - pos["entry"]) * pos["qty"]
                        total_pnl += pnl
                        pos = None
        except Exception as e:
            pass
    return (roc_thresh, vol_thresh, total_pnl)

def run_tuner():
    print("Starting ML Parameter Auto-Tuning (Night Shift)...")
    best_pnl = -9999
    best_params = {"roc": 0.8, "vol_multiplier": 1.5}
    
    # Grid Search Space
    roc_values = [0.5, 0.8, 1.0, 1.5, 2.0]
    vol_values = [1.2, 1.5, 2.0, 3.0, 4.0, 5.0]
    
    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for r in roc_values:
            for v in vol_values:
                tasks.append(executor.submit(evaluate_params, r, v))
                
        for future in concurrent.futures.as_completed(tasks):
            r, v, pnl = future.result()
            print(f"Tested ROC: {r}%, Vol: {v}x -> PnL: ${pnl:.2f}")
            if pnl > best_pnl:
                best_pnl = pnl
                best_params = {"roc": r, "vol_multiplier": v}
                
    print(f"\n[DONE] Tuning Complete. Optimal Params: ROC > {best_params['roc']}%, Vol > {best_params['vol_multiplier']}x")
    print(f"Projected Weekly PnL: ${best_pnl:.2f}")
    
    with open('tuned_params.json', 'w') as f:
        json.dump(best_params, f)

def evolve_tags():
    print("\n[ALGO] Starting AI Tag Evolution Cycle...")
    tags_path = os.path.join(os.path.dirname(__file__), "tags.json")
    try:
        with open(tags_path, 'r', encoding='utf-8') as f:
            tags_content = f.read()
            
        from google import genai
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(__file__), '../../QuickTradeBackend/.env')
        load_dotenv(dotenv_path=env_path)
        
        API_KEY = os.getenv("GEMINI_API_KEY")
        if not API_KEY:
            print("No GEMINI_API_KEY found, skipping tag evolution.")
            return
            
        client = genai.Client(api_key=API_KEY)
        
        prompt = f"""You are an elite quantitative algorithm architect.
Below is the current JSON configuration of "Case Study Tags" my bot uses to classify penny stock breakouts.
The market has been extremely choppy lately, and many of these setups are failing (fake-outs).
Your task is to REWRITE this JSON file. Make the definitions strictly require undeniable volume confirmation and higher criteria. If a tag is fundamentally flawed, delete it entirely.

CURRENT TAGS:
{tags_content}

Return ONLY the raw updated JSON. No markdown formatting, no backticks, no explanations. Just valid JSON."""

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        
        new_tags = response.text.replace('```json', '').replace('```', '').strip()
        # Verify it's valid JSON
        json.loads(new_tags)
        
        with open(tags_path, 'w', encoding='utf-8') as f:
            f.write(new_tags)
            
        print("[ALGO] Tags Successfully Evolved and hardened against fake-outs.")
    except Exception as e:
        print(f"Tag Evolution failed: {e}")

if __name__ == "__main__":
    run_tuner()
    evolve_tags()
