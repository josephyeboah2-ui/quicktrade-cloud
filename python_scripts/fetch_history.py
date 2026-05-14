import yfinance as yf
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument('--ticker', type=str, required=True)
parser.add_argument('--period', type=str, default="1mo")
args = parser.parse_known_args()[0]

period_map = {
    '1d': '5m',
    '5d': '15m',
    '1w': '15m',
    '1mo': '1d',
    '3mo': '1d',
    '6mo': '1d',
    '1y': '1d'
}
interval = period_map.get(args.period, '1d')

try:
    yf_period = '5d' if args.period == '1w' else args.period
    t_obj = yf.Ticker(args.ticker)
    df = t_obj.history(period=yf_period, interval=interval)
    if df.empty:
        print("===CHART_DATA===")
        print(json.dumps({"history": [], "quote": {}}))
        print("===END_CHART_DATA===")
    else:
        data = []
        for index, row in df.iterrows():
            if interval == '1d':
                time_str = str(index).split(' ')[0]
            else:
                time_str = str(index)[5:16]
            data.append({"time": time_str, "price": round(row['Close'], 2)})
            
        quote = {}
        try:
            info = t_obj.info
            quote = {
                "price": info.get('currentPrice') or info.get('regularMarketPrice') or data[-1]['price'],
                "changePct": info.get('regularMarketChangePercent', 0),
                "bid": info.get('bid', 0),
                "ask": info.get('ask', 0),
                "volume": info.get('volume', 0)
            }
        except:
            quote = {
                "price": data[-1]['price'],
                "changePct": 0, "bid": 0, "ask": 0, "volume": 0
            }
        
        print("===CHART_DATA===")
        print(json.dumps({"history": data, "quote": quote}))
        print("===END_CHART_DATA===")
except Exception as e:
    print("===CHART_DATA===")
    print(json.dumps({"history": [], "quote": {}}))
    print("===END_CHART_DATA===")
