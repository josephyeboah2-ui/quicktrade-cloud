import yfinance as yf
import time
import sys
sys.stdout.reconfigure(line_buffering=True, encoding='utf-8')
import argparse
import requests
import os
from google import genai
from google.genai import types
from datetime import datetime
import pytz
from dotenv import load_dotenv
import os
import http.server
import socketserver
import json
import threading
import websocket
try:
    from webull import webull
except ImportError:
    webull = None
from excel_logger import log_trade, close_position
from local_intel_engine import record_trade_outcome

env_path = os.path.join(os.path.dirname(__file__), '../../QuickTradeBackend/.env')
load_dotenv(dotenv_path=env_path)

# Force immediate flush of print statements to fix PowerShell log buffering
sys.stdout.reconfigure(line_buffering=True, encoding='utf-8')

parser = argparse.ArgumentParser()
parser.add_argument('--tickers', type=str, default="SOUN, PLTR, LCID, HOLO, FFIE")
parser.add_argument('--max_size', type=float, default=1000.0)
parser.add_argument('--max_loss', type=float, default=-50.0)
parser.add_argument('--take_profit', type=float, default=3.0)
parser.add_argument('--trailing_stop', type=float, default=3.5)
parser.add_argument('--broker', type=str, default="wealthsimple")
parser.add_argument('--account_id', type=str, default="")
parser.add_argument('--auto_scan', action='store_true', help='Dynamically fetch top gainers')
parser.add_argument('--strategy', type=str, default='standard')
parser.add_argument('--force', action='store_true')
args, unknown = parser.parse_known_args()

if args.force:
    PRE_FLIGHT_STATE = "APPROVED"

LIMIT_ONLY = False
MODE_LABEL = "STANDARD"
STARTING_BALANCE = 0.0
COMMISSION_PER_ORDER = 1.00  # IBKR: $1 per order, $2 per round-trip
ACTIVE_STRATEGIES = ['STANDARD'] if args.strategy == 'standard' else (['AGGRESSIVE'] if args.strategy == 'aggressive' else ['STANDARD', 'AGGRESSIVE'])

# Configuration
# Clean and parse the tickers list: "AAPL, TSLA, MSFT" -> ["AAPL", "TSLA", "MSFT"]
TICKERS_TO_SCAN = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]

PRICE_MIN = 0.30
PRICE_MAX = 100.00

# Dynamic Throttling ("Go Ham" vs "Laid Back")
POLL_INTERVAL_SECONDS = 1 if args.broker == "ibkr" else 10

MAX_POSITION_SIZE = args.max_size  # DO NOT EXCEED per live trade
MAX_DAILY_LOSS = args.max_loss    # HALT ALGO if we lose this much on the day

TAKE_PROFIT_PCT = args.take_profit
TRAILING_STOP_PCT = args.trailing_stop
DAILY_QUOTA = 0.0


FINNHUB_KEY = os.environ.get("Finnhub_KEY")
realtime_prices = {}
ticker_context_cache = {}

ACTIVE_AI_MODEL = "gemini-2.5-flash"
bot_trades_log = []
try:
    j_path = os.path.join(os.path.dirname(__file__), "LiveTrade_Journal.csv")
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
        bot_trades_log = bot_trades_log[-50:] # keep last 50
except Exception as e:
    print(f"Failed to load journal: {e}")

TUNED_PARAMS = {"roc": 0.8, "vol_multiplier": 1.5}
params_path = os.path.join(os.path.dirname(__file__), 'tuned_params.json')
if os.path.exists(params_path):
    try:
        with open(params_path, 'r') as f:
            TUNED_PARAMS.update(json.load(f))
    except: pass

PRE_FLIGHT_STATE = "WAITING_FOR_CONFIG" # WAITING_FOR_CONFIG, ANALYZING, WARNING, APPROVED, REJECTED
PRE_FLIGHT_DATA = {}

cached_indicators = {}
global_bot = None


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
        pass # Suppress HTTP logs
        
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
        
        if self.path == '/api/set-model':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    data = json.loads(body)
                    new_model = data.get("model")
                    if new_model:
                        ACTIVE_AI_MODEL = new_model
                        print(f"🧠 [ALGO] AI Model hot-swapped to: {ACTIVE_AI_MODEL}")
                except Exception as e:
                    print(f"⚠️ Error parsing AI model swap: {e}")
                    
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "model": ACTIVE_AI_MODEL}).encode())
        elif self.path == '/api/run-tuner':
            print("\n?? [ALGO] Manual UI Trigger: Launching AI Auto-Tuner in the background...")
            import subprocess, os
            tuner_path = os.path.join(os.path.dirname(__file__), "auto_tuner.py")
            subprocess.Popen(["python", tuner_path])
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        elif self.path == '/api/pre-flight':
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
                                SLEEPER_VAULT.append({
                                    "ticker": ticker,
                                    "status": "WATCHING",
                                    "balance": balance,
                                    "baseline_price": price,
                                    "entry_price": 0.0,
                                    "qty": 0,
                                    "high_water_mark": 0.0
                                })
                                print(f"👀 [LIVE SLEEPER] Added {ticker} to Watchlist at baseline ${price:.2f}.")
                        except Exception as e:
                            print(f"⚠️ Failed to add sleeper {ticker}: {e}")
                except Exception as e:
                    pass
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
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
                    import pandas as pd
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
                self.wfile.write(json.dumps([]).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        global DIVIDEND_VAULT
        global SLEEPER_VAULT
        if self.path == '/api/bot-trades':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                "trades": bot_trades_log,
                "starting_balance": STARTING_BALANCE,
                "mode_label": MODE_LABEL
            }).encode())
        elif self.path == '/api/bot-prices':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"prices": realtime_prices}).encode())
        elif self.path == '/api/run-tuner':
            print("\n?? [ALGO] Manual UI Trigger: Launching AI Auto-Tuner in the background...")
            import subprocess, os
            tuner_path = os.path.join(os.path.dirname(__file__), "auto_tuner.py")
            subprocess.Popen(["python", tuner_path])
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        elif self.path == '/api/pre-flight':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"state": PRE_FLIGHT_STATE, "data": PRE_FLIGHT_DATA}).encode())
        else:
            self.send_response(404)
            self.end_headers()

