import pandas as pd
import json
import os
from google import genai
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), '../../QuickTradeBackend/.env')
load_dotenv(dotenv_path=env_path)

API_KEY = os.environ.get("GEMINI_API_KEY")
ACTIVE_AI_MODEL = "gemini-2.5-flash"

if not API_KEY:
    print("No Gemini API key found. Cannot run post-mortem.")
    exit(1)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

def run_debrief():
    paper_path = os.path.join(os.path.dirname(__file__), 'PaperTrade_Journal.xlsx')
    live_path = os.path.join(os.path.dirname(__file__), 'LiveTrade_Journal.xlsx')
    
    dfs = []
    
    if os.path.exists(paper_path):
        try:
            df_paper = pd.read_excel(paper_path)
            df_paper['Journal_Source'] = 'PAPER'
            if not df_paper.empty:
                dfs.append(df_paper)
        except Exception as e:
            print(f"Could not read paper excel file: {e}")
            
    if os.path.exists(live_path):
        try:
            df_live = pd.read_excel(live_path)
            df_live['Journal_Source'] = 'LIVE'
            if not df_live.empty:
                dfs.append(df_live)
        except Exception as e:
            print(f"Could not read live excel file: {e}")

    if not dfs:
        print("No trades to analyze in either journal.")
        return
        
    combined_df = pd.concat(dfs, ignore_index=True)
    
    # Convert dataframe to a readable string format for the AI
    trades_csv = combined_df.to_csv(index=False)
    
    prompt = f"""You are the senior trading analyst for a quantitative hedge fund.
The trading bot has just finished its session. Here is the raw trade journal (containing both LIVE real money trades and PAPER simulated trades):

{trades_csv}

Your job is to perform a post-mortem analysis and update the AI Playbook.
1. Identify macro patterns: what times of day or setup types resulted in the biggest wins or worst losses?
2. Analyze Slippage Penalties: the `Entry_Slippage` and `Exit_Slippage` columns represent the penalty incurred from the expected price. Did high-momentum setups at specific times of day cause unacceptable slippage? If so, recommend stricter volume/price requirements to mitigate this.
3. Identify ticker-specific quirks: did a specific stock chop us out repeatedly? Did one trend beautifully?
4. Evaluate Split Testing Performance: The `Strategy` column indicates whether a trade was executed under the 'STANDARD' or 'AGGRESSIVE' strategy. Compare the Win Rate, Average PnL, and Slippage of both strategies. Which one performed better in current market conditions? Add a rule advising on which strategy to favor moving forward.

Output your response strictly as a JSON object matching this schema:
{{
  "master_guidelines": "A markdown string containing 3-5 high-level bullet points to add to the core rules.",
  "ticker_memories": {{
    "TICKER": "A short 1-2 sentence memory to inject next time we trade this specific stock."
  }}
}}
"""

    print("🧠 Asking Gemini to analyze today's performance...")
    try:
        response = client.models.generate_content(
            model=ACTIVE_AI_MODEL,
            contents=prompt,
        )
        txt = response.text.replace('```json', '').replace('```', '').strip()
        data = json.loads(txt)
        
        pb_dir = os.path.join(os.path.dirname(__file__), 'ai_playbook')
        os.makedirs(pb_dir, exist_ok=True)
        tickers_dir = os.path.join(pb_dir, 'tickers')
        os.makedirs(tickers_dir, exist_ok=True)
        
        # 1. Update Master Guidelines
        master_path = os.path.join(pb_dir, 'trading_guidelines.md')
        current_master = ""
        if os.path.exists(master_path):
            with open(master_path, 'r', encoding='utf-8') as f:
                current_master = f.read()
                
        new_master = current_master + f"\n\n## Debrief Notes ({pd.Timestamp.now().strftime('%Y-%m-%d')})\n{data.get('master_guidelines', '')}"
        
        with open(master_path, 'w', encoding='utf-8') as f:
            f.write(new_master)
            
        # 2. Update Ticker Memories
        ticker_mems = data.get('ticker_memories', {})
        for t, mem in ticker_mems.items():
            t_path = os.path.join(tickers_dir, f"{t}.json")
            mem_data = {"memory": mem, "last_updated": pd.Timestamp.now().isoformat()}
            with open(t_path, 'w', encoding='utf-8') as f:
                json.dump(mem_data, f, indent=2)
                
        print("✅ Playbook updated successfully. The bot is now smarter.")
        
    except Exception as e:
        print(f"❌ Failed to run debrief: {e}")

if __name__ == "__main__":
    run_debrief()
