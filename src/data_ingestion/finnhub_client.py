"""
finnhub_client.py (v3)
----------------------
Enhanced Finnhub client with:
  - General market news (existing)
  - Company-specific news (existing)
  - Forex/commodity quotes: crude oil, gold, DXY, USD/INR
  - Intraday news refresh (callable any time, not just 6 AM)

Finnhub free tier: 60 calls/minute, 30 API calls/second.
"""
import os
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
logger = logging.getLogger(__name__)

# Rate limit tracking
_CALL_TIMESTAMPS: List[float] = []


def _wait_for_rate_limit():
    global _CALL_TIMESTAMPS
    now = time.time()
    _CALL_TIMESTAMPS = [t for t in _CALL_TIMESTAMPS if now - t < 60]
    if len(_CALL_TIMESTAMPS) >= 50:
        sleep_time = 60 - (now - _CALL_TIMESTAMPS[0])
        if sleep_time > 0:
            logger.info(f"Finnhub rate limit pause: {sleep_time:.1f}s")
            time.sleep(sleep_time)


def _get(endpoint: str, params: dict) -> Optional[dict]:
    """Generic Finnhub GET with rate limiting and error handling."""
    if not FINNHUB_API_KEY:
        return None
    _wait_for_rate_limit()
    try:
        params["token"] = FINNHUB_API_KEY
        url = f"https://finnhub.io/api/v1/{endpoint}"
        res = requests.get(url, params=params, timeout=10)
        _CALL_TIMESTAMPS.append(time.time())
        if res.status_code == 200:
            return res.json()
        logger.warning(f"Finnhub {endpoint} returned {res.status_code}")
        return None
    except Exception as e:
        logger.warning(f"Finnhub {endpoint} error: {e}")
        return None


# ── News Endpoints ─────────────────────────────────────────────────────────

def fetch_global_sentiment() -> list:
    """Fetch general market news. Callable any time for intraday updates."""
    data = _get("news", {"category": "general"})
    if isinstance(data, list):
        return [
            {"headline": n.get("headline"), "summary": n.get("summary"),
             "source": n.get("source", ""), "datetime": n.get("datetime", 0)}
            for n in data[:15]
        ]
    return []


def fetch_company_news(symbol: str, from_date: str, to_date: str) -> list:
    """Fetch news for a specific stock."""
    finnhub_sym = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    data = _get("company-news", {"symbol": finnhub_sym, "from": from_date, "to": to_date})
    if isinstance(data, list):
        return [
            {"headline": n.get("headline", ""), "summary": n.get("summary", ""),
             "url": n.get("url", ""), "datetime": n.get("datetime", 0),
             "source": n.get("source", "")}
            for n in data[:10]
        ]
    return []


# ── Forex & Commodity Quotes ──────────────────────────────────────────────

@dataclass
class MacroQuote:
    symbol: str         # "OANDA:XAU_USD", "OANDA:BCO_USD" etc.
    name: str           # "Gold", "Brent Crude", etc.
    price: float
    change_pct: float   # daily % change
    timestamp: int


# Finnhub forex/crypto symbols for key macro indicators
MACRO_SYMBOLS = {
    "OANDA:XAU_USD": "Gold (USD/oz)",
    "OANDA:BCO_USD": "Brent Crude (USD/bbl)",
    "OANDA:USD_INR": "USD/INR",
    "OANDA:EUR_USD": "EUR/USD",
    "OANDA:GBP_USD": "GBP/USD",
}

# For DXY we use the US Dollar Index ETF as proxy
DXY_SYMBOL = "UUP"