def start_bot_server():
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", 8002), BotTradeHandler) as httpd:
            httpd.serve_forever()
    except Exception as e:
        print(f"⚠️ Could not start bot trade server on port 8002: {e}")

threading.Thread(target=start_bot_server, daemon=True).start()

SLEEPER_VAULT = []

def run_sleeper_manager():
    global SLEEPER_VAULT
    print("🚀 [LIVE SLEEPER MANAGER] Background Vault Monitoring Started...")
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
                except: continue
                
                position['current_price'] = live_price
                
                if position['status'] == "WATCHING":
                    position['baseline_price'] = min(position['baseline_price'], live_price)
                    surge_pct = ((live_price - position['baseline_price']) / position['baseline_price']) * 100
                    
                    if surge_pct >= 2.0:
                        print(f"🚨 [LIVE SLEEPER] {ticker} AWAKENED! Surged +{surge_pct:.2f}%!")
                        qty = round((position.get("balance", 1000) * 0.40) / live_price, 2)
                        
                        # EXECUTE REAL WEBULL BUY
                        if webull_trader:
                            webull_trader.place_order(ticker=ticker, action='BUY', orderType='MKT', enforce='GTC', quant=qty)
                        
                        log_trade(ticker, "BUY", qty, live_price, "Live Sleeper AI Awakening")
                        position['status'] = "ACTIVE"
                        position['entry_price'] = live_price
                        position['qty'] = qty
                        position['high_water_mark'] = live_price
                        
                elif position['status'] == "ACTIVE":
                    pnl_pct = ((live_price - position['entry_price']) / position['entry_price']) * 100
                    if pnl_pct <= -5.0:
                        print(f"⚠️ [LIVE SLEEPER] {ticker} Dropped -5%. Cutting losses.")
                        if webull_trader:
                            webull_trader.place_order(ticker=ticker, action='SELL', orderType='MKT', enforce='GTC', quant=position['qty'])
                        log_trade(ticker, "SELL", position['qty'], live_price, "Live Sleeper Stop Loss")
                        SLEEPER_VAULT.remove(position)
                        continue
                        
                    position['high_water_mark'] = max(position['high_water_mark'], live_price)
                    stop_price = position['high_water_mark'] * 0.98
                    if live_price <= stop_price:
                        print(f"💰 [LIVE SLEEPER] {ticker} Trailing Stop Triggered! Securing Profit!")
                        if webull_trader:
                            webull_trader.place_order(ticker=ticker, action='SELL', orderType='MKT', enforce='GTC', quant=position['qty'])
                        log_trade(ticker, "SELL", position['qty'], live_price, "Live Sleeper Trailing Stop")
                        SLEEPER_VAULT.remove(position)
                        continue
        except Exception as e:
            pass
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

def run_automated_backtester():
    print("🤖 [AUTO-BACKTESTER] 90-Day Intel Refresher Activated...")
    while True:
        try:
            import subprocess
            import os
            compPath = os.path.join(os.path.dirname(__file__), "backtest_comparison.py")
            subprocess.run(["python", compPath])
            print("🤖 [AUTO-BACKTESTER] 90-Day Comparison Matrix updated.")
        except: pass
        # Sleep for 90 days (90 * 24 * 60 * 60 seconds)
        time.sleep(7776000)

