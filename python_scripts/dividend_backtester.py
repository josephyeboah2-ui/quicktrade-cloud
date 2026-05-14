import json
import time
import pandas as pd
import yfinance as yf

DIVIDEND_BT_REPORT = {"total_invested": 0, "total_dividends_collected": 0, "intel_tags": {}}

def run_5y_simulation():
    print("[DIVIDEND BT] Simulating S&P 500 Dividend Giants...")
    try:
        # Hardcode 20 reliable S&P 500 dividend payers
        tickers = ["AAPL", "MSFT", "JNJ", "PG", "KO", "PEP", "T", "VZ", "XOM", "CVX", 
                   "MCD", "WMT", "JPM", "BAC", "PFE", "MRK", "CSCO", "INTC", "MMM", "IBM"]
        sector_map = {
            "AAPL": "Technology", "MSFT": "Technology", "CSCO": "Technology", "INTC": "Technology", "IBM": "Technology",
            "JNJ": "Healthcare", "PFE": "Healthcare", "MRK": "Healthcare",
            "PG": "Consumer Defensive", "KO": "Consumer Defensive", "PEP": "Consumer Defensive", "WMT": "Consumer Defensive",
            "T": "Communication", "VZ": "Communication",
            "XOM": "Energy", "CVX": "Energy",
            "MCD": "Consumer Cyclical",
            "JPM": "Financials", "BAC": "Financials",
            "MMM": "Industrials"
        }
        
        print(f"[DIVIDEND BT] Downloading 5-Year History for {len(tickers)} stocks...")
        data = yf.download(tickers, period="5y", interval="1d", group_by="ticker", progress=False)
        
        print("[DIVIDEND BT] Simulating Value Accumulation Strategy...")
        total_invested = 0
        total_dividends = 0
        tags = {}
        
        for ticker in tickers:
            if ticker not in data: continue
            df = data[ticker]
            if df.empty or 'Close' not in df.columns: continue
            
            try:
                t_obj = yf.Ticker(ticker)
                divs = t_obj.dividends
                if divs is not None and not divs.empty:
                    divs.index = divs.index.tz_localize(None)
            except: divs = pd.Series()
            
            df = df.dropna()
            if len(df) < 250: continue
            
            df['SMA_200'] = df['Close'].rolling(window=200).mean()
            df = df.dropna()
            
            position = 0
            ticker_divs_collected = 0
            
            for i in range(len(df)):
                date = df.index[i]
                price = df['Close'].iloc[i]
                sma = df['SMA_200'].iloc[i]
                
                if not divs.empty:
                    match_div = divs[divs.index.date == date.date()]
                    if not match_div.empty:
                        payout = match_div.iloc[0] * position
                        if payout > 0:
                            total_dividends += payout
                            ticker_divs_collected += payout
                            position += payout / price
                            total_invested += payout
                        
                if position == 0 and price < sma:
                    position = 1000 / price
                    total_invested += 1000
                    
            if position > 0:
                sector = sector_map.get(ticker, "Unknown")
                if sector not in tags: tags[sector] = {"collected": 0, "count": 0, "tickers": []}
                tags[sector]['collected'] += ticker_divs_collected
                tags[sector]['count'] += 1
                tags[sector]['tickers'].append(ticker)
                
        # Fetch real trailing yields
        for sector, data in tags.items():
            total_yield = 0
            valid_count = 0
            for t in data["tickers"]:
                try:
                    y = yf.Ticker(t).info.get("dividendYield", 0)
                    if y > 0:
                        if y < 1: y = y * 100 # Convert decimal to percentage if needed
                        total_yield += y
                        valid_count += 1
                except:
                    pass
            data["avg_yield"] = round(total_yield / valid_count, 2) if valid_count > 0 else 0.0

        DIVIDEND_BT_REPORT = {
            "total_invested": round(total_invested, 2),
            "total_dividends_collected": round(total_dividends, 2),
            "intel_tags": {k: {"avg_yield": v["avg_yield"], "tickers": v['tickers']} for k, v in tags.items() if v['count'] > 0}
        }
        
        with open("backend/dividend_intel.json", "w") as f:
            json.dump(DIVIDEND_BT_REPORT, f)
            
        print("[DIVIDEND BT] 5-Year Simulation Complete.")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    run_5y_simulation()
