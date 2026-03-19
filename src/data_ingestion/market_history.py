import os
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import logging

try:
    from kiteconnect import KiteConnect
except ImportError:
    KiteConnect = None

logger = logging.getLogger(__name__)

class HistoryStore:
    def __init__(self, db_path: str = "data/history.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ohlcv (
                    symbol TEXT,
                    interval TEXT,
                    timestamp TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    PRIMARY KEY (symbol, interval, timestamp)
                )
            """)

    def get(self, symbol: str, interval: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
        query = """
            SELECT timestamp, open, high, low, close, volume 
            FROM ohlcv 
            WHERE symbol = ? AND interval = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
        """
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query(
                query, 
                conn, 
                params=(symbol, interval, start.isoformat(), end.isoformat()),
                parse_dates=['timestamp']
            )
        
        if df.empty:
            return None
            
        df.set_index('timestamp', inplace=True)
        return df

    def save(self, symbol: str, interval: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        
        records = []
        for ts, row in df.iterrows():
            ts_iso = ts.isoformat() if isinstance(ts, datetime) else str(ts)
            records.append((
                symbol, interval, ts_iso,
                float(row['open']), float(row['high']), 
                float(row['low']), float(row['close']), int(row['volume'])
            ))
            
        query = """
            INSERT OR IGNORE INTO ohlcv (symbol, interval, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(query, records)
            
class KiteHistoryFetcher:
    def __init__(self, kite_client: "KiteConnect"):
        self.kite = kite_client

    def fetch(self, instrument_token: int, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        """
        Call Kite historical data API.
        NOTE: Kite uses instrument_token, not symbol string for historical data.
        """
        if not self.kite:
            raise ValueError("KiteConnect client is not initialized.")
            
        # Map VoltEdge standard intervals onto Kite's required structural API parameters
        k_interval = "minute"
        if interval == "5m": k_interval = "5minute"
        elif interval == "15m": k_interval = "15minute"
        elif interval == "60m": k_interval = "60minute"
        elif interval == "1d": k_interval = "day"
        
        try:
            records = self.kite.historical_data(instrument_token, start, end, k_interval)
            if not records:
                return pd.DataFrame()
                
            df = pd.DataFrame(records)
            df.rename(columns={'date': 'timestamp'}, inplace=True)
            df.set_index('timestamp', inplace=True)
            return df[['open', 'high', 'low', 'close', 'volume']]
        except Exception as e:
            logger.error(f"Kite API historical fetch failed for token {instrument_token}: {e}")
            raise

def get_ohlcv(symbol: str, instrument_token: int, interval: str, start: datetime, end: datetime, kite_client: Optional["KiteConnect"] = None, db_path: str = "data/history.db") -> pd.DataFrame:
    """
    Try HistoryStore first; if missing or partial, use KiteHistoryFetcher to backfill,
    then return a full DataFrame securely cached to local SQLite instances.
    """
    store = HistoryStore(db_path)
    df_cached = store.get(symbol, interval, start, end)
    
    # Advanced gap-filling logic could go here; for V1 we fall back to a full fetch if cache bounds are wildly missed.
    if df_cached is not None and not df_cached.empty:
        # Strip exact timezones for boundary comparisons
        first_cache = df_cached.index[0].tz_localize(None) if df_cached.index[0].tzinfo else df_cached.index[0]
        last_cache = df_cached.index[-1].tz_localize(None) if df_cached.index[-1].tzinfo else df_cached.index[-1]
        
        start_naive = start.tz_localize(None) if start.tzinfo else start
        end_naive = end.tz_localize(None) if end.tzinfo else end
        
        if first_cache <= start_naive and last_cache >= (end_naive - timedelta(days=1)):
            logger.info(f"Loaded {len(df_cached)} {interval} bars for {symbol} cleanly from SQLite Cache.")
            return df_cached

    logger.info(f"Cache miss or partial for {symbol}. Triggering external HTTP fetch from Kite API...")
    if not kite_client:
        logger.warning("No KiteClient provided, returning whatever partial cache exists.")
        return df_cached if df_cached is not None else pd.DataFrame()
        
    fetcher = KiteHistoryFetcher(kite_client)
    df_new = fetcher.fetch(instrument_token, interval, start, end)
    
    if not df_new.empty:
        store.save(symbol, interval, df_new)
        
    return df_new