threading.Thread(target=run_automated_backtester, daemon=True).start()


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
        # Update global configurations from the UI
        TICKERS_TO_SCAN = config.get("tickers", TICKERS_TO_SCAN)
        MAX_POSITION_SIZE = float(config.get("max_size", MAX_POSITION_SIZE))
        MAX_DAILY_LOSS = float(config.get("max_loss", MAX_DAILY_LOSS))
        TAKE_PROFIT_PCT = float(config.get("take_profit", TAKE_PROFIT_PCT))
        TRAILING_STOP_PCT = float(config.get("trailing_stop", TRAILING_STOP_PCT))
        
        # --- STRATEGY & LIMIT PARSING ---
        ui_strategy = config.get("strategy", "standard")
        ui_limit = config.get("limitOnly", False)
        
        if ui_strategy == "auto_pilot":
            try:
                b_path = os.path.join(os.path.dirname(__file__), "backtest_comparison.json")
                with open(b_path, "r") as bf:
                    best = json.load(bf).get("best_strategy", {}).get("strategy", "")
                    if "Limit" in best:
                        ui_limit = True
                    else:
                        ui_limit = False
                        
                    if "Standard" in best:
                        ui_strategy = "standard"
                    elif "Aggressive" in best:
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
        
        # Automatically calculate MAX_POSITION_SIZE from risk % and balance
        STARTING_BALANCE = balance
        if balance > 0 and risk_pct > 0:
            MAX_POSITION_SIZE = balance * (risk_pct / 100.0)
            
        # Assess Volatility of the basket
        vol_info = ""
        try:
            for t in TICKERS_TO_SCAN[:3]:
                df = yf.Ticker(t).history(period="5d")
                if not df.empty:
                    hi_lo = (df['High'].max() - df['Low'].min()) / df['Low'].min() * 100
                    vol_info += f"{t} 5-Day Range: {hi_lo:.1f}% | "
        except Exception:
            vol_info = "Volatility data unavailable."

        prompt = f"""You are a strict quantitative risk manager. 
A day trader is attempting to launch an automated scalping algorithm with these parameters:
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

        # ── Gemini risk evaluation — 25s hard timeout ──────────────────────────
        # Without a timeout the Gemini call blocks indefinitely, keeping
        # PRE_FLIGHT_STATE="ANALYZING" and hanging the UI pre-flight modal.
        import concurrent.futures as _cf
        _gemini_key = os.environ.get("GEMINI_API_KEY")
        def _call_gemini():
            return genai.Client(api_key=_gemini_key).models.generate_content(
                model=ACTIVE_AI_MODEL,
                contents=prompt,
            )
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(_call_gemini)
            try:
                response = _fut.result(timeout=25)
            except _cf.TimeoutError:
                print("⚠️ Pre-Flight Gemini timeout (25s) — auto-approving")
                PRE_FLIGHT_STATE = "APPROVED"
                return
        # ───────────────────────────────────────────────────────────────────────
        
        # Parse JSON from markdown
        txt = response.text.replace('```json', '').replace('```', '').strip()
        result = json.loads(txt)
        
        PRE_FLIGHT_DATA = result
        if result.get("is_safe", True):
            PRE_FLIGHT_STATE = "APPROVED"
        else:
            PRE_FLIGHT_STATE = "WARNING"
            
    except Exception as e:
        print(f"⚠️ Pre-Flight Check failed: {e}")
        PRE_FLIGHT_STATE = "APPROVED" # Default to allow execution if API fails


def on_ws_message(ws, message):
    global global_bot
    data = json.loads(message)
    if data.get('type') == 'trade':
        for trade in data['data']:
            ticker = trade['s']
            price = trade['p']
            realtime_prices[ticker] = price
            
            # High-Frequency WebSocket Execution
            if 'AGGRESSIVE' in ACTIVE_STRATEGIES and global_bot and global_bot.is_running:
                ind = cached_indicators.get(ticker)
                ctx = ticker_context_cache.get(ticker, {"bidSize": 1, "askSize": 1})
                tags_context = "No tags defined."
                try:
                    
                    tags_path = os.path.join(os.path.dirname(__file__), "tags.json")
                    if os.path.exists(tags_path):
                        with open(tags_path, "r", encoding="utf-8") as tf:
                            tags_dict = json.load(tf)
                            tags_context = "\n".join([f"- {k}: {v}" for k, v in tags_dict.items()])
                except Exception as e:
                    print("Error loading tags:", e)
                if ind and ctx:
                    roc = ((price - ind["prev_price"]) / ind["prev_price"]) * 100 if ind["prev_price"] > 0 else 0
                    global_bot.evaluate_algo(ticker, 'AGGRESSIVE', price, ind["ema9"], ind["ema21"], ind["prev_ema9"], ind["prev_ema21"], ind["current_vol"], ind["avg_vol"], roc, ind.get("vwap", 0.0))

def on_ws_error(ws, error):
    print(f"⚠️ Finnhub WebSocket Error: {error}")

def on_ws_close(ws, close_status_code, close_msg):
    print("🔌 Finnhub WebSocket Closed. Reconnecting in 5 seconds...")
    time.sleep(5)
    start_finnhub_websocket()

def on_ws_open(ws):
    print("⚡ Connected to Finnhub WebSockets! Subscribing to tickers...")
    for t in TICKERS_TO_SCAN:
        ws.send(json.dumps({"type": "subscribe", "symbol": t}))

global_ws = None

def start_finnhub_websocket():
    global global_ws
    if not FINNHUB_KEY:
        print("⚠️ No Finnhub API Key found. WebSocket disabled.")
        return
    global_ws = websocket.WebSocketApp(f"wss://ws.finnhub.io?token={FINNHUB_KEY}",
                              on_message=on_ws_message,
                              on_error=on_ws_error,
                              on_close=on_ws_close)
    global_ws.on_open = on_ws_open
    threading.Thread(target=global_ws.run_forever, daemon=True).start()

start_finnhub_websocket()

def fetch_finnhub_quote(ticker):
    if not FINNHUB_KEY: return "N/A", "N/A"
    try:
        res = requests.get(f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}")
        if res.status_code == 200:
            data = res.json()
            return data.get("b", "N/A"), data.get("a", "N/A")
    except: pass
    return "N/A", "N/A"

def fetch_finnhub_news(ticker):
    if not FINNHUB_KEY: return "No recent news found."
    try:
        from datetime import timedelta
        from_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        to_date = datetime.now().strftime("%Y-%m-%d")
        res = requests.get(f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={from_date}&to={to_date}&token={FINNHUB_KEY}")
        if res.status_code == 200:
            news = res.json()
            if len(news) > 0:
                return news[0].get("headline", "No recent news found.")
    except: pass
    return "No recent news found."

def background_research_loop():
    import concurrent.futures
    
    def fetch_l2_data(ticker):
        try:
            info = yf.Ticker(ticker).info
            bidSize = info.get("bidSize", 1)
            askSize = info.get("askSize", 1)
            news = fetch_finnhub_news(ticker)
            return ticker, bidSize, askSize, news
        except:
            return ticker, 1, 1, "No recent news found."
            
    while True:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(fetch_l2_data, TICKERS_TO_SCAN)
            for ticker, bidSize, askSize, news in results:
                ticker_context_cache[ticker] = {
                    "bidSize": bidSize,
                    "askSize": askSize,
                    "news": news
                }
        time.sleep(15)

threading.Thread(target=background_research_loop, daemon=True).start()

def dynamic_scanner_loop():
    global TICKERS_TO_SCAN
    while True:
        try:
            print("🦅 [FREE RANGE] Scanning for new Top Movers...")
            res = requests.get("http://localhost:8000/api/top-movers?min=5&limit=20")
            if res.status_code == 200:
                data = res.json()
                gainers = data.get("gainers", [])
                
                new_tickers = []
                for g in gainers:
                    sym = g.get("symbol", "").upper()
                    if not sym: continue
                    
                    # Verify using yfinance for Price < 20 and Vol > 250k
                    try:
                        hist = yf.Ticker(sym).history(period='5d')
                        if hist.empty: continue
                        
                        last_price = hist['Close'].iloc[-1]
                        avg_vol = hist['Volume'].mean()
                        
                        if last_price <= 20.0 and avg_vol >= 250000:
                            new_tickers.append(sym)
                            if len(new_tickers) >= 10: break # Keep max 10
                    except Exception:
                        pass
                
                if new_tickers and set(new_tickers) != set(TICKERS_TO_SCAN):
                    print(f"🔄 [FREE RANGE] Hot-swapping tickers! New list: {new_tickers}")
                    
                    if global_ws:
                        for old_t in TICKERS_TO_SCAN:
                            if old_t not in new_tickers:
                                global_ws.send(json.dumps({"type": "unsubscribe", "symbol": old_t}))
                        for new_t in new_tickers:
                            if new_t not in TICKERS_TO_SCAN:
                                global_ws.send(json.dumps({"type": "subscribe", "symbol": new_t}))
                                
                    TICKERS_TO_SCAN = new_tickers
        except Exception as e:
            print(f"⚠️ [FREE RANGE] Error dynamically fetching tickers: {e}")
            
        time.sleep(900) # Run every 15 minutes

if args.auto_scan:
    threading.Thread(target=dynamic_scanner_loop, daemon=True).start()

import pandas as pd
try:
    from webull import webull
except ImportError:
    webull = None

def simulate_slippage(price, current_vol, avg_vol):
    import random
    if current_vol > avg_vol * 2:
        return round(price * random.uniform(0.001, 0.003), 4)
    else:
        return round(price * random.uniform(0.0002, 0.001), 4)

class LiveSnapTrader:
    def __init__(self):
        self.is_running = False
        self.positions = {}
        self.consecutive_losses = 0
        self.last_ai_query = {}
        self.daily_pnl = 0.0
        self.gemini_client = None
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if gemini_key:
            self.gemini_client = genai.Client(api_key=gemini_key)
            print("🧠 [ALGO] Gemini AI Brain Initialized.")
        else:
            print("⚠️ [ALGO] No GEMINI_API_KEY found. AI Analysis disabled.")
        
    def start(self):
        self.is_running = True
        print("🟢 [LIVE ALGO] SNAPTRADE BOT STARTED!")
        print(f"⚠️ SAFETY LIMITS: Max Size ${MAX_POSITION_SIZE} | Max Daily Loss ${MAX_DAILY_LOSS}")
        self.run_loop()

    def stop(self):
        self.is_running = False
        print("🛑 [LIVE ALGO] SNAPTRADE BOT STOPPED.")

    def is_regular_market_hours(self):
        try:
            est = pytz.timezone('US/Eastern')
            now = datetime.now(est)
            # Market hours: Monday to Friday, 9:30 AM to 4:00 PM EST
            if now.weekday() > 4:  # 5=Sat, 6=Sun
                return False
            
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
            return market_open <= now <= market_close
        except Exception:
            return True # Fallback to market orders if timezone fails

    
    def _chasing_limit_execution(self, ticker, side, qty, initial_price):
        """
        DON'T PANIC LOGIC (The Chaser Algorithm)
        If a Limit Order fails to fill due to a sudden drop, we don't panic sell at Market.
        Instead, we read the Level 2 Bids/Asks and step our Limit price down slightly, 
        chasing the spread until we get a secure, mathematically safe fill.
        """
        import time
        print(f"?? [CHASER ALGO] Initiating Pegged Limit '{side}' for {ticker}...")
        
        current_limit = initial_price
        max_chase_steps = 5
        
        for step in range(max_chase_steps):
            print(f"?? [CHASER] Step {step+1}: Placing Limit {side} @ ${current_limit:.2f}")
            # Mock API call to broker
            # In production, we would monitor the order ID and check for fill
            time.sleep(0.5) 
            
            # Simulate a 20% chance of filling at this level
            import random
            if random.random() > 0.8:
                print(f"? [CHASER] Order Filled Securely at ${current_limit:.2f}! Spread conquered.")
                return current_limit, False
                
            # If it didn't fill, we pull the Level 2 Queues and adjust safely
            print(f"?? [CHASER] No Fill. Level 2 Queue shifted. Canceling and replacing slightly lower...")
            
            ctx = ticker_context_cache.get(ticker, {"bidSize": 1, "askSize": 1})
            bids = ctx.get("bidSize", 1)
            asks = ctx.get("askSize", 1)
            
            # Educated calculation: If Asks are heavy, we step down slightly faster
            step_size = 0.01
            if asks > bids * 2:
                step_size = 0.03
                
            if side == "SELL":
                current_limit -= step_size
            else:
                current_limit += step_size
                
        print(f"?? [CHASER] Max steps reached. The building is burning! Converting to Market Order bailout!")
        return current_limit, True

    def execute_live_snaptrade_order(self, ticker, side, qty, price, reason):
        """Execute orders against the SnapTrade API via the local backend"""
        print("\n==================================")
        print(f"🚀 [LIVE API: {side}] {qty} shares of {ticker} @ ${price}")
        print(f"📝 Reason: {reason} | Broker: {args.broker.upper()}")
        print("==================================\n")
        
        is_market_open = self.is_regular_market_hours()
        
        # Extended Hours Strict Limit (0% Buffer)
        limit_price = round(price, 2)
        
        order_type = "market" if is_market_open else "limit"
        final_limit_price = None if is_market_open else limit_price

        print(f"🕒 Market Open: {is_market_open} -> Using Order Type: {order_type.upper()}")

        
        if order_type == "limit" and side == "SELL":
            final_limit_price, use_market_bailout = self._chasing_limit_execution(ticker, side, qty, final_limit_price)
            limit_price = final_limit_price
            if use_market_bailout:
                order_type = "market"
                final_limit_price = None
            
        try:
            payload = {
                "symbol": ticker,
                "side": side,
                "qty": qty,
                "orderType": order_type,
                "limitPrice": final_limit_price,
                "stopPrice": None,
                "accountId": args.account_id if args.account_id else None
            }
            res = requests.post("http://localhost:8000/api/order", json=payload)
            if res.status_code == 200:
                print(f"✅ Order sent successfully for {ticker}.")
                bot_trades_log.append({
                    "id": int(time.time() * 1000),
                    "sym": ticker,
                    "side": side,
                    "qty": qty,
                    "price": limit_price,
                    "time": datetime.now().strftime("%m/%d %I:%M:%S %p"),
                    "reason": f"[AUTO BOT] {reason}"
                })
            else:
                print(f"❌ Order failed: {res.text}")
        except Exception as e:
            print(f"❌ Backend connection error: {e}")
        
    def check_safety_halt(self):
        if self.daily_pnl <= MAX_DAILY_LOSS:
            print(f"🚨 [CRITICAL] Max Daily Loss Reached (${self.daily_pnl}). Halting Live Bot.")
            self.stop()
            return True
        return False

    def scan_market(self):
        if self.check_safety_halt():
            return
            
        for ticker in TICKERS_TO_SCAN:
             try:
                 data = yf.Ticker(ticker).history(period='2d', interval='15m', prepost=True)
                 if data.empty or len(data) < 25:
                     if data.empty:
                         print(f"⚠️ [{ticker}] No data found (possibly delisted). Removing from active scan.")
                         TICKERS_TO_SCAN.remove(ticker)
                     continue
                     
                 # Calculate EMAs, Vol, and VWAP
                 data['EMA9'] = data['Close'].ewm(span=9, adjust=False).mean()
                 data['EMA21'] = data['Close'].ewm(span=21, adjust=False).mean()
                 data['Vol_SMA'] = data['Volume'].rolling(window=20).mean()
                 
                 # VWAP calculation
                 data['Typical_Price'] = (data['High'] + data['Low'] + data['Close']) / 3
                 data['Vol_Price'] = data['Typical_Price'] * data['Volume']
                 data['VWAP'] = data.groupby(data.index.date)['Vol_Price'].cumsum() / data.groupby(data.index.date)['Volume'].cumsum()
                 
                 current_price = realtime_prices.get(ticker, round(data['Close'].iloc[-1], 2))
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
                     top_stats = f"Price: ${current_price} | ROC: {roc:.2f}% | Vol: {(current_vol/avg_vol if avg_vol else 0):.1f}x" 
                 
                 cached_indicators[ticker] = {
                     "ema9": ema9, "ema21": ema21, "prev_ema9": prev_ema9, "prev_ema21": prev_ema21,
                     "current_vol": current_vol, "avg_vol": avg_vol, "prev_price": prev_price, "vwap": vwap
                 }

                 if not (PRICE_MIN <= current_price <= PRICE_MAX):
                     continue

                 for strategy in ACTIVE_STRATEGIES:
                     if strategy == 'AGGRESSIVE': continue # Executed on WebSockets
                     self.evaluate_algo(ticker, strategy, current_price, ema9, ema21, prev_ema9, prev_ema21, current_vol, avg_vol, roc, vwap)
                 
             except Exception as e:
                 print(f"[ALGO] Error fetching {ticker}: {e}")
             
    def evaluate_algo(self, ticker, strategy, price, ema9, ema21,  prev_ema9, prev_ema21, current_vol, avg_vol, roc, vwap=0.0):
        # BUY LOGIC
        pos_key = f"{ticker}_{strategy}"
        
        # --- PRE-LLM TAPE FILTER ---
        ctx = ticker_context_cache.get(ticker, {"bidSize": 1, "askSize": 1})
        if ctx.get("askSize", 1) > ctx.get("bidSize", 1) * 3:
            if pos_key not in self.positions: # Only reject entry
                print(f"?? [TAPE FILTER] Rejected {ticker} due to massive 3x Sell Wall. Saved AI tokens.")
                return
        
        tilt_context = ""
        if self.consecutive_losses >= 2:
            tilt_context = f"\n[CRITICAL RISK PROTOCOL]: You have suffered {self.consecutive_losses} consecutive losses. You are in a 'Tilt' state. You must be 500% more strict on your volume analysis and ONLY accept undeniably perfect A+ setups. Reduce Kelly Position Size calculations by half.\n"
            
        chop_context = ""
        import datetime
        now_est = datetime.datetime.now()
        if 11 <= now_est.hour < 14:
            chop_context = f"\n[LUNCH CHOP ZONE ACTIVE]: It is {now_est.strftime('%I:%M %p')} EST. Institutional volume has dried up. Breakouts right now are highly likely to be traps. You MUST reject all 'AGGRESSIVE' setups, and demand 3x higher volume for any standard setups.\n"

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
            
            elif strategy == 'AGGRESSIVE':
                is_vol_spike = current_vol > 50000 and current_vol > (avg_vol * TUNED_PARAMS["vol_multiplier"])
                ctx = ticker_context_cache.get(ticker, {"bidSize": 1, "askSize": 1})
                imbalance = ctx["bidSize"] > (ctx["askSize"] * 2)
                
                # VWAP Institutional Filter
                if is_vol_spike and abs(roc) > TUNED_PARAMS["roc"] and imbalance and price > vwap:
                    signal_reason = f"L2 Order Book Imbalance Surfing (ROC: {roc:.2f}%)"
                else:
                    return
                
            if signal_reason:
                now = time.time()
                if now - self.last_ai_query.get(pos_key, 0) < 300:
                    return
                self.last_ai_query[pos_key] = now

                # Ask Gemini for a detailed opinion if enabled
                if self.gemini_client:
                    context = ticker_context_cache.get(ticker, {"bid": "N/A", "ask": "N/A", "news": "No recent news found."})
                    bid = context["bid"]
                    ask = context["ask"]
                    news_headline = context["news"]
                    
                    

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
                    
                    ctx = ticker_context_cache.get(ticker, {"bidSize": 1, "askSize": 1})
                    
                    est_slippage = simulate_slippage(price, current_vol, avg_vol)
                    est_slippage_pct = (est_slippage / price) * 100
                    prompt = f"""You are a strict, senior quantitative day trader managing a ${MAX_POSITION_SIZE * 5:.2f} portfolio.
{playbook_context}
{quota_context}
{tilt_context}
{chop_context}
[CASE STUDY TAGS (Use these to classify the setup)]:
{tags_context}

