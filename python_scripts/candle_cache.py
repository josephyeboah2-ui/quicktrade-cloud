import sqlite3
import pandas as pd
import yfinance as yf
import os
import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "candles_cache.sqlite")

def _init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS minute_candles (
            ticker TEXT,
            timestamp TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            PRIMARY KEY (ticker, timestamp)
        )
    """)
    conn.commit()
    conn.close()

_init_db()

def get_historical_data(ticker, period="30d", interval="5m"):
    """
    Attempts to fetch from local SQLite cache first.
    If the data is older than today or missing, it downloads from yfinance,
    caches it locally, and then returns the dataframe.
    """
    conn = sqlite3.connect(DB_PATH)
    
    # Try fetching from cache
    query = f"SELECT * FROM minute_candles WHERE ticker = '{ticker}' ORDER BY timestamp ASC"
    df = pd.read_sql_query(query, conn)
    
    if len(df) > 100:
        # We have a robust cache, let's use it
        df['Datetime'] = pd.to_datetime(df['timestamp'])
        df.set_index('Datetime', inplace=True)
        # Verify if the cache is recent enough (contains data from the last 2 days)
        last_date = df.index[-1]
        if (datetime.datetime.now(datetime.timezone.utc) - last_date).days <= 2:
            conn.close()
            print(f"[CACHE HIT] Loaded {len(df)} candles for {ticker} from local SQLite!")
            return df
            
    # Cache miss or stale data, fetch from yfinance
    print(f"[CACHE MISS] Downloading {ticker} from Yahoo Finance...")
    try:
        data = yf.download(ticker, period=period, interval=interval, progress=False)
        if data.empty:
            conn.close()
            return pd.DataFrame()
            
        # Clean up and save to SQLite
        data.reset_index(inplace=True)
        data_to_save = data.copy()
        
        # yfinance multi-index columns fix if present
        if isinstance(data_to_save.columns, pd.MultiIndex):
            data_to_save.columns = [col[0] for col in data_to_save.columns]
            
        # Standardize column names
        col_mapping = {}
        for col in data_to_save.columns:
            if 'open' in col.lower(): col_mapping[col] = 'open'
            if 'high' in col.lower(): col_mapping[col] = 'high'
            if 'low' in col.lower(): col_mapping[col] = 'low'
            if 'close' in col.lower(): col_mapping[col] = 'close'
            if 'volume' in col.lower(): col_mapping[col] = 'volume'
            if 'date' in col.lower() or 'time' in col.lower(): col_mapping[col] = 'timestamp'
            
        data_to_save.rename(columns=col_mapping, inplace=True)
        data_to_save['ticker'] = ticker
        data_to_save['timestamp'] = data_to_save['timestamp'].astype(str)
        
        # Save to DB
        columns_to_keep = ['ticker', 'timestamp', 'open', 'high', 'low', 'close', 'volume']
        # Filter only existing columns
        columns_to_keep = [c for c in columns_to_keep if c in data_to_save.columns]
        
        data_to_save[columns_to_keep].to_sql('minute_candles', conn, if_exists='append', index=False, method='multi')
        print(f"?? Cached {len(data_to_save)} new candles for {ticker} into SQLite.")
        
        conn.close()
        
        data.set_index(data.columns[0], inplace=True)
        return data
        
    except Exception as e:
        print(f"Error fetching/caching {ticker}: {e}")
        conn.close()
        return pd.DataFrame()
