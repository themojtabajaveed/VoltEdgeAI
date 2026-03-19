from dataclasses import dataclass
from enum import Enum
from typing import Optional

from marketdata.intraday import VWAPStats, fetch_intraday_bars, compute_vwap_stats, IntradayBar
from data_ingestion.intraday_context import get_intraday_bars_for_symbol
from data_ingestion.market_history import get_ohlcv
from config.zerodha import load_zerodha_config
from data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
from datetime import datetime
import pandas as pd
import logging

logger = logging.getLogger(__name__)

try:
    from kiteconnect import KiteConnect
    _df_inst = load_instruments_csv()
    _symbol_map = build_symbol_token_map(_df_inst)
except Exception:
    KiteConnect = None
    _symbol_map = {}

_kite_client = None
def _get_kite_client():
    global _kite_client
    if _kite_client is None and KiteConnect:
        cfg = load_zerodha_config()
        if cfg.access_token:
            _kite_client = KiteConnect(api_key=cfg.api_key)
            _kite_client.set_access_token(cfg.access_token)
    return _kite_client

def _get_today_start() -> datetime:
    tz = datetime.now().astimezone().tzinfo
    return datetime.now(tz).replace(hour=9, minute=15, second=0, microsecond=0)

def fetch_intraday_history_fallback(symbol: str) -> list[IntradayBar]:
    """Fetch today's history from Kite (market_history), fallback to yfinance."""
    today_start = _get_today_start()
    now = datetime.now(today_start.tzinfo)
    
    token = _symbol_map.get(symbol)
    if token:
        try:
            kc = _get_kite_client()
            df = get_ohlcv(symbol, token, "1m", today_start, now, kite_client=kc)
            if df is not None and not df.empty:
                bars = []
                for ts, row in df.iterrows():
                    bars.append(IntradayBar(
                        timestamp=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                        open=float(row['open']),
                        high=float(row['high']),
                        low=float(row['low']),
                        close=float(row['close']),
                        volume=float(row['volume'])
                    ))
                return bars
        except Exception as e:
            logger.warning(f"Kite history fetch failed for {symbol}: {e}. Falling back to yfinance.")
            
    # Fallback to yfinance
    return fetch_intraday_bars(symbol, interval="1m")

class AntigravityStatus(str, Enum):
    IMMEDIATE_BUY_ALLOWED = "IMMEDIATE_BUY_ALLOWED"
    WAITING_FOR_GRAVITY = "WAITING_FOR_GRAVITY"
    BEAR_CONTROL = "BEAR_CONTROL"
    NO_DATA = "NO_DATA"

@dataclass
class AntigravityDecision:
    status: AntigravityStatus
    z_score: Optional[float]
    vwap: Optional[float]
    ltp: Optional[float]
    sigma_vwap: Optional[float]
    reason: str

def evaluate_antigravity(vwap_stats: Optional[VWAPStats]) -> AntigravityDecision:
    """
    Apply the Antigravity distance check based on VWAPStats.
    - If no data or sigma_vwap ~ 0, return NO_DATA with a clear reason.
    - If z_score > 2: WAITING_FOR_GRAVITY.
    - Else if ltp < vwap: BEAR_CONTROL.
    - Else: IMMEDIATE_BUY_ALLOWED.
    """
    if not vwap_stats or vwap_stats.z_score is None or vwap_stats.sigma_vwap is None or abs(vwap_stats.sigma_vwap) < 1e-6:
        return AntigravityDecision(
            status=AntigravityStatus.NO_DATA,
            z_score=None,
            vwap=None,
            ltp=None,
            sigma_vwap=None,
            reason="VWAP stats are missing or incomplete."
        )

    z = vwap_stats.z_score
    ltp = vwap_stats.ltp
    vwap = vwap_stats.vwap

    if z > 2.0:
        return AntigravityDecision(
            status=AntigravityStatus.WAITING_FOR_GRAVITY,
            z_score=z,
            vwap=vwap,
            ltp=ltp,
            sigma_vwap=vwap_stats.sigma_vwap,
            reason=f"Price is stretched too far above VWAP (Z={z:.2f}). Wait for snapback."
        )
    elif ltp < vwap:
        return AntigravityDecision(
            status=AntigravityStatus.BEAR_CONTROL,
            z_score=z,
            vwap=vwap,
            ltp=ltp,
            sigma_vwap=vwap_stats.sigma_vwap,
            reason=f"Price ({ltp:.2f}) is below VWAP ({vwap:.2f}). Bears are in control."
        )
    else:
        return AntigravityDecision(
            status=AntigravityStatus.IMMEDIATE_BUY_ALLOWED,
            z_score=z,
            vwap=vwap,
            ltp=ltp,
            sigma_vwap=vwap_stats.sigma_vwap,
            reason=f"Price is in the acceptable zone above VWAP (Z={z:.2f})."
        )

def evaluate_symbol(symbol: str) -> AntigravityDecision:
    """
    Fetch live intraday bars mapped over historical backfills, compute VWAPStats, and return Decision.
    """
    try:
        # 1. Fetch historical from Kite DB/API, breaking into yfinance if failing
        history_bars = fetch_intraday_history_fallback(symbol)
        
        # 2. Fetch lightning fast live bars from current stream memory
        live_bars = get_intraday_bars_for_symbol(symbol, lookback_minutes=400) # Entire day's max capacity
        
        # 3. Stitch them seamlessly by timestamp overriding matching historical records with live actuals
        merged_dict = {b.timestamp: b for b in history_bars}
        
        for lb in live_bars:
            merged_dict[lb.start] = IntradayBar(
                timestamp=lb.start,
                open=float(lb.open),
                high=float(lb.high),
                low=float(lb.low),
                close=float(lb.close),
                volume=float(lb.volume)
            )
            
        intraday_bars = [merged_dict[ts] for ts in sorted(merged_dict.keys())]
        
        if not intraday_bars:
            return AntigravityDecision(
                status=AntigravityStatus.NO_DATA,
                z_score=None,
                vwap=None,
                ltp=None,
                sigma_vwap=None,
                reason=f"Failed to fetch sufficient intraday data (historical or live) for {symbol}."
            )
            
        stats = compute_vwap_stats(intraday_bars, interval="1m")
        return evaluate_antigravity(stats)
    except Exception as e:
        return AntigravityDecision(
            status=AntigravityStatus.NO_DATA,
            z_score=None,
            vwap=None,
            ltp=None,
            sigma_vwap=None,
            reason=f"Error evaluating antigravity for {symbol}: {str(e)}"
        )
