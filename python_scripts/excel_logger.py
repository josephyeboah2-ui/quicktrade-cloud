import pandas as pd
import os
import sys

# Force immediate flush of print statements to fix PowerShell log buffering
sys.stdout.reconfigure(line_buffering=True)
from datetime import datetime

import os
def get_path(journal_file):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, journal_file)

def _load_or_create_df(journal_file):
    journal_file = get_path(journal_file)
    if os.path.exists(journal_file):
        return pd.read_excel(journal_file, sheet_name="Trade_Log")
    else:
        df = pd.DataFrame(columns=[
            "Date", "Ticker", "Side", "Quantity", "Expected_Price", "Execution_Price", 
            "Exit_Price", "Entry_Slippage", "Exit_Slippage", "PnL", "Signal_Reason", "Status", "Strategy"
        ])
        return df

def log_trade(journal_file, ticker, side, qty, expected_price, execution_price, entry_slippage, signal_reason, strategy="STANDARD"):
    df = _load_or_create_df(journal_file)
    
    # Ensure Strategy column exists in old journals
    if "Strategy" not in df.columns:
        df["Strategy"] = "STANDARD"
    
    new_trade = {
        "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Ticker": ticker,
        "Side": side.upper(),
        "Quantity": qty,
        "Expected_Price": expected_price,
        "Execution_Price": execution_price,
        "Exit_Price": None,
        "Entry_Slippage": entry_slippage,
        "Exit_Slippage": 0.0,
        "PnL": None,
        "Signal_Reason": signal_reason,
        "Status": "OPEN",
        "Strategy": strategy.upper()
    }
    
    df = pd.concat([df, pd.DataFrame([new_trade])], ignore_index=True)
    
    with pd.ExcelWriter(journal_file, engine="openpyxl", mode="w") as writer:
        df.to_excel(writer, sheet_name="Trade_Log", index=False)
        
    print(f"[LOGGER] Logged trade to {os.path.basename(journal_file)}: [{strategy.upper()}] {side} {qty} {ticker} @ {execution_price} (Slip: {entry_slippage})")

def close_position(journal_file, ticker, expected_exit, actual_exit, exit_slippage, strategy="STANDARD"):
    df = _load_or_create_df(journal_file)
    
    # Ensure Strategy column exists in old journals
    if "Strategy" not in df.columns:
        df["Strategy"] = "STANDARD"
    
    # Find the most recent open position for this ticker matching the strategy
    open_trades = df[(df['Ticker'] == ticker) & (df['Status'] == 'OPEN') & (df['Side'] == 'BUY') & (df['Strategy'] == strategy.upper())]
    if open_trades.empty:
        # Fallback for old schema where strategy might not be perfectly aligned
        open_trades = df[(df['Ticker'] == ticker) & (df['Status'] == 'OPEN') & (df['Side'] == 'BUY')]
        if open_trades.empty:
            return
        
    last_idx = open_trades.index[-1]
    
    # Check if we are reading from old schema where 'Entry_Price' was used instead of 'Execution_Price'
    if 'Execution_Price' in df.columns and not pd.isna(df.at[last_idx, 'Execution_Price']):
        entry_price = float(df.at[last_idx, 'Execution_Price'])
    else:
        entry_price = float(df.at[last_idx, 'Entry_Price'])
        
    qty = float(df.at[last_idx, 'Quantity'])
    
    # Calculate PnL locally based on ACTUAL fill
    pnl = (actual_exit - entry_price) * qty
    
    df.at[last_idx, 'Exit_Price'] = actual_exit
    df.at[last_idx, 'Exit_Slippage'] = exit_slippage
    df.at[last_idx, 'PnL'] = round(pnl, 4)
    df.at[last_idx, 'Status'] = "CLOSED"
    
    with pd.ExcelWriter(journal_file, engine="openpyxl", mode="w") as writer:
        df.to_excel(writer, sheet_name="Trade_Log", index=False)
        
    print(f"[LOGGER] Closed {ticker} in {os.path.basename(journal_file)}: Exit @ {actual_exit} (Slip: {exit_slippage}) | PnL: ${round(pnl, 2)}")

def generate_weekly_report(journal_file):
    if not os.path.exists(journal_file):
        print(f"No log file found at {journal_file}. Execute some trades first.")
        return
        
    df = pd.read_excel(journal_file, sheet_name="Trade_Log")
    
    closed_trades = df[df['Status'] == 'CLOSED']
    total_trades = len(closed_trades)
    
    if total_trades == 0:
        print("No closed trades to report yet.")
        return
        
    winning_trades = closed_trades[closed_trades['PnL'] > 0]
    win_rate = (len(winning_trades) / total_trades) * 100
    total_pnl = closed_trades['PnL'].sum()
    
    summary = {
        "Metric": ["Total Closed Trades", "Winning Trades", "Win Rate (%)", "Total PnL ($)"],
        "Value": [total_trades, len(winning_trades), round(win_rate, 2), round(total_pnl, 2)]
    }
    
    summary_df = pd.DataFrame(summary)
    
    # Append the summary to a new sheet
    with pd.ExcelWriter(journal_file, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
        summary_df.to_excel(writer, sheet_name="Weekly_Report", index=False)
        
    print(f"\n--- WEEKLY ALGO REPORT GENERATED FOR {os.path.basename(journal_file)} ---")
    print(summary_df.to_string(index=False))
    print("------------------------------------\n")
