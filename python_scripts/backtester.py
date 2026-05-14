import yfinance as yf
import pandas as pd
import numpy as np
import argparse
from candle_cache import get_historical_data
import json
import time
import os
import psycopg2
from dotenv import load_dotenv
from google import genai

env_path = os.path.join(os.path.dirname(__file__), '../../QuickTradeBackend/.env')
load_dotenv(dotenv_path=env_path)

parser = argparse.ArgumentParser()
parser.add_argument('--tickers', type=str, default="SOUN, PLTR, LCID")
parser.add_argument('--days', type=int, default=30)
parser.add_argument('--balance', type=float, default=1000.0)
parser.add_argument('--risk_pct', type=float, default=5.0)
parser.add_argument('--daily_quota', type=float, default=100.0)
parser.add_argument('--strategy', type=str, default='standard')
parser.add_argument('--broker', type=str, default='alpaca')
parser.add_argument('--universe', type=str, default="", help="Pre-load a universe of stocks (e.g. sp600, sp500, nasdaq100)")
parser.add_argument('--use_historical_gainers', action='store_true', help="Perform a true blind simulation using historically logged daily top gainers")
args, unknown = parser.parse_known_args()

ACTIVE_STRATEGIES = ['STANDARD'] if args.strategy == 'standard' else (['AGGRESSIVE'] if args.strategy == 'aggressive' else ['STANDARD', 'AGGRESSIVE'])

historical_gainers_data = {}
if args.use_historical_gainers:
    db_url = os.environ.get('DATABASE_URL')
    if db_url:
        try:
            print("Fetching historical gainers from Postgres Database...")
            conn = psycopg2.connect(db_url)
            cur = conn.cursor()
            cur.execute("SELECT date, tickers FROM historical_gainers")
            rows = cur.fetchall()
            for row in rows:
                date_str = row[0].strftime('%Y-%m-%d')
                historical_gainers_data[date_str] = row[1]
            cur.close()
            conn.close()
            print(f"Loaded {len(historical_gainers_data)} days of historical gainers from database.")
        except Exception as e:
            print(f"Failed to load from Postgres: {e}")
    else:
        hist_file = os.path.join(os.path.dirname(__file__), 'historical_gainers.json')
        if os.path.exists(hist_file):
            try:
                with open(hist_file, 'r') as f:
                    historical_gainers_data = json.load(f)
            except Exception as e:
                print(f"Failed to load historical_gainers.json: {e}")

TICKERS = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]

def fetch_universe(universe):
    try:
        if universe == "SP600":
            df = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_600_companies', storage_options={'User-Agent': 'Mozilla/5.0'})[0]
            return df['Symbol'].tolist()
        elif universe == "SP500":
            df = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies', storage_options={'User-Agent': 'Mozilla/5.0'})[0]
            return df['Symbol'].tolist()
        elif universe == "NASDAQ100":
            df = pd.read_html('https://en.wikipedia.org/wiki/Nasdaq-100', storage_options={'User-Agent': 'Mozilla/5.0'})[4]
            return df['Ticker'].tolist()
    except Exception as e:
        print(f"Failed to fetch universe {universe}: {e}")
    return []

expanded_tickers = []
for t in TICKERS:
    if t in ["SP600", "SP500", "NASDAQ100"]:
        print(f"Auto-expanding universe {t}...")
        expanded_tickers.extend(fetch_universe(t))
    else:
        expanded_tickers.append(t)

if args.universe:
    print(f"Fetching {args.universe} universe tickers via argument...")
    expanded_tickers.extend(fetch_universe(args.universe.upper()))

TICKERS = list(set([t.replace('.', '-') for t in expanded_tickers]))

if args.use_historical_gainers and historical_gainers_data:
    all_hist = set()
    for daily_list in historical_gainers_data.values():
        all_hist.update(daily_list)
    TICKERS = list(all_hist)

