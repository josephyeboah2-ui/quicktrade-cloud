import yfinance as yf
import time
import sys
sys.stdout.reconfigure(line_buffering=True, encoding='utf-8')
import argparse
import requests
import os
from google import genai
from google.genai import types
import http.server
import socketserver
import json
import threading
from dotenv import load_dotenv
import random
import pandas as pd
try:
    from webull import webull
except ImportError:
    webull = None
from excel_logger import log_trade, close_position, generate_weekly_report
from local_intel_engine import query_local_intel, record_trade_outcome, get_brain_summary

env_path = os.path.join(os.path.dirname(__file__), '../../QuickTradeBackend/.env')
load_dotenv(dotenv_path=env_path)

sys.stdout.reconfigure(line_buffering=True, encoding='utf-8')

parser = argparse.ArgumentParser()
parser.add_argument('--tickers', type=str, default="SOUN, PLTR, LCID, HOLO, FFIE")
parser.add_argument('--max_size', type=float, default=1000.0)
parser.add_argument('--max_loss', type=float, default=-50.0)
parser.add_argument('--take_profit', type=float, default=3.0)
parser.add_argument('--trailing_stop', type=float, default=3.5)
parser.add_argument('--strategy', type=str, default='standard')
parser.add_argument('--force', action='store_true')
args, unknown = parser.parse_known_args()

if args.force:
    PRE_FLIGHT_STATE = "APPROVED"

LIMIT_ONLY = False
MODE_LABEL = "STANDARD"
STARTING_BALANCE = 0.0
ACTIVE_STRATEGIES = ['STANDARD'] if args.strategy == 'standard' else (['AGGRESSIVE'] if args.strategy == 'aggressive' else ['STANDARD', 'AGGRESSIVE'])

TICKERS_TO_SCAN = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]

PRICE_MIN = 0.30
PRICE_MAX = 100.00
POLL_INTERVAL_SECONDS = 5

ticker_context_cache = {}

def _refresh_l2_cache():
    """Background thread: refreshes bid/ask sizes for all scanned tickers every 30s.
    This feeds the L2 Order Book Defense System with real data."""
    import concurrent.futures
    def _fetch_one(tkr):
        try:
            info = yf.Ticker(tkr).info
            return tkr, int(info.get("bidSize") or 1), int(info.get("askSize") or 1)
        except:
            return tkr, 1, 1
    while True:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                for tkr, bid, ask in ex.map(_fetch_one, TICKERS_TO_SCAN):
                    ticker_context_cache[tkr] = {"bidSize": bid, "askSize": ask}
        except Exception as e:
            pass  # Never let this crash the main bot
        time.sleep(30)

threading.Thread(target=_refresh_l2_cache, daemon=True).start()

MAX_POSITION_SIZE = args.max_size
MAX_DAILY_LOSS = args.max_loss
TAKE_PROFIT_PCT = args.take_profit
TRAILING_STOP_PCT = args.trailing_stop

TUNED_PARAMS = {"roc": 0.8, "vol_multiplier": 1.5}
params_path = os.path.join(os.path.dirname(__file__), 'tuned_params.json')
if os.path.exists(params_path):
    try:
        with open(params_path, 'r') as f:
            TUNED_PARAMS.update(json.load(f))
    except: pass
DAILY_QUOTA = 0.0


ACTIVE_AI_MODEL = "gemini-2.5-flash"
PRE_FLIGHT_STATE = "WAITING_FOR_CONFIG"
PRE_FLIGHT_DATA = {}
bot_trades_log = []
try:
    j_path = os.path.join(os.path.dirname(__file__), "PaperTrade_Journal.csv")
    if os.path.exists(j_path):
        import pandas as pd
        import time
        df = pd.read_csv(j_path)
        for idx, row in df.iterrows():
            px = row.get("Execution_Price", row.get("Entry_Price", 0))
            if pd.isna(px): px = 0
            try:
                date_obj = pd.to_datetime(row["Date"])
                fmt_time = date_obj.strftime("%m/%d %I:%M:%S %p")
            except:
                fmt_time = str(row["Date"])
            # Create a deterministic ID so reboots don't duplicate trades in the UI
            trade_id = abs(hash(str(row["Date"]) + str(row["Ticker"]) + str(row["Side"])))
            
            pnl_val = row.get("PnL", None)
            if pd.isna(pnl_val): pnl_val = None
            else: pnl_val = float(pnl_val)
            
            pl_color = "#cccccc"
            if pnl_val is not None:
                pl_color = "#00ff6a" if pnl_val >= 0 else "#ff4a4a"
                
            bot_trades_log.append({
                  "id": trade_id,
                  "sym": str(row.get("Ticker", "UKN")),
                  "side": str(row.get("Side", "UKN")),
                  "qty": int(row["Quantity"]) if "Quantity" in row and pd.notna(row["Quantity"]) else 0,
                  "price": float(px),
                  "time": fmt_time,
                  "reason": str(row.get("Signal_Reason", "")),
                  "pl": pnl_val,
                  "plColor": pl_color,
                  "status": str(row.get("Status", "")) if "Status" in row and pd.notna(row["Status"]) else "",
                  "strategy": str(row.get("Strategy", "")) if "Strategy" in row and pd.notna(row["Strategy"]) else ""
              })
        bot_trades_log = bot_trades_log[-50:]
    print(f'LOADED {len(bot_trades_log)} TRADES INTO bot_trades_log!')
except Exception as e:
    print(f"Failed to load journal: {e}")
realtime_prices = {}


