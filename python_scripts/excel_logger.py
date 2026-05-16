import csv
import os
import sys

# Force immediate flush of print statements to fix PowerShell log buffering
sys.stdout.reconfigure(line_buffering=True)
from datetime import datetime

COLUMNS = [
    "Date", "Ticker", "Side", "Quantity", "Expected_Price", "Execution_Price",
    "Exit_Price", "Entry_Slippage", "Exit_Slippage", "PnL", "Signal_Reason", "Status", "Strategy"
]

def _csv_path(journal_file):
    """Always resolve to a .csv file next to this script, regardless of what extension was passed in."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Strip any extension and force .csv so old callers passing .xlsx still work
    name = os.path.splitext(os.path.basename(journal_file))[0] + ".csv"
    return os.path.join(base_dir, name)

def get_path(journal_file):
    return _csv_path(journal_file)

def _load_rows(csv_file):
    """Return list of dicts from the CSV, or empty list if file doesn't exist."""
    if not os.path.exists(csv_file):
        return []
    with open(csv_file, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    # Backfill Strategy column for old files that didn't have it
    for row in rows:
        if "Strategy" not in row or row["Strategy"] == "":
            row["Strategy"] = "STANDARD"
    return rows

def _save_rows(csv_file, rows):
    """Write list of dicts back to CSV, creating the file if needed."""
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

def log_trade(journal_file, ticker, side, qty, expected_price, execution_price, entry_slippage, signal_reason, strategy="STANDARD"):
    csv_file = _csv_path(journal_file)
    rows = _load_rows(csv_file)

    new_trade = {
        "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Ticker": ticker,
        "Side": side.upper(),
        "Quantity": qty,
        "Expected_Price": expected_price,
        "Execution_Price": execution_price,
        "Exit_Price": "",
        "Entry_Slippage": entry_slippage,
        "Exit_Slippage": 0.0,
        "PnL": "",
        "Signal_Reason": signal_reason,
        "Status": "OPEN",
        "Strategy": strategy.upper()
    }

    rows.append(new_trade)
    _save_rows(csv_file, rows)
    print(f"[LOGGER] Logged trade to {os.path.basename(csv_file)}: [{strategy.upper()}] {side} {qty} {ticker} @ {execution_price} (Slip: {entry_slippage})")

def close_position(journal_file, ticker, expected_exit, actual_exit, exit_slippage, strategy="STANDARD"):
    csv_file = _csv_path(journal_file)
    rows = _load_rows(csv_file)

    # Find the last OPEN BUY for this ticker + strategy
    target_idx = None
    for i in reversed(range(len(rows))):
        row = rows[i]
        if (row.get("Ticker") == ticker
                and row.get("Status") == "OPEN"
                and row.get("Side") == "BUY"
                and row.get("Strategy", "STANDARD") == strategy.upper()):
            target_idx = i
            break

    if target_idx is None:
        # Fallback: match without strategy
        for i in reversed(range(len(rows))):
            row = rows[i]
            if row.get("Ticker") == ticker and row.get("Status") == "OPEN" and row.get("Side") == "BUY":
                target_idx = i
                break

    if target_idx is None:
        return  # No open position found

    try:
        entry_price = float(rows[target_idx].get("Execution_Price") or rows[target_idx].get("Expected_Price") or 0)
        qty = float(rows[target_idx].get("Quantity", 1))
    except (ValueError, TypeError):
        entry_price = 0
        qty = 1

    pnl = round((actual_exit - entry_price) * qty, 4)

    rows[target_idx]["Exit_Price"] = actual_exit
    rows[target_idx]["Exit_Slippage"] = exit_slippage
    rows[target_idx]["PnL"] = pnl
    rows[target_idx]["Status"] = "CLOSED"

    _save_rows(csv_file, rows)
    print(f"[LOGGER] Closed {ticker} in {os.path.basename(csv_file)}: Exit @ {actual_exit} (Slip: {exit_slippage}) | PnL: ${pnl:.2f}")

def generate_weekly_report(journal_file):
    csv_file = _csv_path(journal_file)
    rows = _load_rows(csv_file)

    closed = [r for r in rows if r.get("Status") == "CLOSED"]
    total = len(closed)

    if total == 0:
        print("No closed trades to report yet.")
        return

    winning = [r for r in closed if float(r.get("PnL") or 0) > 0]
    win_rate = (len(winning) / total) * 100
    total_pnl = sum(float(r.get("PnL") or 0) for r in closed)

    print(f"\n--- WEEKLY ALGO REPORT ({os.path.basename(csv_file)}) ---")
    print(f"  Total Closed Trades : {total}")
    print(f"  Winning Trades      : {len(winning)}")
    print(f"  Win Rate            : {round(win_rate, 2)}%")
    print(f"  Total PnL           : ${round(total_pnl, 2)}")
    print("------------------------------------\n")
