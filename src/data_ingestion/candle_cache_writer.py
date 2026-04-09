"""
candle_cache_writer.py — Fetch 5-minute historical candles for the TimesFM
watchlist and write them into VoltEdgeAI's shared cache.

Designed to be called:
  • Once at runner.py startup (after WebSocket is live)
  • Periodically every 15 minutes during market hours

Key design decisions:
  • Re-reads access token on EVERY call (requirement: token may be refreshed
    between calls by the daily auto-login routine).
  • Per-symbol try/except — one failure must not abort others.
  • Writes keys "SYMBOL:5minute" matching what timesfm_dryrun/data_fetcher.py reads.
  • Also writes LTP so outcome_tracker can evaluate predictions at 15:45 IST.
"""

import logging
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_MODULE_ROOT = Path(__file__).resolve().parents[2]   # ~/VoltEdgeAI/
sys.path.insert(0, str(_MODULE_ROOT))

# Load .env if not already loaded (defensive — main.py does this; handles standalone calls)
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(dotenv_path=_MODULE_ROOT / ".env", override=False)
except ImportError:
    pass

from kiteconnect import KiteConnect

from cache.data_cache import CacheManager
from src.config.zerodha import load_zerodha_config
from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Symbols that TimesFM dry-run needs — must mirror timesfm_dryrun/config.py
_TIMESFM_WATCHLIST = ["RELIANCE", "INFY", "HDFCBANK", "TCS", "ICICIBANK"]

# Hard-coded NSE index instrument tokens (stable, never change)
_INDEX_TOKENS: dict[str, int] = {
    "NIFTY 50":   256265,
    "NIFTY BANK": 260105,
}

_CANDLE_INTERVAL = "5minute"   # matches timesfm_dryrun/config.py CANDLE_INTERVAL
_LOOKBACK_DAYS   = 12          # enough to collect 200+ 5-min bars on trading days


def _build_kite() -> KiteConnect:
    """
    Create a fresh KiteConnect client each call by re-reading the current
    access token from load_zerodha_config().  This is intentional — the token
    may have been renewed by the daily auto-login while the process is running.
    """
    cfg = load_zerodha_config()
    if not cfg.access_token:
        raise ValueError("ZERODHA_ACCESS_TOKEN is not set — cannot build KiteConnect client")
    kite = KiteConnect(api_key=cfg.api_key)
    kite.set_access_token(cfg.access_token)
    return kite


def _fetch_5min_candles(kite: KiteConnect, token: int, symbol: str) -> list[dict] | None:
    """
    Fetch up to 200 5-minute candles for *symbol* via KiteConnect REST API.
    Returns a list of dicts {"date", "open", "high", "low", "close", "volume"}
    or None on failure.
    """
    try:
        to_dt   = datetime.now(IST).replace(tzinfo=None)
        from_dt = (datetime.now(IST) - timedelta(days=_LOOKBACK_DAYS)).replace(tzinfo=None)

        raw = kite.historical_data(
            instrument_token=token,
            from_date=from_dt,
            to_date=to_dt,
            interval=_CANDLE_INTERVAL,
            continuous=False,
        )
        if not raw:
            logger.warning("[CacheWriter] No candles returned for %s", symbol)
            return None

        # KiteConnect returns a list of dicts already; normalise date field
        candles = []
        for bar in raw[-200:]:   # keep last 200
            candles.append({
                "date":   bar["date"].isoformat() if hasattr(bar["date"], "isoformat") else str(bar["date"]),
                "open":   float(bar["open"]),
                "high":   float(bar["high"]),
                "low":    float(bar["low"]),
                "close":  float(bar["close"]),
                "volume": int(bar["volume"]),
            })
        return candles

    except Exception as exc:  # noqa: BLE001
        logger.error("[CacheWriter] Failed to fetch candles for %s (token=%d): %s", symbol, token, exc)
        return None


def write_candle_cache() -> None:
    """
    Main entry point.  Fetches 5-min candles for all TimesFM symbols and
    writes them to ~/VoltEdgeAI/cache/candles_cache.json.
    Also writes LTP from the last candle close for each symbol.

    Safe to call in a try/except from runner.py — never raises.
    """
    try:
        kite = _build_kite()
    except Exception as exc:  # noqa: BLE001
        logger.error("[CacheWriter] Cannot build KiteConnect client: %s", exc)
        return

    # Build equity symbol → token map from instruments CSV
    try:
        df = load_instruments_csv()
        full_map: dict[str, int] = build_symbol_token_map(df)
    except Exception as exc:  # noqa: BLE001
        logger.error("[CacheWriter] Could not load instruments CSV: %s", exc)
        full_map = {}

    cm = CacheManager()
    written = 0
    total   = len(_TIMESFM_WATCHLIST) + len(_INDEX_TOKENS)

    # ── Equities ──────────────────────────────────────────────────────────────
    for symbol in _TIMESFM_WATCHLIST:
        try:
            token = full_map.get(symbol)
            if not token:
                logger.warning("[CacheWriter] No instrument token found for %s — skipping", symbol)
                continue

            candles = _fetch_5min_candles(kite, token, symbol)
            if not candles:
                continue

            cm.write_candles(symbol, _CANDLE_INTERVAL, candles)
            cm.write_ltp(symbol, candles[-1]["close"])
            written += 1
            logger.info(
                "[CacheWriter] %s — wrote %d 5-min candles, last_close=%.2f",
                symbol, len(candles), candles[-1]["close"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[CacheWriter] Unexpected error for %s: %s", symbol, exc)

    # ── Indices ───────────────────────────────────────────────────────────────
    for symbol, token in _INDEX_TOKENS.items():
        try:
            candles = _fetch_5min_candles(kite, token, symbol)
            if not candles:
                continue

            cm.write_candles(symbol, _CANDLE_INTERVAL, candles)
            cm.write_ltp(symbol, candles[-1]["close"])
            written += 1
            logger.info(
                "[CacheWriter] %s — wrote %d 5-min candles, last_close=%.2f",
                symbol, len(candles), candles[-1]["close"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[CacheWriter] Unexpected error for %s: %s", symbol, exc)

    logger.info(
        "[CacheWriter] Done — wrote 5-min candles for %d/%d symbols to ~/VoltEdgeAI/cache/",
        written, total,
    )