def auto_tuner_scheduler():
    import time, datetime, subprocess, os, json
    
    # Catch-Up Protocol
    params_path = os.path.join(os.path.dirname(__file__), 'tuned_params.json')
    tuner_path = os.path.join(os.path.dirname(__file__), "auto_tuner.py")
    
    # Check if params are stale on boot
    try:
        if os.path.exists(params_path):
            mtime = os.path.getmtime(params_path)
            age_hours = (time.time() - mtime) / 3600
            if age_hours > 24.0:
                print(f"\n?? [ALGO] Parameters are stale ({age_hours:.1f} hours old). Running Catch-Up AI Auto-Tuner...")
                subprocess.Popen(["python", tuner_path])
                time.sleep(60) # Give it a minute to start before beginning the main loop
    except Exception as e:
        print(f"Catch-up check failed: {e}")

    while True:
        try:
            now = datetime.datetime.now()
            if now.hour == 16 and now.minute == 5:
                print("\n?? [ALGO] Market Closed! Triggering AI Auto-Tuner parameter evolution...")
                subprocess.Popen(["python", tuner_path])
                time.sleep(60)
                
                # Hot-reload params after tuning finishes
                try:
                    time.sleep(300) # Wait 5 mins for tuning to complete
                    if os.path.exists(params_path):
                        with open(params_path, 'r') as f:
                            global TUNED_PARAMS
                            TUNED_PARAMS.update(json.load(f))
                            print("\n?? [ALGO] Hot-Reloaded newly evolved parameters into Live Engine!")
                except:
                    pass
            time.sleep(15)
        except Exception as e:
            time.sleep(60)
            
            time.sleep(15)
        except Exception as e:
            time.sleep(60)

class BotTradeHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass
        
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        
    def do_POST(self):
        global DIVIDEND_VAULT
        global SLEEPER_VAULT
        global ACTIVE_AI_MODEL
        
        if self.path == '/api/pre-flight':
            global PRE_FLIGHT_STATE
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    data = json.loads(body)
                    if PRE_FLIGHT_STATE == "WAITING_FOR_CONFIG":
                        PRE_FLIGHT_STATE = "ANALYZING"
                        threading.Thread(target=run_pre_flight_check, args=(data,), daemon=True).start()
                except Exception as e:
                    print(f"⚠️ Error starting pre-flight check: {e}")
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == '/api/pre-flight/approve':
            PRE_FLIGHT_STATE = "APPROVED"
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == '/api/bot-trades/clear':
            global bot_trades_log
            bot_trades_log.clear()
            try:
                j_path = os.path.join(os.path.dirname(__file__), "PaperTrade_Journal.csv")
                if os.path.exists(j_path):
                    os.remove(j_path)
            except Exception as e:
                print("Failed to delete journal file:", e)
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == '/api/sleeper/start':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    data = json.loads(body)
                    tickers = data.get("tickers", [])
                    balance = float(data.get("balance", 1000))
                    
                    
                    for ticker in tickers:
                        try:
                            df = yf.Ticker(ticker).history(period="1d", interval="1m")
                            if not df.empty:
                                price = float(df['Close'].iloc[-1])
                                # Do NOT buy yet! Just put it on the Watchlist.
                                SLEEPER_VAULT.append({
                                    "ticker": ticker,
                                    "status": "WATCHING",
                                    "balance": balance,
                                    "baseline_price": price,
                                    "entry_price": 0.0,
                                    "qty": 0,
                                    "high_water_mark": 0.0
                                })
                                print(f"👀 [SLEEPER] Added {ticker} to Watchlist at baseline ${price:.2f}. Waiting for awakening...")
                        except Exception as e:
                            print(f"⚠️ Failed to add sleeper {ticker}: {e}")
                            
                except Exception as e:
                    print(f"⚠️ Error processing sleeper list: {e}")
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        global DIVIDEND_VAULT
        global SLEEPER_VAULT
        if self.path == '/api/pre-flight':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"state": PRE_FLIGHT_STATE, "data": PRE_FLIGHT_DATA}).encode())
        elif self.path == '/api/bot-trades':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            self.wfile.write(json.dumps({
                'trades': bot_trades_log,
                'starting_balance': STARTING_BALANCE,
                'mode_label': MODE_LABEL
            }).encode())
        elif self.path == '/api/bot-prices':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"prices": realtime_prices}).encode())
        elif self.path == '/api/sleeper/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"vault": SLEEPER_VAULT}).encode())
        elif self.path == '/api/dividend/start':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    data = json.loads(body)
                    tickers = data.get("tickers", [])
                    balance = float(data.get("balance", 10000))
                    
                    for ticker in tickers:
                        try:
                            df = yf.Ticker(ticker).history(period="1d", interval="1m")
                            if not df.empty:
                                price = float(df['Close'].iloc[-1])
                                qty = round((balance * 0.10) / price, 2) # 10% per dividend stock
                                
                                DIVIDEND_VAULT.append({
                                    "ticker": ticker,
                                    "entry_price": price,
                                    "current_price": price,
                                    "qty": qty,
                                    "status": "HOLDING"
                                })
                                print(f"🏦 [DIVIDEND VAULT] Accumulated {qty} shares of {ticker} at ${price:.2f}.")
                        except: pass
                except: pass
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            
        elif self.path == '/api/dividend/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"vault": DIVIDEND_VAULT}).encode())
        elif self.path == '/api/history':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                file_name = 'PaperTrade_Journal.csv' if 'paper' in __file__ else 'LiveTrade_Journal.csv'
                file_path = os.path.join(base_dir, file_name)
                if os.path.exists(file_path):
                    df = pd.read_csv(file_path)
                    # Convert dates safely
                    json_str = df.to_json(orient='records', date_format='iso')
                    self.wfile.write(json_str.encode())
                else:
                    self.wfile.write(json.dumps([]).encode())
            except Exception as e:
                print(f"Error reading history: {e}")
                self.wfile.write(json.dumps([]).encode())
            except Exception as e:
                print(f"Error reading history: {e}")
                self.wfile.write(json.dumps([]).encode())
        else:
            self.send_response(404)
            self.end_headers()

