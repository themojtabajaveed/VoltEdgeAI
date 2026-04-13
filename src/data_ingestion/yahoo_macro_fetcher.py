"""
yahoo_macro_fetcher.py — Yahoo Finance Global Macro Data
---------------------------------------------------------
Primary data source for global market signals (SPY, QQQ, crude,
gold, DXY, USD/INR, VIX). Replaces Finnhub as primary source
because Yahoo is more reliable pre-market (no rate limit issues).

Writes result to cache/macro_cache.json with timestamp so the
brief pipeline can read without re-fetching.
"""
import json
import logging
import os
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Yahoo Finance symbols
_SYMBOLS = {
    "SPY": "spy",
    "QQQ": "qqq",
    "CL=F": "crude_wti",
    "BZ=F": "crude_brent",
    "GC=F": "gold",
    "DX-Y.NYB": "dxy",
    "INR=X": "usdinr",
    "^VIX": "us_vix",
    "^NSEI": "gift_nifty",
}

MACRO_CACHE_PATH = "cache/macro_cache.json"


def fetch_global_macro() -> dict:
    """
    Fetch global macro data from Yahoo Finance.

    Returns dict with pct change and absolute values. Never crashes —
    returns partial dict with None for failed symbols.
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")

    result: Dict[str, Optional[float]] = {
        "spy_pct": None,
        "qqq_pct": None,
        "crude_wti": None,
        "crude_wti_pct": None,
        "crude_brent": None,
        "crude_brent_pct": None,
        "gold": None,
        "gold_pct": None,
        "dxy": None,
        "dxy_pct": None,
        "usdinr": None,
        "us_vix": None,
        "gift_nifty": None,
        "fetched_at": datetime.now(IST).isoformat(),
        "source": "yahoo_finance",
    }

    try:
        import yfinance as yf
    except ImportError:
        logger.error("[YahooMacro] yfinance not installed. Run: pip install yfinance")
        return result

    tickers_str = " ".join(_SYMBOLS.keys())
    failed = []
    success_parts = []

    try:
        # Single batch download for efficiency
        data = yf.download(
            tickers=tickers_str,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        if data.empty:
            logger.warning("[YahooMacro] Download returned empty dataframe")
            return result

        for yahoo_sym, field_base in _SYMBOLS.items():
            try:
                # Extract close prices for this symbol
                if len(_SYMBOLS) > 1:
                    # Multi-ticker download: columns are MultiIndex
                    close_col = ("Close", yahoo_sym)
                    if close_col not in data.columns:
                        failed.append(yahoo_sym)
                        continue
                    closes = data[close_col].dropna()
                else:
                    closes = data["Close"].dropna()

                if len(closes) < 2:
                    failed.append(yahoo_sym)
                    continue

                latest = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])

                if prev <= 0:
                    failed.append(yahoo_sym)
                    continue

                pct_change = round(((latest - prev) / prev) * 100, 2)

                # Map to result dict
                if field_base == "spy":
                    result["spy_pct"] = pct_change
                    success_parts.append(f"SPY: {pct_change:+.2f}%")
                elif field_base == "qqq":
                    result["qqq_pct"] = pct_change
                    success_parts.append(f"QQQ: {pct_change:+.2f}%")
                elif field_base == "crude_wti":
                    result["crude_wti"] = round(latest, 2)
                    result["crude_wti_pct"] = pct_change
                    success_parts.append(f"Crude WTI: ${latest:.2f} ({pct_change:+.2f}%)")
                elif field_base == "crude_brent":
                    result["crude_brent"] = round(latest, 2)
                    result["crude_brent_pct"] = pct_change
                    success_parts.append(f"Brent: ${latest:.2f} ({pct_change:+.2f}%)")
                elif field_base == "gold":
                    result["gold"] = round(latest, 2)
                    result["gold_pct"] = pct_change
                    success_parts.append(f"Gold: ${latest:.2f} ({pct_change:+.2f}%)")
                elif field_base == "dxy":
                    result["dxy"] = round(latest, 2)
                    result["dxy_pct"] = pct_change
                    success_parts.append(f"DXY: {latest:.2f} ({pct_change:+.2f}%)")
                elif field_base == "usdinr":
                    result["usdinr"] = round(latest, 4)
                    success_parts.append(f"USD/INR: {latest:.4f}")
                elif field_base == "us_vix":
                    result["us_vix"] = round(latest, 2)
                    success_parts.append(f"US VIX: {latest:.2f}")
                elif field_base == "gift_nifty":
                    result["gift_nifty"] = round(latest, 2)
                    success_parts.append(f"Nifty: {latest:.2f}")

            except Exception as e:
                failed.append(f"{yahoo_sym} ({e})")

    except Exception as e:
        logger.error(f"[YahooMacro] Batch download failed: {e}")
        return result

    # Log results
    if success_parts:
        logger.info(f"[YahooMacro] {' | '.join(success_parts)}")
    if failed:
        logger.warning(f"[YahooMacro] FAILED symbols: {', '.join(failed)}")

    # Write to cache
    _write_cache(result)

    return result


def _write_cache(data: dict) -> None:
    """Write macro data to cache/macro_cache.json."""
    try:
        os.makedirs("cache", exist_ok=True)
        with open(MACRO_CACHE_PATH, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"[YahooMacro] Cache written: {MACRO_CACHE_PATH}")
    except Exception as e:
        logger.warning(f"[YahooMacro] Cache write failed: {e}")


def read_macro_cache(max_age_seconds: int = 1200) -> Optional[dict]:
    """
    Read cached macro data if fresh enough.

    Args:
        max_age_seconds: Maximum age in seconds (default 20 min)

    Returns:
        Cached dict or None if stale/missing.
    """
    import time
    try:
        if not os.path.exists(MACRO_CACHE_PATH):
            return None
        with open(MACRO_CACHE_PATH) as f:
            cached = json.load(f)

        fetched_at = cached.get("fetched_at", "")
        if fetched_at:
            import zoneinfo
            IST = zoneinfo.ZoneInfo("Asia/Kolkata")
            fetched_dt = datetime.fromisoformat(fetched_at)
            age = (datetime.now(IST) - fetched_dt).total_seconds()
            if age < max_age_seconds:
                logger.info(f"[YahooMacro] Using cached data ({age:.0f}s old)")
                return cached
            else:
                logger.info(f"[YahooMacro] Cache stale ({age:.0f}s > {max_age_seconds}s)")
                return None
        return None
    except Exception as e:
        logger.warning(f"[YahooMacro] Cache read failed: {e}")
        return None