def fetch_macro_quotes() -> Dict[str, MacroQuote]:
    """
    Fetch live quotes for crude oil, gold, USD/INR, and major forex pairs.
    Uses Finnhub's /quote endpoint for forex pairs.

    Returns dict keyed by human-readable name.
    """
    results = {}

    for symbol, name in MACRO_SYMBOLS.items():
        data = _get("quote", {"symbol": symbol})
        if data and "c" in data:
            price = float(data["c"])       # current price
            prev = float(data.get("pc", price))  # previous close
            change_pct = ((price - prev) / prev * 100) if prev > 0 else 0
            results[name] = MacroQuote(
                symbol=symbol, name=name, price=price,
                change_pct=round(change_pct, 3),
                timestamp=int(data.get("t", time.time())),
            )

    # DXY proxy via US Dollar Index ETF
    dxy_data = _get("quote", {"symbol": DXY_SYMBOL})
    if dxy_data and "c" in dxy_data:
        price = float(dxy_data["c"])
        prev = float(dxy_data.get("pc", price))
        change_pct = ((price - prev) / prev * 100) if prev > 0 else 0
        results["DXY (US Dollar Index)"] = MacroQuote(
            symbol=DXY_SYMBOL, name="DXY (US Dollar Index)", price=price,
            change_pct=round(change_pct, 3),
            timestamp=int(dxy_data.get("t", time.time())),
        )

    return results


def get_macro_summary() -> str:
    """
    Returns a human-readable one-liner of macro conditions.
    Used by the runner for logging and by the scorer for macro awareness.
    """
    quotes = fetch_macro_quotes()
    if not quotes:
        return "Macro data unavailable (FINNHUB_API_KEY missing)"

    parts = []
    for name, q in quotes.items():
        arrow = "▲" if q.change_pct > 0 else "▼" if q.change_pct < 0 else "─"
        parts.append(f"{name}: {q.price:.2f} {arrow}{abs(q.change_pct):.2f}%")

    return " | ".join(parts)


def get_macro_risk_signal() -> dict:
    """
    Analyzes macro conditions and returns a risk signal.

    Returns:
        {
            "crude_risk": "high"|"medium"|"low",
            "gold_signal": "risk_off"|"neutral"|"risk_on",
            "usd_inr_pressure": "rupee_weak"|"neutral"|"rupee_strong",
            "overall_macro_bias": "risk_on"|"neutral"|"risk_off",
            "details": "human readable summary"
        }
    """
    quotes = fetch_macro_quotes()
    if not quotes:
        return {"overall_macro_bias": "neutral", "details": "No macro data available"}

    crude = quotes.get("Brent Crude (USD/bbl)")
    gold = quotes.get("Gold (USD/oz)")
    usd_inr = quotes.get("USD/INR")

    signal = {
        "crude_risk": "medium",
        "gold_signal": "neutral",
        "usd_inr_pressure": "neutral",
        "overall_macro_bias": "neutral",
        "details": "",
    }
    risk_score = 0  # negative = risk_off, positive = risk_on

    # Crude oil: rising crude → bearish for Indian market (import cost)
    if crude:
        if crude.change_pct > 2.0:
            signal["crude_risk"] = "high"
            risk_score -= 2
        elif crude.change_pct > 0.5:
            signal["crude_risk"] = "medium"
            risk_score -= 1
        else:
            signal["crude_risk"] = "low"
            risk_score += 1

    # Gold: rising gold = flight to safety = risk-off for equities
    if gold:
        if gold.change_pct > 1.0:
            signal["gold_signal"] = "risk_off"
            risk_score -= 1
        elif gold.change_pct < -0.5:
            signal["gold_signal"] = "risk_on"
            risk_score += 1

    # USD/INR: rupee weakening → FII outflows → bearish
    if usd_inr:
        if usd_inr.change_pct > 0.3:
            signal["usd_inr_pressure"] = "rupee_weak"
            risk_score -= 1
        elif usd_inr.change_pct < -0.3:
            signal["usd_inr_pressure"] = "rupee_strong"
            risk_score += 1

    # Overall
    if risk_score >= 2:
        signal["overall_macro_bias"] = "risk_on"
    elif risk_score <= -2:
        signal["overall_macro_bias"] = "risk_off"

    signal["details"] = get_macro_summary()
    return signal