def start_bot_server():
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", 8003), BotTradeHandler) as httpd:
            httpd.serve_forever()
    except Exception as e:
        print(f"⚠️ Could not start bot trade server on port 8003: {e}")

threading.Thread(target=start_bot_server, daemon=True).start()

SLEEPER_VAULT = []

def run_sleeper_manager():
    global SLEEPER_VAULT
    print("🚀 [SLEEPER MANAGER] Background Vault Monitoring Started...")
    while True:
        try:
            if not SLEEPER_VAULT:
                time.sleep(30)
                continue
                
            for position in SLEEPER_VAULT[:]:
                ticker = position['ticker']
                
                try:
                    df = yf.Ticker(ticker).history(period="1d", interval="1m")
                    if df.empty: continue
                    live_price = float(df['Close'].iloc[-1])
                except:
                    continue
                
                # --- PHASE 1: WATCHING ---
                if position['status'] == "WATCHING":
                    # If the stock drops further while sleeping, update the baseline so we catch the true bottom!
                    position['baseline_price'] = min(position['baseline_price'], live_price)
                    
                    surge_pct = ((live_price - position['baseline_price']) / position['baseline_price']) * 100
                    
                    position['current_price'] = live_price
                    if surge_pct >= 2.0:
                        print(f"🚨 [SLEEPER] {ticker} AWAKENED! Surged +{surge_pct:.2f}% from bottom!")
                        print(f"🚀 [SLEEPER] Buying {ticker} at ${live_price:.2f} and attaching 2% Trailing Stop!")
                        
                        qty = round((position.get("balance", 1000) * 0.40) / live_price, 2)
                        log_trade(ticker, "BUY", qty, live_price, "Sleeper AI Awakening Trigger")
                        
                        position['status'] = "ACTIVE"
                        position['entry_price'] = live_price
                        position['qty'] = qty
                        position['high_water_mark'] = live_price
                        
                # --- PHASE 2: ACTIVE (Holding the Position) ---
                elif position['status'] == "ACTIVE":
                    position['current_price'] = live_price
                    pnl_pct = ((live_price - position['entry_price']) / position['entry_price']) * 100
                    
                    # Cut Loss Rule (-5%)
                    if pnl_pct <= -5.0:
                        print(f"⚠️ [SLEEPER] {ticker} Awakening failed. Dropped -5%. Cutting losses at ${live_price:.2f}.")
                        close_position(ticker, live_price)
                        SLEEPER_VAULT.remove(position)
                        continue
                        
                    # Trailing Stop Management
                    position['high_water_mark'] = max(position['high_water_mark'], live_price)
                    stop_price = position['high_water_mark'] * 0.98
                    
                    if live_price <= stop_price:
                        print(f"💰 [SLEEPER] {ticker} Momentum exhausted. Trailing Stop Triggered at ${live_price:.2f}. Securing Profit!")
                        close_position(ticker, live_price)
                        SLEEPER_VAULT.remove(position)
                        continue
                        
        except Exception as e:
            print(f"⚠️ [SLEEPER] Vault Error: {e}")
            
        time.sleep(30)

threading.Thread(target=run_sleeper_manager, daemon=True).start()
DIVIDEND_VAULT = []
def run_dividend_manager():
    print("🏦 [DIVIDEND MANAGER] Passive Income Monitoring Started...")
    while True:
        try:
            if not DIVIDEND_VAULT:
                time.sleep(3600) # Check every hour for longterm
                continue
                
            for position in DIVIDEND_VAULT[:]:
                ticker = position['ticker']
                try:
                    df = yf.Ticker(ticker).history(period="1d", interval="1m")
                    if df.empty: continue
                    live_price = float(df['Close'].iloc[-1])
                    position['current_price'] = live_price
                except: continue
                
        except: pass
        time.sleep(3600)

threading.Thread(target=run_dividend_manager, daemon=True).start()

def dynamic_webull_top_gainers():
    global TICKERS_TO_SCAN
    if not webull:
        return
    wb = webull()
    while True:
        try:
            gainers = wb.active_gainer_loser('gainer')
            if gainers and isinstance(gainers, list):
                new_tickers = []
                for g in gainers:
                    try:
                        sym = g['ticker']['symbol'].upper()
                        if sym not in TICKERS_TO_SCAN:
                            new_tickers.append(sym)
                    except KeyError:
                        pass
                if new_tickers:
                    TICKERS_TO_SCAN.extend(new_tickers)
                    print(f"🔥 [WEBULL] Automatically added {len(new_tickers)} Top Gainers to active scan list!")
        except Exception as e:
            pass
        time.sleep(300)

threading.Thread(target=dynamic_webull_top_gainers, daemon=True).start()

