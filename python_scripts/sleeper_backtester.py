import json
import time
import pandas as pd
import yfinance as yf
from webull import webull
import concurrent.futures
import threading

webull_instance = webull()

SLEEPER_BT_STATUS = "IDLE"
SLEEPER_BT_VAULT = []
SLEEPER_BT_REPORT = {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "intel_tags": {}}

def run_1y_simulation():
    global SLEEPER_BT_STATUS, SLEEPER_BT_VAULT, SLEEPER_BT_REPORT
    SLEEPER_BT_STATUS = "SCANNING_600"
    
    try:
        resp = webull_instance.active_gainer_loser('gainer', rank_type='52w', count=600)
        tickers = [g.get('ticker', {}).get('symbol') for g in resp.get('data', []) if g.get('ticker', {}).get('symbol')]
        # Limit to 150 for backtest speed to avoid yfinance rate limits
        tickers = tickers[:150]
        
        SLEEPER_BT_STATUS = f"DOWNLOADING_HISTORY_FOR_{len(tickers)}"
        # Fetch 1 year daily history
        data = yf.download(tickers, period="1y", interval="1d", group_by="ticker", progress=False)
        
        # We need sector info for TAG intel
        SLEEPER_BT_STATUS = "GATHERING_TAG_INTEL"
        sector_map = {}
        def fetch_sector(t):
            try:
                info = yf.Ticker(t).info
                return t, info.get('sector', 'Unknown')
            except:
                return t, 'Unknown'
                
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as exe:
            results = exe.map(fetch_sector, tickers)
            for t, sec in results:
                sector_map[t] = sec

        SLEEPER_BT_STATUS = "SIMULATING_DAYS"
        vault = {}
        completed_trades = []
        
        # Iterate over each stock to simulate time
        for ticker in tickers:
            if ticker not in data: continue
            df = data[ticker]
            if df.empty or 'Close' not in df.columns: continue
            
            df = df.dropna()
            if len(df) < 50: continue
            
            high_water = df['High'].iloc[0]
            
            # Walk through days
            for i in range(10, len(df)):
                row = df.iloc[i]
                price = row['Close']
                
                # If we don't hold it, check if it's sleeping
                if ticker not in vault:
                    high_water = max(high_water, row['High'])
                    if price <= high_water * 0.5: # Dropped 50%, it's sleeping
                        vault[ticker] = {
                            "status": "WATCHING",
                            "baseline": price,
                            "entry": 0.0,
                            "high_mark": 0.0
                        }
                else:
                    pos = vault[ticker]
                    if pos['status'] == "WATCHING":
                        pos['baseline'] = min(pos['baseline'], price)
                        surge = (price - pos['baseline']) / pos['baseline']
                        if surge >= 0.02: # AWAKENED!
                            pos['status'] = "ACTIVE"
                            pos['entry'] = price
                            pos['high_mark'] = price
                    elif pos['status'] == "ACTIVE":
                        pnl = (price - pos['entry']) / pos['entry']
                        if pnl <= -0.05: # Loss
                            completed_trades.append({"ticker": ticker, "sector": sector_map.get(ticker, "Unknown"), "win": False})
                            del vault[ticker]
                            continue
                        pos['high_mark'] = max(pos['high_mark'], price)
                        stop = pos['high_mark'] * 0.98
                        if price <= stop:
                            completed_trades.append({"ticker": ticker, "sector": sector_map.get(ticker, "Unknown"), "win": True})
                            del vault[ticker]
                            continue
                            
        # Simulation Finished. Compile Intel.
        SLEEPER_BT_STATUS = "COMPILING_INTEL"
        wins = sum(1 for t in completed_trades if t['win'])
        losses = len(completed_trades) - wins
        
        tags = {}
        for t in completed_trades:
            sec = t['sector']
            if sec not in tags: tags[sec] = {"wins": 0, "total": 0, "winning_tickers": [], "losing_tickers": []}
            tags[sec]['total'] += 1
            if t['win']: 
                tags[sec]['wins'] += 1
                if len(tags[sec]['winning_tickers']) < 5: tags[sec]['winning_tickers'].append(t['ticker'])
            else:
                if len(tags[sec]['losing_tickers']) < 3: tags[sec]['losing_tickers'].append(t['ticker'])
            
        for sec in tags:
            tags[sec]['win_rate'] = round((tags[sec]['wins'] / tags[sec]['total']) * 100, 1) if tags[sec]['total'] > 0 else 0
            
        SLEEPER_BT_REPORT = {
            "total_trades": len(completed_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / len(completed_trades)) * 100, 1) if completed_trades else 0,
            "intel_tags": tags
        }
        
        # Save Intel for Gemini
        with open("backend/sleeper_intel.json", "w") as f:
            json.dump(SLEEPER_BT_REPORT, f)
            
        SLEEPER_BT_STATUS = "DONE"
        print("✅ [BACKTEST] 1-Year Sleeper Simulation Complete. Intel Generated.")
        
    except Exception as e:
        SLEEPER_BT_STATUS = f"ERROR: {e}"

if __name__ == "__main__":
    run_1y_simulation()
    print(json.dumps(SLEEPER_BT_REPORT, indent=2))
