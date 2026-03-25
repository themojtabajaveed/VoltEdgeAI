"""
nse_prices.py — Daily OHLCV + Technical Indicators
----------------------------------------------------
Provides daily OHLCV data via Kite Historical API (with SQLite cache).
Also provides pure-math technical indicator functions used across the system.

Previously used yfinance — fully replaced by Kite API as of Phase H.
"""
from datetime import datetime, timedelta
from typing import Optional
import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)


def fetch_daily_ohlcv(symbol: str, days: int = 250) -> Optional[pd.DataFrame]:
    """
    Fetch the last `days` daily OHLCV candles for the given NSE symbol via Kite API.
    Uses market_history.py (SQLite cache + Kite backfill) for efficient data retrieval.
    
    Returns a DataFrame indexed by date with columns: ['open', 'high', 'low', 'close', 'volume'],
    or None if data cannot be fetched.
    """
    try:
        from src.data_ingestion.market_history import get_ohlcv
        from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
        from src.config.zerodha import load_zerodha_config

        # Resolve instrument token
        df_inst = load_instruments_csv()
        token_map = build_symbol_token_map(df_inst)
        token = token_map.get(symbol)
        if not token:
            logger.warning(f"No instrument token found for {symbol}")
            return None

        # Build Kite client
        kite_client = None
        try:
            from kiteconnect import KiteConnect
            cfg = load_zerodha_config()
            if cfg.access_token:
                kite_client = KiteConnect(api_key=cfg.api_key)
                kite_client.set_access_token(cfg.access_token)
        except Exception as e:
            logger.warning(f"Could not init Kite client: {e}")

        # Fetch data (cache-first, then Kite API backfill)
        end = datetime.now()
        start = end - timedelta(days=days * 2)  # fetch extra to ensure we have enough after weekends/holidays
        
        df = get_ohlcv(symbol, token, "1d", start, end, kite_client=kite_client)
        
        if df is None or df.empty:
            return None

        # Standardize columns (Kite returns lowercase already from market_history)
        required = ['open', 'high', 'low', 'close', 'volume']
        available = [c for c in required if c in df.columns]
        if len(available) < 5:
            return None

        df = df[available].copy()
        for col in available:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df.sort_index(ascending=True)
        return df.tail(days)

    except Exception as e:
        logger.error(f"fetch_daily_ohlcv failed for {symbol}: {e}")
        return None


# ── Pure Math Indicators (no external data dependency) ────────────────────

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
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    latest_gain = avg_gain.iloc[-1]
    latest_loss = avg_loss.iloc[-1]
    if pd.isna(latest_gain) or pd.isna(latest_loss):
        return None
    if latest_loss == 0:
        return 100.0
    rs = latest_gain / latest_loss
    return float(100 - (100 / (1 + rs)))


def compute_avg_volume(volume_series: pd.Series, period: int = 20) -> Optional[float]:
    """Return the average volume over the last `period` bars, or None if not enough data."""
    if volume_series is None or len(volume_series) < period:
        return None
    val = volume_series.tail(period).mean()
    if pd.isna(val):
        return None
    return float(val)


def compute_atr(high_series: pd.Series, low_series: pd.Series, close_series: pd.Series, period: int = 14) -> Optional[float]:
    """Compute the Average True Range (ATR) over the last `period` bars."""
    if len(high_series) < period + 1 or len(low_series) < period + 1 or len(close_series) < period + 1:
        return None
    close_prev = close_series.shift(1)
    high_low = high_series - low_series
    high_close_prev = (high_series - close_prev).abs()
    low_close_prev = (low_series - close_prev).abs()
    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    if pd.isna(atr):
        return None
    return float(atr)