def run_pre_flight_check(config):
    global PRE_FLIGHT_STATE, PRE_FLIGHT_DATA, TICKERS_TO_SCAN, MAX_POSITION_SIZE, MAX_DAILY_LOSS, TAKE_PROFIT_PCT, TRAILING_STOP_PCT, DAILY_QUOTA, ACTIVE_STRATEGIES, LIMIT_ONLY, MODE_LABEL, STARTING_BALANCE
    
    if config.get("force", False):
        PRE_FLIGHT_STATE = "APPROVED"
        return

    try:
        raw_tickers = config.get("tickers", TICKERS_TO_SCAN)
        if isinstance(raw_tickers, str):
            TICKERS_TO_SCAN = [t.strip().upper() for t in raw_tickers.split(",") if t.strip() and "LOADING" not in t.strip().upper()]
        else:
            TICKERS_TO_SCAN = [t for t in raw_tickers if "LOADING" not in str(t).upper()]
        MAX_POSITION_SIZE = float(config.get("max_size", MAX_POSITION_SIZE))
        MAX_DAILY_LOSS = float(config.get("max_loss", MAX_DAILY_LOSS))
        TAKE_PROFIT_PCT = float(config.get("take_profit", TAKE_PROFIT_PCT))
        TRAILING_STOP_PCT = float(config.get("trailing_stop", TRAILING_STOP_PCT))
        
        # --- STRATEGY & LIMIT PARSING ---
        ui_strategy = config.get("strategy", "standard")
        ui_limit = config.get("limitOnly", False)
        
        if ui_strategy == "auto_pilot":
            # AUTO-PILOT: always use limit orders regardless of backtest suggestion
            LIMIT_ONLY = True
            MODE_LABEL = "AUTO-PILOT"
            try:
                b_path = os.path.join(os.path.dirname(__file__), "backtest_comparison.json")
                with open(b_path, "r") as bf:
                    best = json.load(bf).get("best_strategy", {}).get("strategy", "")
                    if "Aggressive" in best:
                        ui_strategy = "aggressive"
                    else:
                        ui_strategy = "standard"
            except Exception as e:
                print("Failed to load auto-pilot intel:", e)
                ui_strategy = "standard"
                
        LIMIT_ONLY = ui_limit
        if ui_strategy == 'standard':
            ACTIVE_STRATEGIES = ['STANDARD']
            if MODE_LABEL != "AUTO-PILOT": MODE_LABEL = "STANDARD"
        elif ui_strategy == 'aggressive':
            ACTIVE_STRATEGIES = ['AGGRESSIVE']
            if MODE_LABEL != "AUTO-PILOT": MODE_LABEL = "AGGRESSIVE"
        else:
            ACTIVE_STRATEGIES = ['STANDARD', 'AGGRESSIVE']
            if MODE_LABEL != "AUTO-PILOT": MODE_LABEL = "BOTH"
        # --------------------------------
        
        DAILY_QUOTA = float(config.get("daily_quota", 0))
        daily_quota = DAILY_QUOTA
        risk_pct = float(config.get("risk_pct", 0))
        balance = float(config.get("balance", 0))
        STARTING_BALANCE = balance
        
        if balance > 0 and risk_pct > 0:
            MAX_POSITION_SIZE = balance * (risk_pct / 100.0)
            
        vol_info = "Volatility check bypassed for stability."

        prompt = f"""You are a strict quantitative risk manager. 
A day trader is attempting to launch an automated PAPER scalping algorithm with these parameters:
- Account Balance: ${balance:.2f}
- Daily Profit Quota: ${daily_quota:.2f}
- Max Position Size per trade: ${MAX_POSITION_SIZE:.2f}
- Risk Per Trade: {risk_pct:.1f}%
- Max Daily Loss Allowed: ${MAX_DAILY_LOSS:.2f}
- Tickers to trade: {', '.join(TICKERS_TO_SCAN)}
- Market Volatility: {vol_info}

Analyze if these inputs are mathematically realistic and safe. 
If the logic is safe, return state: "APPROVED".
If the logic is highly flawed (e.g., $1000 daily quota but only risking $10 a trade, or max loss is $5 but size is $1000 on volatile penny stocks), return state: "WARNING" and provide suggested safe limits (including a realistic daily_quota).
CRITICAL: Your suggested max_size MUST NEVER exceed the Account Balance of $${balance:.2f}.

Respond ONLY with a valid JSON object matching this schema:
{{
  "is_safe": boolean,
  "reasoning": "A concise, 2-3 sentence punchy analysis explaining the dangers or approving the logic.",
  "suggested": {{
    "max_size": number,
    "max_loss": number,
    "take_profit": number,
    "trailing_stop": number,
    "daily_quota": number
  }}
}}"""

        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        response = client.models.generate_content(
            model=ACTIVE_AI_MODEL,
            contents=prompt,
        )
        
        txt = response.text.replace('```json', '').replace('```', '').strip()
        result = json.loads(txt)
        
        PRE_FLIGHT_DATA = result
        if result.get("is_safe", True):
            PRE_FLIGHT_STATE = "APPROVED"
        else:
            PRE_FLIGHT_STATE = "WARNING"
            
    except Exception as e:
        print(f"⚠️ Pre-Flight Check failed: {e}")
        PRE_FLIGHT_STATE = "APPROVED"

def simulate_slippage(price, current_vol, avg_vol):
    import random
    if current_vol > avg_vol * 2:
        return round(price * random.uniform(0.001, 0.003), 4)
    else:
        return round(price * random.uniform(0.0002, 0.001), 4)

