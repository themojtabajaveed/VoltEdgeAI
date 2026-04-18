"""
data_loader.py — Backtest OHLCV fetcher
----------------------------------------
Loads historical daily candles from the Zerodha KiteConnect SQLite cache
(or live API if an authenticated Kite client is available) for the backtest
universe.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")

# Module-level flag so the "no Kite auth" warning only fires once per process.
_NO_AUTH_WARNED = False


def get_kite_client() -> Optional[Any]:
    """Return an authenticated KiteConnect client, or None if auth is unavailable.

    Fast path: reads existing token from .env, tests it via kite.profile().
    If the token is expired, auto-refreshes via auto_refresh_access_token().
    Degrades gracefully to None (SQLite cache-only) on any unrecoverable error.
    """
    global _NO_AUTH_WARNED
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_PATH)
    except Exception:
        pass  # dotenv is optional; env may already be loaded

    api_key = os.getenv("ZERODHA_API_KEY", "")
    access_token = os.getenv("ZERODHA_ACCESS_TOKEN", "")

    if not api_key or not access_token:
        if not _NO_AUTH_WARNED:
            logger.warning(
                "[BACKTEST] No Kite auth available — using SQLite cache only"
            )
            _NO_AUTH_WARNED = True
        return None

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        if not _NO_AUTH_WARNED:
            logger.warning("[BACKTEST] kiteconnect package not installed — cache-only mode")
            _NO_AUTH_WARNED = True
        return None

    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        kite.profile()  # lightweight call to verify token is still valid
        return kite
    except Exception as fast_path_err:
        # Determine if this is an auth failure (token expired/invalid)
        try:
            from kiteconnect import exceptions as _ke
            _is_auth = isinstance(fast_path_err, (_ke.TokenException, _ke.PermissionException))
        except Exception:
            _is_auth = any(
                kw in str(fast_path_err).lower()
                for kw in ("token", "expired", "invalid", "unauthorized", "permission")
            )

        if not _is_auth:
            logger.warning("[BACKTEST] KiteConnect init failed: %s — cache-only mode", fast_path_err)
            return None

        logger.info("[BACKTEST] Token expired — attempting auto-refresh via auto_login...")
        try:
            from src.tools.auto_login import auto_refresh_access_token
            new_token = auto_refresh_access_token(env_file=_ENV_PATH)
            if new_token:
                kite2 = KiteConnect(api_key=api_key)
                kite2.set_access_token(new_token)
                return kite2
            logger.warning("[BACKTEST] Auto-refresh failed — cache-only mode")
            return None
        except Exception as refresh_err:
            logger.warning("[BACKTEST] Auto-refresh error: %s — cache-only mode", refresh_err)
            return None


def get_backtest_universe() -> list[str]:
    """Return the same universe used by the live scanner."""
    from src.data_ingestion.pre_market_data import get_scan_universe
    return get_scan_universe()


def fetch_historical_ohlcv(
    symbol: str,
    from_date: str,
    to_date: str,
    kite_client: Optional[Any] = None,
) -> list[dict]:
    """Fetch daily OHLCV for *symbol* between *from_date* and *to_date* (YYYY-MM-DD).

    If *kite_client* is None, only the SQLite cache is consulted.  When provided,
    the existing get_ohlcv() utility transparently backfills missing ranges from
    the Kite API.  Returns [] on any error — callers must tolerate missing data.
    """
    from src.data_ingestion.market_history import get_ohlcv

    # Resolve instrument token from instruments CSV; fall back to 0 (cache-only).
    instrument_token: int = 0
    try:
        from src.data_ingestion.instruments import (
            build_symbol_token_map,
            load_instruments_csv,
        )
        df_inst = load_instruments_csv()
        token_map = build_symbol_token_map(df_inst)
        instrument_token = int(token_map.get(symbol, 0))
    except FileNotFoundError:
        pass  # Instruments CSV absent — proceed with cache-only (token=0).
    except Exception as e:
        logger.debug("[BACKTEST DATA] Token lookup for %s failed: %s", symbol, e)

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")

    try:
        df = get_ohlcv(symbol, instrument_token, "1d", start, end, kite_client=kite_client)
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
    Kite 3-req/s limit.  Reuses a single KiteClient across all symbols so auth
    happens once.  Returns {symbol: [candles]} — empty list for failures.
    """
    from src.config_loader import get_backtest_throttle

    universe = get_backtest_universe()
    total = len(universe)
    throttle = get_backtest_throttle()
    kite_client = get_kite_client()  # None if no auth — degrades to cache-only
    result: dict[str, list[dict]] = {}

    for i, symbol in enumerate(universe, 1):
        logger.info("[BACKTEST DATA] Fetching %d/%d: %s", i, total, symbol)
        result[symbol] = fetch_historical_ohlcv(symbol, from_date, to_date, kite_client=kite_client)
        time.sleep(throttle)

    return result


def warm_cache(from_date: str, to_date: str) -> int:
    """Pre-warm the SQLite history cache by fetching every universe symbol.

    Idempotent — safe to call repeatedly (SQLite INSERT OR IGNORE prevents dupes).
    Returns the count of symbols that ended up with ≥ 5 daily candles in cache.
    """
    universe = get_backtest_universe()
    total = len(universe)
    data = fetch_universe_historical(from_date, to_date)
    fetched = sum(1 for candles in data.values() if len(candles) >= 5)
    logger.info("[BACKTEST] Cache warm complete: %d/%d symbols fetched", fetched, total)
    return fetched
