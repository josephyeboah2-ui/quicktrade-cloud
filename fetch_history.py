import sys
import pandas as pd
import json
import os

def read_history(filename):
    if not os.path.exists(filename):
        return []
    try:
        df = pd.read_excel(filename)
        return json.loads(df.to_json(orient='records', date_format='iso'))
    except Exception as e:
        return []

if __name__ == "__main__":
    paper = read_history('PaperTrade_Journal.xlsx')
    live = read_history('LiveTrade_Journal.xlsx')
    
    for t in paper:
        t['isBot'] = True
        t['type'] = 'PAPER'
    
    for t in live:
        t['isBot'] = True
        t['type'] = 'LIVE'
        
    print(json.dumps(paper + live))