class PaperTrader:
    def __init__(self):
        self.is_running = False
        self.positions = {}
        self.consecutive_losses = 0
        self.last_ai_query = {}
        self.daily_pnl = 0.0
        self.last_activity_str = ""
        self.last_activity_time = 0
        try:
            self.gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        except Exception:
            self.gemini_client = None
            print("⚠️ Warning: Could not initialize Gemini Client for Paper Trader.")
        
    def start(self):
        self.is_running = True
        print("🤖 [ALGO] Auto-Paper Trader Started! Checking Pre-Flight...")
        threading.Thread(target=auto_tuner_scheduler, daemon=True).start()
        self.run_loop()

    def stop(self):
        self.is_running = False
        print("⏹ [ALGO] Auto-Paper Trader Stopped.")
        generate_weekly_report()

    def scan_market(self):
        top_ticker = None
        top_roc = -999
        top_stats = ""
        for ticker in TICKERS_TO_SCAN[:]:
             try:
                 data = yf.Ticker(ticker).history(period='2d', interval='15m', prepost=True)
                 if data.empty or len(data) < 25:
                     if data.empty:
                         print(f"⚠️ [{ticker}] No data found (possibly delisted). Removing from active scan.")
                         TICKERS_TO_SCAN.remove(ticker)
                     continue
                     
                 data['EMA9'] = data['Close'].ewm(span=9, adjust=False).mean()
                 data['EMA21'] = data['Close'].ewm(span=21, adjust=False).mean()
                 data['Vol_SMA'] = data['Volume'].rolling(window=20).mean()
                 
                 # VWAP calculation
                 data['Typical_Price'] = (data['High'] + data['Low'] + data['Close']) / 3
                 data['Vol_Price'] = data['Typical_Price'] * data['Volume']
                 data['VWAP'] = data.groupby(data.index.date)['Vol_Price'].cumsum() / data.groupby(data.index.date)['Volume'].cumsum()
                 
                 current_price = round(data['Close'].iloc[-1], 2)
                 realtime_prices[ticker] = current_price
                 current_vol = data['Volume'].iloc[-1]
                 avg_vol = data['Vol_SMA'].iloc[-1]
                 ema9 = data['EMA9'].iloc[-1]
                 ema21 = data['EMA21'].iloc[-1]
                 prev_ema9 = data['EMA9'].iloc[-2]
                 prev_ema21 = data['EMA21'].iloc[-2]
                 vwap = data['VWAP'].iloc[-1]

                 prev_price = round(data['Close'].iloc[-2], 2)
                 roc = ((current_price - prev_price) / prev_price) * 100

                 if roc > top_roc:
                     top_roc = roc
                     top_ticker = ticker
                     top_stats = f"${current_price} | Vol: {current_vol}"

                 if not (PRICE_MIN <= current_price <= PRICE_MAX):
                     continue

                 for strategy in ACTIVE_STRATEGIES:
                     self.evaluate_algo(ticker, strategy, current_price, ema9, ema21, prev_ema9, prev_ema21, current_vol, avg_vol, roc, vwap)
                 
             except Exception as e:
                 print(f"[ALGO] Error fetching {ticker}: {e}")
             
        if top_ticker:
            activity_str = f"   [ACTIVITY] Watching {len(TICKERS_TO_SCAN)} stocks. Hottest right now: {top_ticker} -> {top_stats}"
            current_time = time.time()
            if activity_str != self.last_activity_str or (current_time - self.last_activity_time) >= 180:
                print(activity_str)
                self.last_activity_str = activity_str
                self.last_activity_time = current_time
            
    def evaluate_algo(self, ticker, strategy, price, ema9, ema21, prev_ema9, prev_ema21, current_vol, avg_vol, roc, vwap=0.0):
        pos_key = f"{ticker}_{strategy}"
        if pos_key not in self.positions:
            signal_reason = ""
            
            if strategy == 'STANDARD':
                is_crossover = (prev_ema9 < prev_ema21) and (ema9 > ema21)
                is_vol_spike = (current_vol > (avg_vol * 3))
                
                if current_vol < 50000 or price < ema21:
                    return
                
                if is_crossover:
                    signal_reason = "EMA Crossover (9 > 21)"
                elif is_vol_spike:
                    signal_reason = "Volume Spike (>300%)"
                else:
                    # L2 BID PRESSURE: check order book imbalance as early pre-surge signal
                    l2 = ticker_context_cache.get(ticker, {})
                    bid = l2.get("bid", 0)
                    ask = l2.get("ask", 0)
                    bid_size = l2.get("bid_size", 0)
                    ask_size = l2.get("ask_size", 1)
                    if bid > 0 and ask > 0 and bid_size > 0 and ask_size > 0:
                        imbalance_ratio = bid_size / ask_size
                        spread_pct = (ask - bid) / bid * 100 if bid > 0 else 999
                        if imbalance_ratio >= 2.0 and spread_pct < 1.5 and price > vwap:
                            signal_reason = f"Order Book Pressure (Bid {imbalance_ratio:.1f}x Ask)"
                    
            elif strategy == 'AGGRESSIVE':
                is_vol_spike = current_vol > 50000 and current_vol > (avg_vol * TUNED_PARAMS["vol_multiplier"])
                
                # VWAP Institutional Filter
                if is_vol_spike and abs(roc) > TUNED_PARAMS["roc"] and price > vwap:
                    signal_reason = f"Aggressive Momentum Surfing (ROC: {roc:.2f}%)"
                else:
                    return
                    
            if signal_reason:
                now = time.time()
                if now - self.last_ai_query.get(pos_key, 0) < 300:
                    return
                self.last_ai_query[pos_key] = now

                # --- LOCAL BRAIN FIRST ---
                # Check if we have enough local intel to decide without Gemini
                local_result = query_local_intel(strategy, price, current_vol, avg_vol, roc, vwap, ticker=ticker)
                local_decision = local_result.get("decision")
                local_samples = local_result.get("samples", 0)

                if local_decision == "APPROVED":
                    print(f"\n🧠 [LOCAL BRAIN] {ticker} — {local_result['reasoning']}")
                    qty = max(1, int(MAX_POSITION_SIZE / price))
                    self.positions[pos_key] = {
                        "entry_price": price, "qty": qty, "initial_qty": qty,
                        "highest_price": price, "entry_time_ms": time.time(),
                        "trailing_stop_pct": TRAILING_STOP_PCT, "scale_out_plan": [],
                        "entry_vol": current_vol, "entry_avg_vol": avg_vol,
                        "entry_roc": roc, "entry_vwap": vwap
                    }
                    entry_slippage = simulate_slippage(price, current_vol, avg_vol)
                    execution_price = price + entry_slippage
                    log_trade("PaperTrade_Journal.csv", ticker, "BUY", qty, price, execution_price, entry_slippage, signal_reason, strategy=strategy)
                    bot_trades_log.append({
                        "id": int(time.time() * 1000), "sym": ticker, "side": "BUY",
                        "qty": qty, "price": execution_price,
                        "time": datetime.datetime.now().strftime("%m/%d %I:%M:%S %p"),
                        "reason": f"[LOCAL BRAIN] {signal_reason} ({local_samples} samples)"
                    })
                    return
                elif local_decision == "REJECTED":
                    print(f"\n🧠 [LOCAL BRAIN] SKIPPING {ticker} — {local_result['reasoning']}")
                    return
                else:
                    if local_samples > 0:
                        print(f"\n🧠 [LOCAL BRAIN] Uncertain on {ticker} ({local_samples} samples). Deferring to Gemini.")
                # --- END LOCAL BRAIN ---

                if self.gemini_client:
                    # --- AI PLAYBOOK & MEMORY INJECTION ---
                    playbook_context = ""
                    try:
                        pb_dir = os.path.join(os.path.dirname(__file__), 'ai_playbook')
                        
                        # Read Master Guidelines
                        master_path = os.path.join(pb_dir, 'trading_guidelines.md')
                        if os.path.exists(master_path):
                            with open(master_path, 'r', encoding='utf-8') as f:
                                playbook_context += f"\n[MASTER TRADING GUIDELINES]:\n{f.read()}\n"
                                
                        # Read Ticker-Specific Memory
                        ticker_mem_path = os.path.join(pb_dir, 'tickers', f"{ticker}.json")
                        if os.path.exists(ticker_mem_path):
                            with open(ticker_mem_path, 'r', encoding='utf-8') as f:
                                ticker_data = json.load(f)
                                playbook_context += f"\n[HISTORICAL MEMORY FOR {ticker}]:\n{json.dumps(ticker_data, indent=2)}\n"
                    except Exception as e:
                        print(f"Error loading playbook: {e}")
                    
                    # --- END PLAYBOOK INJECTION ---
                    
                    quota_context = ""
                    if self.daily_pnl >= DAILY_QUOTA and DAILY_QUOTA > 0:
                        quota_context = f"\n[CRITICAL QUOTA NOTICE]: Your daily profit is currently ${self.daily_pnl:.2f}, which has ALREADY REACHED the target quota of ${DAILY_QUOTA:.2f}. The name of the game is to make as much profit as possible, but safely. If the market is still surging and conditions are exceptionally strong, keep pushing in a calculated manner. However, if the setup is mediocre, REJECT it to protect the quota.\n"
                      
                tags_context = "No tags defined."
                try:
                    
                    tags_path = os.path.join(os.path.dirname(__file__), "tags.json")
                    if os.path.exists(tags_path):
                        with open(tags_path, "r", encoding="utf-8") as tf:
                            tags_dict = json.load(tf)
                            tags_context = "\n".join([f"- {k}: {v}" for k, v in tags_dict.items()])
                except Exception as e:
                    print("Error loading tags:", e)
                          
                    est_slippage = simulate_slippage(price, current_vol, avg_vol)
                    est_slippage_pct = (est_slippage / price) * 100

                    prompt = f"""You are a strict, senior quantitative day trader managing a ${MAX_POSITION_SIZE * 5:.2f} PAPER portfolio.\n{playbook_context}\n{quota_context}
[CASE STUDY TAGS (Use these to classify the setup)]:
{tags_context}

[CAPITAL PRESERVATION DIRECTIVE]: You are EXTREMELY protective of paper capital to simulate real trading. If the market environment looks poor, REJECT the trade.
Analyze this penny stock setup for {ticker}. 
Current Price: ${price} (VWAP: ${vwap:.2f})
  Estimated Slippage: ${est_slippage:.4f} ({est_slippage_pct:.2f}%)
Volume: {current_vol} (Avg: {avg_vol})
EMA9: {ema9:.2f}
EMA21: {ema21:.2f}
ROC: {roc:.2f}%
Signal: {signal_reason}

Respond ONLY with a valid JSON object. Include a scale_out_plan if you want to scale out of the position:
{{
  "decision": "APPROVED" or "REJECTED",
  "reasoning": "1-2 sentence analysis",
    "scenario_tag": "string (e.g. MORNING_PANIC_DIP or UNKNOWN)",
  "kelly_position_size": number,
  "trailing_stop_pct": number,
  "scale_out_plan": [
      {{"target_pct": 5.0, "scale_pct": 0.25}},
      {{"target_pct": 10.0, "scale_pct": 0.25}}
  ]
}}"""
                    try:
                        print(f"\n🤖 [PAPER] Asking Gemini AI to evaluate setup and calculate Kelly Criterion for {ticker}...")
                        response = self.gemini_client.models.generate_content(
                            model=ACTIVE_AI_MODEL,
                            contents=prompt,
                        )
                        txt = response.text.replace('```json', '').replace('```', '').strip()
                        ai_data = json.loads(txt)
                        
                        decision = ai_data.get('decision')
                        size = ai_data.get('kelly_position_size')
                        trail = ai_data.get('trailing_stop_pct')
                        shares = max(1, int(float(size) / price)) if size else 0
                        stop_loss = price * (1 - (float(trail)/100)) if trail else 0
                        take_profit = price * (1 + (TAKE_PROFIT_PCT/100))
                        
                        icon = "✅" if decision == "APPROVED" else "❌"
                        print(f"\n🎯 [ALGO] Setup Found: {ticker}")
                        print(f"📈 Trigger: {signal_reason} | Price: ${price:.2f}")
                        print(f"🤖 --- Gemini Evaluation ---")
                        print(f"{icon} Decision: {decision}")
                        print(f"?? Scenario Tag: {ai_data.get('scenario_tag', 'UNKNOWN')}")
                        if decision == "APPROVED":
                            print(f"💰 Kelly Size: ${size} ({shares} Shares)")
                            print(f"🎯 Target: ${take_profit:.2f} (+{TAKE_PROFIT_PCT}%) | 🛑 Stop: ${stop_loss:.2f} (-{trail}%)")
                        else:
                            print(f"📉 Reasoning: {ai_data.get('reasoning')}")
                        print("---------------------------\n")
                        
                        if ai_data.get("decision") == "APPROVED":
                            ai_size = float(ai_data.get("kelly_position_size", MAX_POSITION_SIZE))
                            actual_size = min(ai_size, MAX_POSITION_SIZE)
                            qty = max(1, int(actual_size / price))
                            ai_trail = float(ai_data.get("trailing_stop_pct", TRAILING_STOP_PCT))
                            
                            scale_plan = ai_data.get("scale_out_plan", [])
                            if (qty * price) < 500.0:
                                scale_plan = [] # Commission Block
                                
                            self.positions[pos_key] = { 
                                "entry_price": price, 
                                "qty": qty, 
                                "initial_qty": qty,
                                "highest_price": price,
                                "entry_time_ms": time.time(),
                                "trailing_stop_pct": ai_trail,
                                "scale_out_plan": scale_plan,
                                "entry_vol": current_vol,
                                "entry_avg_vol": avg_vol,
                                "entry_roc": roc,
                                "entry_vwap": vwap
                            }
                            entry_slippage = simulate_slippage(price, current_vol, avg_vol)
                            execution_price = price + entry_slippage
                            log_trade("PaperTrade_Journal.csv", ticker, "BUY", qty, price, execution_price, entry_slippage, signal_reason, strategy=strategy)
                            bot_trades_log.append({
                                "id": int(time.time() * 1000),
                                "sym": ticker,
                                "side": "BUY",
                                "qty": qty,
                                "price": execution_price,
                                "time": datetime.datetime.now().strftime("%m/%d %I:%M:%S %p"),
                                "reason": f"[PAPER] {signal_reason}"
                            })
                        else:
                            print(f"🛑 [PAPER] Trade skipped for {ticker} because Gemini rejected the setup.")
                        return
                        
                    except Exception as e:
                        print(f"⚠️ [PAPER] Gemini API Error: {e}")
                        return
                else:
                    # No Gemini AI — calculate shares from the user's configured MAX_POSITION_SIZE budget
                    qty = max(1, int(MAX_POSITION_SIZE / price))
                    cost = qty * price
                    print(f"[PAPER] No-AI fallback buy: {qty} shares of {ticker} @ ${price:.2f} = ${cost:.2f} (budget: ${MAX_POSITION_SIZE:.2f})")
                    self.positions[pos_key] = { "entry_price": price, "qty": qty, "highest_price": price, "trailing_stop_pct": TRAILING_STOP_PCT }
                    entry_slippage = simulate_slippage(price, current_vol, avg_vol)
                    execution_price = price + entry_slippage
                    log_trade("PaperTrade_Journal.csv", ticker, "BUY", qty, price, execution_price, entry_slippage, signal_reason, strategy=strategy)
                    bot_trades_log.append({
                        "id": int(time.time() * 1000),
                        "sym": ticker,
                        "side": "BUY",
                        "qty": qty,
                        "price": execution_price,
                        "time": datetime.datetime.now().strftime("%m/%d %I:%M:%S %p"),
                        "reason": f"[PAPER] {signal_reason}"
                    })

        else:
            entry_price = self.positions[pos_key]["entry_price"]
            qty = self.positions[pos_key]["qty"]
            
            highest_price = self.positions[pos_key].get("highest_price", entry_price)
            if price > highest_price:
                self.positions[pos_key]["highest_price"] = price
                highest_price = price
                  
            # --- L2 ORDER BOOK DEFENSE SYSTEM ---
            ctx = ticker_context_cache.get(ticker, {"bidSize": 1, "askSize": 1})
            bids = ctx.get("bidSize", 1)
            if bids == 0: bids = 1 # Prevent div by zero
            asks = ctx.get("askSize", 1)
            current_trail = self.positions[pos_key].get("trailing_stop_pct", TRAILING_STOP_PCT)
            
            if asks > bids * 5 and current_trail > 1.5:
                print(f"?? [L2 DEFENSE] MASSIVE Sell Wall on {ticker} (Asks {asks}x vs Bids {bids}x). Panic tightening stop to 1.5%!")
                self.positions[pos_key]["trailing_stop_pct"] = 1.5
            elif asks > bids * 3 and current_trail > 2.5:
                print(f"?? [L2 DEFENSE] Heavy Sell Pressure on {ticker} (Asks {asks}x vs Bids {bids}x). Tightening stop to 2.5%.")
                self.positions[pos_key]["trailing_stop_pct"] = 2.5
            # --- END L2 DEFENSE ---
                
            take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT / 100.0)
            trade_trail_pct = self.positions[pos_key].get("trailing_stop_pct", TRAILING_STOP_PCT)
            trailing_stop_price = highest_price * (1 - trade_trail_pct / 100.0)
              
            is_rocketing = (ema9 > ema21) and (current_vol > avg_vol)
            
            # Aggressive 1/3rd profit protection logic
            reason = ""
            if strategy == 'AGGRESSIVE':
                unrealized_profit = (highest_price - entry_price) * qty
                current_profit = (price - entry_price) * qty
                if unrealized_profit > 15: # minimum profit threshold to care
                    if current_profit < unrealized_profit * 0.66: # lost 1/3rd of profit
                        reason = f"Aggressive Surfing: Locked in profit (lost 1/3rd of peak ${unrealized_profit:.2f} gain)"
            
            # Multi-Tier Scale Out Execution
            if not reason:
                scale_plan = self.positions[pos_key].get("scale_out_plan", [])
                if scale_plan:
                    next_scale = scale_plan[0]
                    target_price = entry_price * (1 + next_scale.get("target_pct", 999)/100.0)
                    if price >= target_price:
                        scale_qty = max(1, int(self.positions[pos_key].get("initial_qty", qty) * next_scale.get("scale_pct", 0.5)))
                        if scale_qty < qty: # Partial sell
                            print(f"🚀 [PAPER] AI Scaling Out: Locked in {next_scale.get('scale_pct')*100}% at +{next_scale.get('target_pct')}%")
                            self.positions[pos_key]["qty"] -= scale_qty
                            self.positions[pos_key]["scale_out_plan"].pop(0) # Remove executed target
                            scale_pnl = (price - entry_price) * scale_qty
                            pl_color = "#00ff6a" if scale_pnl >= 0 else "#ff4a4a"
                            bot_trades_log.append({
                                "id": int(time.time() * 1000),
                                "sym": ticker,
                                "side": "SELL",
                                "qty": scale_qty,
                                "price": price,
                                "exit_price": round(price, 4),
                                "entry_price": round(entry_price, 4),
                                "pl": round(scale_pnl, 2),
                                "plColor": pl_color,
                                "time": datetime.datetime.now().strftime("%m/%d %I:%M:%S %p"),
                                "reason": f"Scale Out (+{next_scale.get('target_pct')}%)"
                            })
                            qty = self.positions[pos_key]["qty"]
                            
            # Time Stop Execution
            if not reason:
                entry_time = self.positions[pos_key].get("entry_time_ms", time.time())
                if (time.time() - entry_time) > 15 * 60: # 15 minutes
                    if price < entry_price * 1.005: # Not significantly in profit
                        reason = "Time Stop (15m elapsed without breakout)"
                        
            if not reason:
                # Standard mode uses Take Profit. Aggressive mode ignores it to ride the surge infinitely.
                if strategy == 'STANDARD' and price >= take_profit_price:
                    if is_rocketing:
                        print(f"🚀 [{ticker}] Take Profit target reached, but momentum is strong! Letting it ride.")
                    else:
                        reason = f"Take Profit Hit (${price:.2f}) - Momentum Faded"
                elif price <= trailing_stop_price:
                    reason = f"Trailing Stop Hit (Locked in gains from ${highest_price:.2f})" if highest_price > entry_price else "Stop Loss Hit"
                
            if reason:
                print(f"[PAPER SIGNAL] [{strategy}] {reason} on {ticker} at ${price}")
                exit_slippage = simulate_slippage(price, current_vol, avg_vol)
                actual_exit = price - exit_slippage
                trade_pnl = (actual_exit - entry_price) * qty
                self.daily_pnl += trade_pnl
                close_position("PaperTrade_Journal.csv", ticker, price, actual_exit, exit_slippage, strategy=strategy)
                pl_color = "#00ff6a" if trade_pnl >= 0 else "#ff4a4a"
                bot_trades_log.append({
                    "id": int(time.time() * 1000),
                    "sym": ticker,
                    "side": "SELL",
                    "qty": qty,
                    "price": actual_exit,
                    "exit_price": round(actual_exit, 4),
                    "entry_price": round(entry_price, 4),
                    "pl": round(trade_pnl, 2),
                    "plColor": pl_color,
                    "time": datetime.datetime.now().strftime("%m/%d %I:%M:%S %p"),
                    "reason": reason,
                    "strategy": strategy,
                    "status": "CLOSED"
                })
                # --- TEACH THE LOCAL BRAIN ---
                entry_vol = self.positions[pos_key].get("entry_vol", current_vol)
                entry_avg_vol = self.positions[pos_key].get("entry_avg_vol", avg_vol)
                entry_roc = self.positions[pos_key].get("entry_roc", roc)
                entry_vwap = self.positions[pos_key].get("entry_vwap", vwap)
                record_trade_outcome(strategy, entry_price, entry_vol, entry_avg_vol, entry_roc, entry_vwap, pnl=trade_pnl, ticker=ticker)
                # --- END TEACHING ---

                del self.positions[pos_key]
                if trade_pnl < 0:
                    self.consecutive_losses += 1
                    print(f"⚠️ [RISK ALERT] Consecutive Losses: {self.consecutive_losses}")
                else:
                    self.consecutive_losses = 0

    def run_loop(self):
        global PRE_FLIGHT_STATE
        last_log_time = 0
        while self.is_running:
            if PRE_FLIGHT_STATE in ["WAITING_FOR_CONFIG", "ANALYZING", "WARNING"]:
                time.sleep(0.5)
                continue
            elif PRE_FLIGHT_STATE == "REJECTED":
                self.is_running = False
                break
            


            self.scan_market()
            time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    trader = PaperTrader()
    trader.start()
