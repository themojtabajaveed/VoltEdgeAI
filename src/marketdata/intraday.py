from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional

import pandas as pd
import numpy as np
import yfinance as yf

BarInterval = Literal["1m", "5m"]

@dataclass
class IntradayBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class VWAPStats:
    vwap: float
    sigma_vwap: float
    z_score: float
    ltp: float
    interval: BarInterval
    n_bars: int

def compute_vwap_stats(bars: List[IntradayBar], interval: BarInterval = "1m") -> VWAPStats:
    """
    Given intraday bars for the current day (sorted by time), compute:
    - VWAP over all bars
    - Volume-weighted intraday standard deviation around VWAP (σ_VWAP)
    - LTP (last bar close)
    - Z-score = (LTP - VWAP) / σ_VWAP
    If there are too few bars or σ_VWAP is effectively zero, handle gracefully (e.g., z_score = 0.0).
    """
    if not bars:
        return VWAPStats(0.0, 0.0, 0.0, 0.0, interval, 0)
        
    df = pd.DataFrame([vars(b) for b in bars])
    
    # Typical price
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3.0
    
    # VWAP = cumulative(volume * typical_price) / cumulative(volume)
    df['pv'] = df['typical_price'] * df['volume']
    df['cum_pv'] = df['pv'].cumsum()
    df['cum_volume'] = df['volume'].cumsum()
    
    # Replace 0 volume with NaN to avoid division by zero
    cum_vol_safe = df['cum_volume'].replace(0, np.nan)
    df['vwap'] = df['cum_pv'] / cum_vol_safe
    
    # Volume-weighted variance
    # sigma^2 = cumulative(volume * (typical_price - VWAP)^2) / cumulative(volume)
    df['dev_sq'] = (df['typical_price'] - df['vwap']) ** 2
    df['vol_dev_sq'] = df['volume'] * df['dev_sq']
    df['cum_vol_dev_sq'] = df['vol_dev_sq'].cumsum()
    
    df['vwap_variance'] = df['cum_vol_dev_sq'] / cum_vol_safe
    df['sigma_vwap'] = np.sqrt(df['vwap_variance'])
    
    # Get the latest values
    latest = df.iloc[-1]
    
    ltp = float(latest['close'])
    vwap = float(latest['vwap'])
    sigma_vwap = float(latest['sigma_vwap'])
    
    # Z-score = (LTP - VWAP) / sigma_vwap
    z_score = 0.0
    if sigma_vwap > 0 and not pd.isna(sigma_vwap):
        z_score = (ltp - vwap) / sigma_vwap
        
    return VWAPStats(
        vwap=vwap,
        sigma_vwap=sigma_vwap,
        z_score=z_score,
        ltp=ltp,
        interval=interval,
        n_bars=len(df)
    )

def fetch_intraday_bars(symbol: str, interval: BarInterval = "1m") -> List[IntradayBar]:
    """
    Fetch today's intraday bars for `symbol` from market open until now, in the given interval.
    Assumes NSE symbols are suffixed with '.NS' for yfinance.
    """
    yf_symbol = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
    
    # yfinance valid intervals for intraday: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo
    df = yf.download(tickers=yf_symbol, period="1d", interval=interval, progress=False, auto_adjust=False)
    
    if df.empty:
        return []
        
    bars = []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
        
    for index, row in df.iterrows():
        open_val = row['Open'].item() if isinstance(row['Open'], pd.Series) else row['Open']
        high_val = row['High'].item() if isinstance(row['High'], pd.Series) else row['High']
        low_val = row['Low'].item() if isinstance(row['Low'], pd.Series) else row['Low']
        close_val = row['Close'].item() if isinstance(row['Close'], pd.Series) else row['Close']
        vol_val = row['Volume'].item() if isinstance(row['Volume'], pd.Series) else row['Volume']
        
        bars.append(IntradayBar(
            timestamp=index,
            open=float(open_val),
            high=float(high_val),
            low=float(low_val),
            close=float(close_val),
            volume=float(vol_val)
        ))
        
    return bars

if __name__ == "__main__":
    test_symbol = "RELIANCE"
    print(f"Testing intraday VWAP fetch for {test_symbol}...")
    bars_1m = fetch_intraday_bars(test_symbol, interval="1m")
    print(f"Fetched {len(bars_1m)} 1m bars.")
    
    if bars_1m:
        stats = compute_vwap_stats(bars_1m, interval="1m")
        if stats:
            print("\nLatest 1m VWAP Stats:")
            print(f"  LTP:        {stats.ltp:.2f}")
            print(f"  VWAP:       {stats.vwap:.2f}")
            print(f"  Sigma VWAP: {stats.sigma_vwap:.2f}")
            print(f"  Z-Score:    {stats.z_score:.2f}")
            
    bars_5m = fetch_intraday_bars(test_symbol, interval="5m")
    print(f"\nFetched {len(bars_5m)} 5m bars.")
    
    if bars_5m:
        stats_5m = compute_vwap_stats(bars_5m, interval="5m")
        if stats_5m:
            print("\nLatest 5m VWAP Stats:")
            print(f"  LTP:        {stats_5m.ltp:.2f}")
            print(f"  VWAP:       {stats_5m.vwap:.2f}")
            print(f"  Sigma VWAP: {stats_5m.sigma_vwap:.2f}")
            print(f"  Z-Score:    {stats_5m.z_score:.2f}")
