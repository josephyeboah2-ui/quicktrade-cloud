import json
import os
import yfinance as yf
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def analyze_dividends(tickers):
    print("[DIVIDEND AI] Gathering S&P 500 Yield Data...")
    
    candidates = []
    for ticker in tickers[:20]: # Test 20
        try:
            info = yf.Ticker(ticker).info
            yield_pct = info.get('dividendYield', 0)
            if yield_pct and yield_pct > 0.04: # > 4% yield
                candidates.append(f"{ticker}: {yield_pct*100:.2f}% Yield")
        except: pass
        
    intel_str = "No historical intel."
    try:
        with open(os.path.join(os.path.dirname(__file__), "dividend_intel.json"), "r") as f:
            intel = json.load(f)
            if "intel_tags" in intel:
                intel_str = "HISTORICAL TAG INTEL (5-Yr Avg Cash Collected by Sector):\\n"
                sorted_tags = sorted(intel['intel_tags'].items(), key=lambda x: x[1]['avg_yield'], reverse=True)
                for tag, data in sorted_tags:
                    intel_str += f"- {tag}: ${data['avg_yield']} collected\\n"
    except: pass
    
    print("[DIVIDEND AI] Uplinking to Gemini for Longterm Selection...")
    
    prompt = f'''You are a quantitative value investor. We scanned the market for stocks with >4% dividend yields.
    
    Candidates:
    {", ".join(candidates)}
    
    CRITICAL INTELLIGENCE (5-Year Backtest):
    {intel_str}
    
    Task: Select the top 5 most reliable, fundamentally strong dividend stocks from the candidates list.
    Return ONLY a JSON array of strings of the 5 chosen tickers. No markdown, no explanations. Example: ["T", "VZ"]'''
    
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(prompt)
        raw_text = response.text.replace("```json", "").replace("```", "").strip()
        picks = json.loads(raw_text)
        print(f"[DIVIDEND AI] Top Picks Secured: {picks}")
        return picks
    except Exception as e:
        print(f"Error: {e}")
        return []

if __name__ == "__main__":
    print(analyze_dividends(["T", "VZ", "MO", "PM", "PFE", "MMM", "IBM", "CVX", "XOM", "KO"]))
