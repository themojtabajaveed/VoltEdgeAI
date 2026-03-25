"""
intraday.py — Intraday Bar Models + VWAP Engine
-------------------------------------------------
Provides IntradayBar and VWAPStats data models, VWAP computation,
and intraday bar fetching via Kite Historical API.

Previously used yfinance — fully replaced by Kite API as of Phase H.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Literal, Optional
import logging

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

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

    cum_vol_safe = df['cum_volume'].replace(0, np.nan)
    df['vwap'] = df['cum_pv'] / cum_vol_safe

    # Volume-weighted variance
    df['dev_sq'] = (df['typical_price'] - df['vwap']) ** 2
    df['vol_dev_sq'] = df['volume'] * df['dev_sq']
    df['cum_vol_dev_sq'] = df['vol_dev_sq'].cumsum()
    df['vwap_variance'] = df['cum_vol_dev_sq'] / cum_vol_safe
    df['sigma_vwap'] = np.sqrt(df['vwap_variance'])

    latest = df.iloc[-1]
    ltp = float(latest['close'])
    vwap = float(latest['vwap'])
    sigma_vwap = float(latest['sigma_vwap'])

    z_score = 0.0
    if sigma_vwap > 0 and not pd.isna(sigma_vwap):
        z_score = (ltp - vwap) / sigma_vwap

    return VWAPStats(
        vwap=vwap, sigma_vwap=sigma_vwap, z_score=z_score,
        ltp=ltp, interval=interval, n_bars=len(df),
    )


def fetch_intraday_bars(symbol: str, interval: BarInterval = "1m") -> List[IntradayBar]:
    """
    Fetch today's intraday bars for `symbol` from Kite Historical API.
    Returns a list of IntradayBar objects sorted by timestamp.
    """
    try:
        from src.data_ingestion.market_history import get_ohlcv
        from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
        from src.config.zerodha import load_zerodha_config

        df_inst = load_instruments_csv()
        token_map = build_symbol_token_map(df_inst)
        token = token_map.get(symbol)
        if not token:
            logger.warning(f"No instrument token for {symbol}")
            return []

        kite_client = None
        try:
            from kiteconnect import KiteConnect
            cfg = load_zerodha_config()
            if cfg.access_token:
                kite_client = KiteConnect(api_key=cfg.api_key)
                kite_client.set_access_token(cfg.access_token)
        except Exception:
            pass

        # Map interval
        kite_interval = "minute" if interval == "1m" else "5minute"
        
        now = datetime.now()
        today_start = now.replace(hour=9, minute=15, second=0, microsecond=0)

        df = get_ohlcv(symbol, token, interval, today_start, now, kite_client=kite_client)

        if df is None or df.empty:
            return []

        bars = []
        for ts, row in df.iterrows():
            bars.append(IntradayBar(
                timestamp=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                open=float(row['open']),
                high=float(row['high']),
                low=float(row['low']),
                close=float(row['close']),
                volume=float(row['volume']),
            ))
        return bars

    except Exception as e:
        logger.error(f"fetch_intraday_bars failed for {symbol}: {e}")
        return []