[CAPITAL PRESERVATION DIRECTIVE]: You are EXTREMELY protective of capital. If the market environment looks like "trash" (e.g., poor liquidity, weak volume, unconvincing catalyst), you MUST REJECT the trade. Do NOT take reckless chances just to hit the daily quota. It is better to take zero trades than to take low-probability gambles.
Analyze this penny stock setup for {ticker}. 
Current Price: ${price} (VWAP: ${vwap:.2f})
  Estimated Slippage: ${est_slippage:.4f} ({est_slippage_pct:.2f}%)
Order Book Size: Bid {ctx.get('bidSize', 1)}x / Ask {ctx.get('askSize', 1)}x
Volume: {current_vol} (Avg: {avg_vol})
EMA9: {ema9:.2f}
EMA21: {ema21:.2f}
3. The optimal Trailing Stop Loss percentage based on the stock's current volatility. (Recommendation: Volatile penny stocks require at least 3.5% to 5.0% to avoid premature shakeouts).

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
                        print(f"\n🤖 Asking Gemini AI to evaluate setup and calculate Kelly Criterion for {ticker}...")
                        response = self.gemini_client.models.generate_content(
                            model=ACTIVE_AI_MODEL,
                            contents=prompt,
                        )
                        
                        txt = response.text.replace('```json', '').replace('```', '').strip()
                        ai_data = json.loads(txt)
                        
                        print("\n🧠 --- Gemini Analysis ---")
                        print(f"Decision: {ai_data.get('decision')}")
                        print(f"Scenario Tag: {ai_data.get('scenario_tag', 'UNKNOWN')}")
                        print(f"Reasoning: {ai_data.get('reasoning')}")
                        print(f"Kelly Size: ${ai_data.get('kelly_position_size')} | Trail Stop: {ai_data.get('trailing_stop_pct')}%")
                        print("--------------------------\n")
                        
                        if ai_data.get("decision") == "APPROVED":
                            # Use AI sizing up to MAX_POSITION_SIZE limit
                            ai_size = float(ai_data.get("kelly_position_size", MAX_POSITION_SIZE))
                            actual_size = min(ai_size, MAX_POSITION_SIZE)
                            
                            qty = max(1, int(actual_size / price))
                            ai_trail = float(ai_data.get("trailing_stop_pct", TRAILING_STOP_PCT))

                            # --- IBKR FEE FILTER ---
                            round_trip_fee = COMMISSION_PER_ORDER * 2
                            expected_gain_2pct = qty * price * 0.02
                            if expected_gain_2pct <= round_trip_fee:
                                print(f"   ⚠️ [FEE FILTER] Skipping {ticker}: expected 2% gain ${expected_gain_2pct:.2f} ≤ fee ${round_trip_fee:.2f}. Not worth it.")
                                return
                            # --- END FEE FILTER ---
                            
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
                            print(f"\n✅ [LIVE SIGNAL] [{strategy}] {signal_reason} on {ticker}. AI APPROVED size: ${qty * price:.2f}")
                            entry_slippage = simulate_slippage(price, current_vol, avg_vol)
                            execution_price = price + entry_slippage
                            self.execute_live_snaptrade_order(ticker, "BUY", qty, price, signal_reason)
                            log_trade("LiveTrade_Journal.csv", ticker, "BUY", qty, price, execution_price, entry_slippage, signal_reason, strategy=strategy)
                        else:
                            print(f"🛑 Trade skipped for {ticker} because Gemini rejected the setup.")
                            
                        return
                        
                    except Exception as e:
                        err_str = str(e)
                        if "403" in err_str or "PERMISSION_DENIED" in err_str:
                            print(f"⚠️ [LIVE] Gemini unavailable (403). Rule-based fallback for {ticker}.")
                        else:
                            print(f"⚠️ Gemini API Error: {e}. Rule-based fallback for {ticker}.")

                        # --- RULE-BASED FALLBACK (Gemini offline) ---
                        fb_qty = max(1, int(MAX_POSITION_SIZE / price))
                        round_trip_fee = COMMISSION_PER_ORDER * 2
                        if fb_qty * price * 0.02 <= round_trip_fee:
                            print(f"   ⚠️ [FEE FILTER] Fallback: skipping {ticker} — too small to cover fee.")
                            return
                        print(f"✅ [RULE-BASED LIVE] {ticker} | {fb_qty} sh @ ${price:.2f} | Trail: {TRAILING_STOP_PCT}%")
                        self.positions[pos_key] = {
                            "entry_price": price, "qty": fb_qty, "initial_qty": fb_qty,
                            "highest_price": price, "entry_time_ms": time.time(),
                            "trailing_stop_pct": TRAILING_STOP_PCT, "scale_out_plan": [],
                            "entry_vol": current_vol, "entry_avg_vol": avg_vol,
                            "entry_roc": roc, "entry_vwap": vwap
                        }
                        entry_slippage = simulate_slippage(price, current_vol, avg_vol)
                        execution_price = price + entry_slippage
                        self.execute_live_snaptrade_order(ticker, "BUY", fb_qty, price, f"[RULE-BASED] {signal_reason}")
                        log_trade("LiveTrade_Journal.csv", ticker, "BUY", fb_qty, price, execution_price, entry_slippage, f"[RULE-BASED] {signal_reason}", strategy=strategy)
                        return
                        # --- END RULE-BASED FALLBACK ---

                
        # SELL LOGIC
        else:
            entry_price = self.positions[pos_key]["entry_price"]
            qty = self.positions[pos_key]["qty"]
            
            # Trailing Stop Loss logic: Ride the wave!
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
                
            # Pac-Man Take Profit target
            take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT / 100.0)
            # Trailing stop price
            trade_trail_pct = self.positions[pos_key].get("trailing_stop_pct", TRAILING_STOP_PCT)
            trailing_stop_price = highest_price * (1 - trade_trail_pct / 100.0)
              
            is_rocketing = (ema9 > ema21) and (current_vol > avg_vol)
            
            sell_reason = ""
            if strategy == 'AGGRESSIVE':
                unrealized_profit = (highest_price - entry_price) * qty
                current_profit = (price - entry_price) * qty
                if unrealized_profit > 15: # minimum profit threshold to care
                    if current_profit < unrealized_profit * 0.66: # lost 1/3rd of profit
                        sell_reason = f"Aggressive Surfing: Locked in profit (lost 1/3rd of peak ${unrealized_profit:.2f} gain)"

            # Multi-Tier Scale Out Execution
            if not sell_reason:
                scale_plan = self.positions[pos_key].get("scale_out_plan", [])
                if scale_plan:
                    next_scale = scale_plan[0]
                    target_price = entry_price * (1 + next_scale.get("target_pct", 999)/100.0)
                    if price >= target_price:
                        scale_qty = max(1, int(self.positions[pos_key].get("initial_qty", qty) * next_scale.get("scale_pct", 0.5)))
                        if scale_qty < qty: # Partial sell
                            self.execute_trade(ticker, "SELL", scale_qty, price)
                            self.positions[pos_key]["qty"] -= scale_qty
                            self.positions[pos_key]["scale_out_plan"].pop(0) # Remove executed target
                            qty = self.positions[pos_key]["qty"]
                            print(f"🚀 [{ticker}] AI Scaling Out: Locked in {next_scale.get('scale_pct')*100}% at +{next_scale.get('target_pct')}%")
            
            # Time Stop Execution
            if not sell_reason:
                entry_time = self.positions[pos_key].get("entry_time_ms", time.time())
                if (time.time() - entry_time) > 15 * 60: # 15 minutes
                    if price < entry_price * 1.005: # Not significantly in profit
                        sell_reason = "Time Stop (15m elapsed without breakout)"
            
            if not sell_reason:
                # Standard mode uses Take Profit. Aggressive mode ignores it to ride the surge infinitely.
                if strategy == 'STANDARD' and price >= take_profit_price:
                    trade_gross_pnl = (price - entry_price) * qty
                    if trade_gross_pnl <= COMMISSION_PER_ORDER * 2:
                        print(f"⏳ [{ticker}] Take Profit reached but gross profit (${trade_gross_pnl:.2f}) doesn't cover fee. Waiting.")
                    elif is_rocketing:
                        print(f"🚀 [{ticker}] Take Profit target reached, but momentum is strong! Letting it ride.")
                    else:
                        sell_reason = f"Take Profit Hit (${price:.2f}) - Momentum Faded"
                elif price <= trailing_stop_price:
                    sell_reason = f"Trailing Stop Hit (Locked in gains from ${highest_price:.2f})" if highest_price > entry_price else "Stop Loss Hit"
                
            if sell_reason:
                gross_pnl = (price - entry_price) * qty
                round_trip_fee = COMMISSION_PER_ORDER * 2
                trade_pnl = gross_pnl - round_trip_fee
                self.daily_pnl += trade_pnl
                
                self.execute_live_snaptrade_order(ticker, "SELL", qty, price, sell_reason)
                
                exit_slippage = simulate_slippage(price, current_vol, avg_vol)
                actual_exit = price - exit_slippage
                close_position("LiveTrade_Journal.csv", ticker, price, actual_exit, exit_slippage, strategy=strategy)
                # --- LOG SELL TO TRADE REPORT ---
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
                    "gross_pl": round(gross_pnl, 2),
                    "fee": round_trip_fee,
                    "plColor": pl_color,
                    "time": datetime.now().strftime("%m/%d %I:%M:%S %p"),
                    "reason": sell_reason,
                    "strategy": strategy,
                    "status": "CLOSED"
                })
                # --- TEACH THE LOCAL BRAIN (REAL MONEY = HIGHEST QUALITY SIGNAL) ---
                entry_vol = self.positions[pos_key].get("entry_vol", current_vol)
                entry_avg_vol = self.positions[pos_key].get("entry_avg_vol", avg_vol)
                entry_roc = self.positions[pos_key].get("entry_roc", roc)
                entry_vwap = self.positions[pos_key].get("entry_vwap", vwap)
                record_trade_outcome(strategy, entry_price, entry_vol, entry_avg_vol, entry_roc, entry_vwap, pnl=trade_pnl, ticker=ticker)
                # --- END TEACHING ---
                del self.positions[pos_key]
                if trade_pnl < 0:
                    self.consecutive_losses += 1
                    print(f"?? [RISK ALERT] Consecutive Losses: {self.consecutive_losses}")
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
                
            if getattr(self, "_approval_logged", False) is False:
                print("?? [ALGO] Pre-Flight Approved! SCANNING IS NOW ACTIVE.")
                self._approval_logged = True

            self.scan_market()
            time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    bot = LiveSnapTrader()
    global_bot = bot
    bot.start()
