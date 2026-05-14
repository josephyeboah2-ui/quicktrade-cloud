import argparse
import json
import sys

try:
    from webull import webull
except ImportError:
    print(json.dumps({"error": "webull library not installed"}))
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rank_type', type=str, default='preMarket')
    parser.add_argument('--count', type=int, default=30)
    args = parser.parse_args()

    wb = webull()
    try:
        if 'all' in args.rank_type:
            rank_types = ['preMarket', '1d', 'afterMarket']
        else:
            rank_types = [rt.strip() for rt in args.rank_type.split(',') if rt.strip()]
        
        tickers = []
        seen = set()

        for rt in rank_types:
            gainers_resp = wb.active_gainer_loser('gainer', rank_type=rt, count=args.count)
            if gainers_resp and isinstance(gainers_resp.get('data'), list):
                for g in gainers_resp['data']:
                    symbol = g.get('ticker', {}).get('symbol')
                    if symbol and symbol not in seen:
                        tickers.append(symbol)
                        seen.add(symbol)
        
        print(json.dumps({"ok": True, "tickers": tickers}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))

if __name__ == "__main__":
    main()
