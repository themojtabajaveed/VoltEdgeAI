"""
nse_scraper.py
--------------
Scrapes live data from NSE India website:
  1. FII/DII daily flow data (buy/sell/net)
  2. Bulk/block deal data
  3. Pre-open market data

NSE endpoints used:
  FII/DII:    https://www.nseindia.com/api/fiidiiTradeReact
  Bulk deals: https://www.nseindia.com/api/snapshot-capital-market-largedeal
  Block deals: same endpoint, filtered
  Pre-open:   https://www.nseindia.com/api/market-data-pre-open?key=NIFTY

Note: NSE requires a valid session cookie. We first hit the homepage
to get cookies, then use them for API calls.
"""
import logging
from datetime import datetime, date
from typing import Dict, List, Optional
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# NSE requires these headers to not block requests
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_session: Optional[requests.Session] = None


def _get_nse_session() -> requests.Session:
    """Create a session with valid NSE cookies."""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(NSE_HEADERS)
        try:
            # Hit homepage to get session cookies
            _session.get("https://www.nseindia.com", timeout=10)
        except Exception as e:
            logger.warning(f"NSE session init failed: {e}")
    return _session


def _nse_get(endpoint: str) -> Optional[dict]:
    """GET an NSE API endpoint with session cookies."""
    session = _get_nse_session()
    url = f"https://www.nseindia.com/api/{endpoint}"
    try:
        res = session.get(url, timeout=10)
        if res.status_code == 200:
            return res.json()
        elif res.status_code == 401:
            # Session expired, reset and retry once
            global _session
            _session = None
            session = _get_nse_session()
            res = session.get(url, timeout=10)
            if res.status_code == 200:
                return res.json()
        logger.warning(f"NSE API {endpoint} returned {res.status_code}")
        return None
    except Exception as e:
        logger.warning(f"NSE API {endpoint} error: {e}")
        return None


# ── FII/DII Flow Data ─────────────────────────────────────────────────────

@dataclass
class InstitutionalFlow:
    category: str       # "FII/FPI" or "DII"
    buy_value: float    # ₹ crores
    sell_value: float   # ₹ crores
    net_value: float    # ₹ crores (buy - sell)
    date: str


def fetch_fii_dii_data() -> List[InstitutionalFlow]:
    """
    Fetch today's FII and DII cash market activity.
    Returns list with FII and DII flow data.

    This is a KEY signal:
      - FII net positive + DII net positive = very bullish
      - FII net negative = bearish pressure (foreign institutional selling)
      - DII net positive when FII selling = domestic support (range-bound)
    """
    data = _nse_get("fiidiiTradeReact")
    if not data:
        return []

    flows = []
    for item in data:
        try:
            flows.append(InstitutionalFlow(
                category=item.get("category", ""),
                buy_value=float(item.get("buyValue", 0)),
                sell_value=float(item.get("sellValue", 0)),
                net_value=float(item.get("netValue", 0)),
                date=item.get("date", str(date.today())),
            ))
        except (ValueError, KeyError) as e:
            logger.warning(f"FII/DII parse error: {e}")

    return flows


def get_institutional_signal() -> dict:
    """
    Analyze FII/DII flows and return a trading signal.

    Returns:
        {
            "fii_net_cr": float,  # FII net in ₹ crores
            "dii_net_cr": float,  # DII net in ₹ crores
            "signal": "bullish"|"bearish"|"neutral",
            "summary": str,
        }
    """
    flows = fetch_fii_dii_data()
    if not flows:
        return {"fii_net_cr": 0, "dii_net_cr": 0, "signal": "neutral",
                "summary": "FII/DII data unavailable"}

    fii_net = 0.0
    dii_net = 0.0
    for f in flows:
        if "FII" in f.category.upper() or "FPI" in f.category.upper():
            fii_net = f.net_value
        elif "DII" in f.category.upper():
            dii_net = f.net_value

    # Signal logic
    if fii_net > 500 and dii_net > 0:
        signal = "bullish"
        summary = f"Strong: FII +₹{fii_net:.0f}Cr, DII +₹{dii_net:.0f}Cr"
    elif fii_net > 0:
        signal = "bullish"
        summary = f"FII buying +₹{fii_net:.0f}Cr"
    elif fii_net < -1000:
        signal = "bearish"
        summary = f"Heavy FII selling ₹{fii_net:.0f}Cr"
    elif fii_net < -500:
        if dii_net > abs(fii_net) * 0.8:
            signal = "neutral"
            summary = f"FII selling ₹{fii_net:.0f}Cr but DII absorbing +₹{dii_net:.0f}Cr"
        else:
            signal = "bearish"
            summary = f"FII selling ₹{fii_net:.0f}Cr, DII +₹{dii_net:.0f}Cr (not enough)"
    else:
        signal = "neutral"
        summary = f"FII ₹{fii_net:.0f}Cr, DII ₹{dii_net:.0f}Cr — balanced"

    return {"fii_net_cr": fii_net, "dii_net_cr": dii_net, "signal": signal, "summary": summary}


# ── Bulk/Block Deals ──────────────────────────────────────────────────────

@dataclass
class LargeDeal:
    symbol: str
    client_name: str
    deal_type: str          # "BULK" or "BLOCK"
    buy_sell: str           # "BUY" or "SELL"
    quantity: int
    price: float
    date: str


def fetch_bulk_block_deals() -> List[LargeDeal]:
    """
    Fetch today's bulk and block deals from NSE.

    Bulk deal: > 0.5% of shares traded by single entity
    Block deal: > 5 lakh shares or ₹10 crore in a single trade

    These are STRONG signals:
      - Institutional BUY block deal = bullish
      - Promoter SELL bulk deal = very bearish
    """
    data = _nse_get("snapshot-capital-market-largedeal")
    if not data:
        return []

    deals = []
    deal_list = data.get("BLOCK_DEALS_DATA", []) + data.get("BULK_DEALS_DATA", [])

    for item in deal_list:
        try:
            symbol = item.get("symbol", "").strip()
            if not symbol:
                continue

            bs = item.get("buySell", "").upper().strip()
            if bs not in ("BUY", "SELL"):
                bs = "BUY" if "BUY" in item.get("clientName", "").upper() else "SELL"

            deals.append(LargeDeal(
                symbol=symbol,
                client_name=item.get("clientName", "Unknown"),
                deal_type="BLOCK" if item in data.get("BLOCK_DEALS_DATA", []) else "BULK",
                buy_sell=bs,
                quantity=int(float(item.get("quantity", 0))),
                price=float(item.get("price", 0)),
                date=item.get("date", str(date.today())),
            ))
        except (ValueError, KeyError) as e:
            logger.warning(f"Deal parse error: {e}")

    return deals


def get_deals_for_symbol(symbol: str) -> List[LargeDeal]:
    """Get bulk/block deals for a specific stock today."""
    all_deals = fetch_bulk_block_deals()
    return [d for d in all_deals if d.symbol.upper() == symbol.upper()]


def get_deal_signal(symbol: str) -> Optional[str]:
    """
    Check if there are institutional deals for a stock.
    Returns "INSTITUTIONAL_BUY", "INSTITUTIONAL_SELL", or None.
    """
    deals = get_deals_for_symbol(symbol)
    if not deals:
        return None

    buy_qty = sum(d.quantity for d in deals if d.buy_sell == "BUY")
    sell_qty = sum(d.quantity for d in deals if d.buy_sell == "SELL")

    if buy_qty > sell_qty * 2:
        return "INSTITUTIONAL_BUY"
    elif sell_qty > buy_qty * 2:
        return "INSTITUTIONAL_SELL"
    return None