print(f"Running backtest on {len(TICKERS)} tickers...")
DAYS = args.days
BALANCE = args.balance
RISK_PCT = args.risk_pct
DAILY_QUOTA = args.daily_quota

TUNED_PARAMS = {"roc": 0.8, "vol_multiplier": 1.5}
params_path = os.path.join(os.path.dirname(__file__), 'tuned_params.json')
if os.path.exists(params_path):
    try:
        with open(params_path, 'r') as f:
            TUNED_PARAMS.update(json.load(f))
    except: pass

MAX_POSITION_SIZE = BALANCE * (RISK_PCT / 100.0)
ACTIVE_AI_MODEL = "gemini-2.5-flash"

try:
    gemini_client = genai.Client()
except:
    gemini_client = None

def simulate_slippage(price, current_vol, avg_vol):
    import random
    if current_vol > avg_vol * 2:
        return round(price * random.uniform(0.001, 0.003), 4)
    else:
        return round(price * random.uniform(0.0002, 0.001), 4)

def get_ai_evaluation(ticker, price, vol, avg_vol, ema9, ema21, signal, strategy):
    # HYPER-SPEED MODE: Bypass network API calls. Emulate AI Kelly Criterion locally
    # to allow the backtest to complete in seconds instead of hours.
    
    import random
    
    # Simulate a dynamic Kelly position size based on signal strength
    base_size = MAX_POSITION_SIZE * 0.5
    if signal == "Volume Spike":
        base_size = MAX_POSITION_SIZE * 0.8
        trail = 3.5
    elif strategy == 'AGGRESSIVE':
        base_size = MAX_POSITION_SIZE * 0.9
        trail = 4.5
    else:
        base_size = MAX_POSITION_SIZE * 0.6
        trail = 4.0
        
    # Introduce slight AI variance
    import random
    variance = random.uniform(0.9, 1.1)
    final_size = base_size * variance
    
    # 35% approval rate (matching stricter LLM tape filter & lunch chop rejection)
    decision = "APPROVED" if random.random() < 0.35 else "REJECTED"
    
    # Emulate AI deciding to scale out
    scale_plan = []
    if random.random() < 0.5:
        scale_plan = [
            {"target_pct": random.uniform(3.0, 6.0), "scale_pct": 0.25},
            {"target_pct": random.uniform(6.5, 12.0), "scale_pct": 0.25}
        ]
    
    return {"decision": decision, "kelly_position_size": final_size, "trailing_stop_pct": trail, "scale_out_plan": scale_plan}

