import os
import json
import time
import concurrent.futures
import yfinance as yf
from dotenv import load_dotenv
import google.generativeai as genai

try:
    from webull import webull
except ImportError:
    webull = None

# Load Gemini API Key
env_path = os.path.join(os.path.dirname(__file__), '../../QuickTradeBackend/.env')
load_dotenv(env_path)
API_KEY = os.getenv("GEMINI_API_KEY")

if API_KEY:
    genai.configure(api_key=API_KEY)

def calculate_sleeper_score(ticker):
    """
    Mathematical Pre-Filter:
    Finds if a stock is a 'Sleeper' (Down from its 52-week high, low recent volume)
    """
    try:
        df = yf.Ticker(ticker).history(period="1y")
        if df.empty or len(df) < 50:
            return None
        
        high_52w = df['High'].max()
        current_price = df['Close'].iloc[-1]
        
        # If it's too close to the high, it's not sleeping. We want it down at least 50%
        if current_price > (high_52w * 0.5):
            return None
            
        # Calculate Volume Dropoff (Recent 10 days vs previous spikes)
        recent_vol = df['Volume'].iloc[-10:].mean()
        max_vol = df['Volume'].max()
        
        # If recent volume is less than 10% of its max spike volume, it is truly asleep
        if recent_vol > (max_vol * 0.1):
            return None
            
        drop_pct = ((high_52w - current_price) / high_52w) * 100
        return {"ticker": ticker, "drop_pct": drop_pct, "price": current_price, "high": high_52w}
    except Exception:
        return None

def run_sleeper_scan():
    print("?? [SLEEPER AI] Initializing Sleeper Protocol...")
    if not webull:
        print("?? Webull library missing.")
        return

    wb = webull()
    print("?? [SLEEPER AI] Harvesting Top 600 Most Profitable Stocks of the Year (52w)...")
    resp = wb.active_gainer_loser('gainer', rank_type='52w', count=600)
    data = resp.get('data', [])
    
    tickers = [g.get('ticker', {}).get('symbol') for g in data if g.get('ticker', {}).get('symbol')]
    print(f"?? [SLEEPER AI] Collected {len(tickers)} historical gainers. Running Mathematical Pre-Filter...")

    sleepers = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = executor.map(calculate_sleeper_score, tickers)
        for res in results:
            if res:
                sleepers.append(res)
                
    # Sort by the most devastating drop % to find the deepest sleepers
    sleepers = sorted(sleepers, key=lambda x: x['drop_pct'], reverse=True)[:30]
    print(f"?? [SLEEPER AI] Filtered down to Top {len(sleepers)} Mathematical Sleepers.")
    
    if not API_KEY:
        print("?? [SLEEPER AI] No Gemini API Key found. Halting.")
        return
        
    
    try:
        
        comp_path = os.path.join(os.path.dirname(__file__), "backtest_comparison.json")
        with open(comp_path, "r") as f:
            comp_intel = json.load(f)
            best = comp_intel.get('best_strategy', {})
            intel_str += "\nCRITICAL TAG INTELLIGENCE (90-Day Strategy Comparison):\n"
            intel_str += f"- Best Strategy Configuration: {best.get('strategy')}\n"
            intel_str += f"- Win Rate: {best.get('win_rate')}%, Yield: {best.get('yield')}%, Max Drawdown: {best.get('max_drawdown')}%\n"
            intel_str += "ADJUST YOUR ASSET SELECTION TO FAVOR THIS EXACT CONFIGURATION BEHAVIOR.\n"
    except: pass

    print("?? [SLEEPER AI] Uplinking to Google Gemini for Cycle Analysis...")
    prompt = f"""
    You are an elite quantitative hedge fund manager specializing in Cyclical Small-Cap Breakouts.
    I am giving you a list of {len(sleepers)} stocks. All of these stocks were massive gainers over the last year, 
    but they have all bled out and are currently 'sleeping' down 50-90% from their highs with dead volume.
    
    Data: {json.dumps(sleepers)}
    
    Task: Identify the Top 10 stocks from this list that have the highest probability of waking up and surging soon.
    Look for specific sector rotations, historical meme-cycles, or structural support levels.
    
    CRITICAL INTELLIGENCE: Our 1-year backtest generated the following Win Rates for the Awakening strategy across different sectors:
    """ + tag_intel_str + """
    
    You MUST heavily prioritize stocks that fall into the sectors with the highest historical win rates, and avoid sectors with low win rates.
    
    Return EXACTLY a JSON array of the top 10 tickers like this:
    ["TICKER1", "TICKER2", "TICKER3", ...]
    Do NOT return any markdown, do not return any explanations. JUST the raw JSON array.
    """
    
    try:
        model = genai.GenerativeModel("gemini-2.5-pro")
        response = model.generate_content(prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        top_10 = json.loads(text)
        print("\n??? [SLEEPER AI] Google Gemini has identified the Top 10 Sleepers:")
        print(f"??? WATCHLIST: {', '.join(top_10)}\n")
        return top_10
    except Exception as e:
        print(f"?? [SLEEPER AI] Gemini Analysis Failed: {e}")

if __name__ == "__main__":
    run_sleeper_scan()
