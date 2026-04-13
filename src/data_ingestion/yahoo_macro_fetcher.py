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
    yahoo_count = sum(1 for k in ("spy_pct", "qqq_pct", "crude_wti_pct", "crude_brent_pct",
                                   "gold_pct", "dxy_pct", "usdinr", "us_vix", "gift_nifty")
                      if result.get(k) is not None)
    if success_parts:
        logger.info(f"[YahooMacro] {' | '.join(success_parts)}")
    if failed:
        logger.warning(f"[YahooMacro] FAILED symbols: {', '.join(failed)}")

    # ── Twelve Data fallback: fill gaps if Yahoo got < 4 symbols ──
    if yahoo_count < 4:
        td_result = fetch_global_macro_twelvedata()
        if td_result:
            td_filled = 0
            for key in ("spy_pct", "qqq_pct", "crude_wti", "crude_wti_pct",
                         "crude_brent", "crude_brent_pct", "gold", "gold_pct",
                         "dxy", "dxy_pct", "usdinr"):
                if result.get(key) is None and td_result.get(key) is not None:
                    result[key] = td_result[key]
                    td_filled += 1
            if td_filled > 0:
                result["source"] = f"yahoo_finance+twelve_data({td_filled})"
            logger.info(f"[Macro] Yahoo: {yahoo_count}/9 | TwelveData: {td_filled} gaps filled")

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


# ── Twelve Data Fallback ──────────────────────────────────────────────

_TD_EQUITY_SYMBOLS = {"SPY": "spy_pct", "QQQ": "qqq_pct"}
_TD_COMMODITY_SYMBOLS = {
    "CL1:COM": ("crude_wti", "crude_wti_pct"),
    "BZ1:COM": ("crude_brent", "crude_brent_pct"),
    "GC1:COM": ("gold", "gold_pct"),
}


def fetch_global_macro_twelvedata() -> dict:
    """
    Fallback: fetch global macro data from Twelve Data API.
    Returns same dict format as fetch_global_macro(). Never crashes.
    Silently returns {} if TWELVE_DATA_API_KEY not set.
    """
    import zoneinfo

    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        return {}

    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    result: dict = {
        "spy_pct": None, "qqq_pct": None,
        "crude_wti": None, "crude_wti_pct": None,
        "crude_brent": None, "crude_brent_pct": None,
        "gold": None, "gold_pct": None,
        "dxy": None, "dxy_pct": None,
        "usdinr": None, "us_vix": None, "gift_nifty": None,
        "fetched_at": datetime.now(IST).isoformat(),
        "source": "twelve_data",
    }
    filled = 0

    try:
        import requests
    except ImportError:
        logger.warning("[TwelveData] requests not installed")
        return {}

    def _td_get(url: str, params: dict) -> Optional[dict]:
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "code" in data and data["code"] != 200:
                    return None
                return data
        except Exception as e:
            logger.debug(f"[TwelveData] Request failed: {e}")
        return None

    # Equities: SPY, QQQ
    for symbol, pct_key in _TD_EQUITY_SYMBOLS.items():
        try:
            price_data = _td_get(
                "https://api.twelvedata.com/price",
                {"symbol": symbol, "apikey": api_key},
            )
            eod_data = _td_get(
                "https://api.twelvedata.com/eod",
                {"symbol": symbol, "apikey": api_key},
            )
            if price_data and eod_data and "price" in price_data and "close" in eod_data:
                current = float(price_data["price"])
                prev_close = float(eod_data["close"])
                if current > 0 and prev_close > 0:
                    pct = round(((current - prev_close) / prev_close) * 100, 2)
                    result[pct_key] = pct
                    filled += 1
        except Exception as e:
            logger.debug(f"[TwelveData] {symbol} failed: {e}")

    # Commodities: crude, gold
    for symbol, (abs_key, pct_key) in _TD_COMMODITY_SYMBOLS.items():
        try:
            price_data = _td_get(
                "https://api.twelvedata.com/price",
                {"symbol": symbol, "apikey": api_key},
            )
            eod_data = _td_get(
                "https://api.twelvedata.com/eod",
                {"symbol": symbol, "apikey": api_key},
            )
            if price_data and eod_data and "price" in price_data and "close" in eod_data:
                current = float(price_data["price"])
                prev_close = float(eod_data["close"])
                if current > 0 and prev_close > 0:
                    pct = round(((current - prev_close) / prev_close) * 100, 2)
                    result[abs_key] = round(current, 2)
                    result[pct_key] = pct
                    filled += 1
        except Exception as e:
            logger.debug(f"[TwelveData] {symbol} failed: {e}")

    # Forex: USD/INR
    try:
        fx_data = _td_get(
            "https://api.twelvedata.com/exchange_rate",
            {"symbol": "USD/INR", "apikey": api_key},
        )
        if fx_data and "rate" in fx_data:
            result["usdinr"] = round(float(fx_data["rate"]), 4)
            filled += 1
    except Exception as e:
        logger.debug(f"[TwelveData] USD/INR failed: {e}")

    if filled > 0:
        logger.info(f"[TwelveData] Fetched {filled} symbols")
    else:
        logger.warning("[TwelveData] No symbols fetched")

    return result


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
