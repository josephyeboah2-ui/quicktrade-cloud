import sys
from excel_logger import log_trade, close_position

def main():
    if len(sys.args) < 5:
        print("Usage: python manual_trade_bridge.py <side> <qty> <ticker> <price>")
        sys.exit(1)
        
    side = sys.args[1]
    qty = float(sys.args[2])
    ticker = sys.args[3]
    price = float(sys.args[4])
    
    if side.upper() == "BUY":
        log_trade(ticker, side, qty, price, "Manual UI Trade (SIM)")
    else:
        close_position(ticker, price)
        
if __name__ == "__main__":
    main()