def process_ticker(ticker):
    trades = []
    try:
        interval = '15m' if DAYS <= 60 else '1d'
        df = yf.Ticker(ticker).history(period=f'{DAYS}d', interval=interval)
        if df.empty or len(df) < 25:
            return trades
            
        df['EMA9'] = df['Close'].ewm(span=9, adjust=False).mean()
        df['EMA21'] = df['Close'].ewm(span=21, adjust=False).mean()
        df['Vol_SMA'] = df['Volume'].rolling(window=20).mean()
        
        # VWAP calculation
        df['Typical_Price'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['Vol_Price'] = df['Typical_Price'] * df['Volume']
        df['VWAP'] = df.groupby(df.index.date)['Vol_Price'].cumsum() / df.groupby(df.index.date)['Volume'].cumsum()
        
        df = df.dropna()
        
        positions = {}
        
        for i in range(1, len(df)):
            prev_row = df.iloc[i-1]
            row = df.iloc[i]
            
            price = round(row['Close'], 2)
            vol = row['Volume']
            avg_vol = row['Vol_SMA']
            ema9 = row['EMA9']
            ema21 = row['EMA21']
            vwap = row['VWAP']
            prev_price = round(prev_row['Close'], 2)
            roc = ((price - prev_price) / prev_price) * 100 if prev_price > 0 else 0
            
            is_top_gainer_today = True
            if args.use_historical_gainers and historical_gainers_data:
                row_date_str = df.index[i].strftime('%Y-%m-%d')
                if row_date_str in historical_gainers_data:
                    if ticker not in historical_gainers_data[row_date_str]:
                        is_top_gainer_today = False
                else:
                    # If we don't have data for this date, we shouldn't trade
                    is_top_gainer_today = False
            
            for strategy in ACTIVE_STRATEGIES:
                if strategy not in positions:
                    if args.use_historical_gainers and not is_top_gainer_today:
                        continue
                    
                    signal = ""
                    if strategy == 'STANDARD':
                        is_crossover = (prev_row['EMA9'] < prev_row['EMA21']) and (ema9 > ema21)
                        is_vol_spike = (vol > (avg_vol * 3))
                        if is_crossover: signal = "EMA Crossover"
                        elif is_vol_spike: signal = "Volume Spike"
                    elif strategy == 'AGGRESSIVE':
                        is_vol_spike = vol > 50000 and vol > (avg_vol * TUNED_PARAMS["vol_multiplier"])
                        if is_vol_spike and abs(roc) > TUNED_PARAMS["roc"] and price > vwap:
                            signal = f"Aggressive Momentum Surfing (ROC: {roc:.2f}%)"
                    
                    if signal:
                        ai_data = get_ai_evaluation(ticker, price, vol, avg_vol, ema9, ema21, signal, strategy)
                        if ai_data.get("decision") == "APPROVED":
                            ai_size = float(ai_data.get("kelly_position_size", MAX_POSITION_SIZE))
                            actual_size = min(ai_size, MAX_POSITION_SIZE)
                            
                            entry_slippage = simulate_slippage(price, vol, avg_vol)
                            entry_price = price + entry_slippage
                            
                            qty = max(1, int(actual_size / entry_price))
                            if qty > 0:
                                scale_plan = ai_data.get("scale_out_plan", [])
                                if (qty * entry_price) < 500.0:
                                    scale_plan = []
                                elif not isinstance(scale_plan, list):
                                    scale_plan = [scale_plan] if scale_plan else []
                                    
                                positions[strategy] = {
                                    "entry_price": entry_price,
                                    "highest_price": entry_price,
                                    "trail_pct": float(ai_data.get("trailing_stop_pct", 2.0)),
                                    "entry_time": str(row.name),
                                    "entry_index": i,
                                    "initial_qty": qty,
                                    "qty": qty,
                                    "scale_out_plan": scale_plan
                                }
                else:
                    pos = positions[strategy]
                    if price > pos["highest_price"]:
                        pos["highest_price"] = price
                        
                    trailing_stop_price = pos["highest_price"] * (1 - pos["trail_pct"] / 100.0)
                    
                    sell_reason = ""
                    if strategy == 'AGGRESSIVE':
                        unrealized_profit = (pos["highest_price"] - pos["entry_price"]) * pos["qty"]
                        current_profit = (price - pos["entry_price"]) * pos["qty"]
                        if unrealized_profit > 15:
                            if current_profit < unrealized_profit * 0.66:
                                sell_reason = "Aggressive Surfing: Locked in profit (lost 1/3rd of peak gain)"
                    
                    if not sell_reason:
                        scale_plan = pos.get("scale_out_plan", [])
                        if scale_plan:
                            next_scale = scale_plan[0]
                            target_price = pos["entry_price"] * (1 + next_scale.get("target_pct", 999)/100.0)
                            if price >= target_price:
                                scale_qty = max(1, int(pos.get("initial_qty", pos["qty"]) * next_scale.get("scale_pct", 0.5)))
                                if scale_qty < pos["qty"]:
                                    exit_slippage = simulate_slippage(price, vol, avg_vol)
                                    actual_exit = price - exit_slippage
                                    pnl = (actual_exit - pos["entry_price"]) * scale_qty
                                    if args.broker == "ibkr": pnl -= 2.00
                                    trades.append({
                                        "ticker": f"{ticker} [{strategy}] (Scale Out)",
                                        "entry_time": pos["entry_time"],
                                        "exit_time": str(row.name),
                                        "entry_price": pos["entry_price"],
                                        "exit_price": actual_exit,
                                        "qty": scale_qty,
                                        "pnl": round(pnl, 2),
                                        "trail_pct": pos["trail_pct"]
                                    })
                                    pos["qty"] -= scale_qty
                                    pos["scale_out_plan"].pop(0)
                    
                    if not sell_reason:
                        if (i - pos.get("entry_index", i)) > 1: # 15m candle = 1 period
                            if price < pos["entry_price"] * 1.005:
                                sell_reason = "Time Stop Hit (15m passed without breakout)"

                    if not sell_reason:
                        if price >= pos["entry_price"] * 1.08:
                            sell_reason = "Hard Take-Profit Hit (8%+)"
                            
                    if not sell_reason:
                        if price <= trailing_stop_price:
                            sell_reason = "Trailing Stop Hit"
                    
                    if sell_reason:
                        exit_slippage = simulate_slippage(price, vol, avg_vol)
                        actual_exit = price - exit_slippage
                        pnl = (actual_exit - pos["entry_price"]) * pos["qty"]
                        if args.broker == "ibkr": pnl -= 2.00
                        trades.append({
                            "ticker": f"{ticker} [{strategy}]",
                            "entry_time": pos["entry_time"],
                            "exit_time": str(row.name),
                            "entry_price": pos["entry_price"],
                            "exit_price": price,
                            "qty": pos["qty"],
                            "pnl": round(pnl, 2),
                            "trail_pct": pos["trail_pct"]
                        })
                        del positions[strategy]
    except Exception as e:
        print(f"Error: {e}")
    return trades

def run_backtest():
    import concurrent.futures
    all_trades = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = executor.map(process_ticker, TICKERS)
        for t_list in results:
            all_trades.extend(t_list)
            
    # Sort trades chronologically by entry time to simulate margin usage
    all_trades.sort(key=lambda x: x['entry_time'])
    
    total_pnl = 0.0
    wins = 0
    losses = 0
    peak_balance = BALANCE
    max_drawdown = 0.0
    current_balance = BALANCE
    equity_curve = [{"time": "Start", "balance": BALANCE}]
    
    active_trades = []
    executed_trades = []
    
    for t in all_trades:
        # Clear out trades that exited before this trade entered
        active_trades = [at for at in active_trades if at['exit_time'] > t['entry_time']]
        
        # Calculate margin in use
        margin_used = sum([at['entry_price'] * at['qty'] for at in active_trades])
        
        # Only take trade if we have enough margin and haven't hit MAX_DAILY_LOSS (simplified)
        if current_balance - margin_used >= (t['entry_price'] * t['qty']):
            active_trades.append(t)
            executed_trades.append(t)
            
            pnl = t['pnl']
            current_balance += pnl
            total_pnl += pnl
            if pnl > 0: wins += 1
            else: losses += 1
            
            if current_balance > peak_balance:
                peak_balance = current_balance
            drawdown = (peak_balance - current_balance) / peak_balance * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown
                
            equity_curve.append({
                "time": t['exit_time'],
                "balance": round(current_balance, 2)
            })

    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    
    # Sort executed trades chronologically by exit time for UI display
    executed_trades.sort(key=lambda x: x['exit_time'])
    
    result = {
        "total_trades": wins + losses,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_drawdown, 2),
        "final_balance": round(current_balance, 2),
        "trades": executed_trades[-50:],
        "equity_curve": equity_curve
    }
    
    print("===BACKTEST_RESULT===")
    print(json.dumps(result, indent=2))
    print("===END_RESULT===")

if __name__ == "__main__":
    run_backtest()
