"""
data_loader.py — Backtest OHLCV fetcher
----------------------------------------
Loads historical daily candles from the Zerodha KiteConnect SQLite cache
(or live API if the cache is cold) for the backtest universe.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def get_backtest_universe() -> list[str]:
    """Return the same universe used by the live scanner."""
    from src.data_ingestion.pre_market_data import get_scan_universe
    return get_scan_universe()


def fetch_historical_ohlcv(symbol: str, from_date: str, to_date: str) -> list[dict]:
    """Fetch daily OHLCV for *symbol* between *from_date* and *to_date* (YYYY-MM-DD).

    Tries the HistoryStore SQLite cache first; falls back to the KiteConnect API
    when a kite client is available.  Returns [] on any error — callers must tolerate
    missing data gracefully.
    """
    from src.data_ingestion.market_history import get_ohlcv

    # Resolve instrument token from instruments CSV; fall back to 0 (cache-only).
    instrument_token: int = 0
    try:
        from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
        df_inst = load_instruments_csv()
        token_map = build_symbol_token_map(df_inst)
        instrument_token = token_map.get(symbol, 0)
    except FileNotFoundError:
        pass  # Instruments CSV absent — proceed with cache-only (token=0).
    except Exception as e:
        logger.debug("[BACKTEST DATA] Token lookup for %s failed: %s", symbol, e)

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")

    try:
        df = get_ohlcv(symbol, instrument_token, "1d", start, end, kite_client=None)
        if df is None or df.empty:
            return []

        candles: list[dict] = []
        for ts, row in df.iterrows():
            date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            candles.append({
                "date": date_str,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            })
        return candles
    except Exception as e:
        logger.warning("[BACKTEST DATA] Failed to fetch %s: %s", symbol, e)
        return []


def fetch_universe_historical(from_date: str, to_date: str) -> dict[str, list[dict]]:
    """Fetch daily OHLCV for every symbol in the backtest universe.

    Throttles calls at the configured rate (default 0.35 s) to stay under the
    Kite 3-req/s limit.  Returns {symbol: [candles]} — empty list for failures.
    """
    from src.config_loader import get_backtest_throttle

    universe = get_backtest_universe()
    total = len(universe)
    throttle = get_backtest_throttle()
    result: dict[str, list[dict]] = {}

    for i, symbol in enumerate(universe, 1):
        logger.info("[BACKTEST DATA] Fetching %d/%d: %s", i, total, symbol)
        result[symbol] = fetch_historical_ohlcv(symbol, from_date, to_date)
        time.sleep(throttle)

    return result
