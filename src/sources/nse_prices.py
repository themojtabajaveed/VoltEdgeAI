from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

def fetch_daily_ohlcv(symbol: str, days: int = 250) -> Optional[pd.DataFrame]:
    """
    Fetch the last `days` daily OHLCV candles for the given NSE symbol using Yahoo Finance.
    - Symbol "ESAFSFB" should be mapped to ticker "ESAFSFB.NS".
    - Returns a DataFrame indexed by date with columns: ['open', 'high', 'low', 'close', 'volume'].
    - If data cannot be fetched or is empty, return None.
    """
    end = datetime.utcnow()
    start = end - timedelta(days=days * 2)
    
    ticker = f"{symbol}.NS"
    try:
        # Fetch data without printing progress bars to keep console output clean
        df = yf.download(ticker, start=start, end=end, interval="1d", auto_adjust=False, progress=False)
    except Exception as e:
        print(f"Error fetching OHLCV for {symbol}: {e}")
        return None

    if df is None or df.empty:
        return None
        
    # Handle pandas MultiIndex columns that sometimes happen with newer yfinance versions
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
        
    rename_map = {
        'Open': 'open',
        'High': 'high',
        'Low': 'low',
        'Close': 'close',
        'Volume': 'volume'
    }
    
    # Rename columns to standardized lowercase
    df = df.rename(columns=rename_map)
    
    required_cols = ['open', 'high', 'low', 'close', 'volume']
    
    # Make sure we have the required columns
    available_cols = [c for c in required_cols if c in df.columns]
    if not available_cols:
        return None
        
    df = df[available_cols].copy()
    
    # Convert types to numeric
    for col in available_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    df = df.sort_index(ascending=True)
    return df.tail(days)

def compute_ema(close_series: pd.Series, period: int) -> Optional[float]:
    """Return the latest EMA value for the given period, or None if not enough data."""
    if close_series is None or len(close_series) < period:
        return None
    
    val = close_series.ewm(span=period, adjust=False).mean().iloc[-1]
    if pd.isna(val):
        return None
    return float(val)

def compute_rsi(close_series: pd.Series, period: int = 14) -> Optional[float]:
    """Return the latest RSI value, or None if not enough data."""
    if close_series is None or len(close_series) < period + 1:
        return None
        
    delta = close_series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    # Simple rolling mean approach for standard gain/loss
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    
    latest_gain = avg_gain.iloc[-1]
    latest_loss = avg_loss.iloc[-1]
    
    if pd.isna(latest_gain) or pd.isna(latest_loss):
        return None
        
    if latest_loss == 0:
        return 100.0  # If there are no losses, RSI is 100
        
    rs = latest_gain / latest_loss
    rsi = 100 - (100 / (1 + rs))
    
    return float(rsi)

def compute_avg_volume(volume_series: pd.Series, period: int = 20) -> Optional[float]:
    """Return the average volume over the last `period` bars, or None if not enough data."""
    if volume_series is None or len(volume_series) < period:
        return None
        
    val = volume_series.tail(period).mean()
    if pd.isna(val):
        return None
    return float(val)

def compute_atr(high_series: pd.Series, low_series: pd.Series, close_series: pd.Series, period: int = 14) -> Optional[float]:
    """
    Compute the Average True Range (ATR) over the last `period` bars.
    Returns the latest ATR value, or None if there is not enough data.
    """
    if len(high_series) < period + 1 or len(low_series) < period + 1 or len(close_series) < period + 1:
        return None
        
    close_prev = close_series.shift(1)
    
    high_low = high_series - low_series
    high_close_prev = (high_series - close_prev).abs()
    low_close_prev = (low_series - close_prev).abs()
    
    # Compute True Range as element-wise maximum
    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
    
    # Simple rolling mean of the True Range over the period
    atr = tr.rolling(window=period).mean().iloc[-1]
    
    if pd.isna(atr):
        return None
        
    return float(atr)
