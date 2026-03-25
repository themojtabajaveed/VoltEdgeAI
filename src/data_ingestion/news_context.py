from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
import os
import logging
import requests

logger = logging.getLogger(__name__)

@dataclass
class NewsItem:
    symbol: Optional[str]
    source: str
    headline: str
    summary: str
    url: str
    published_at: datetime
    sentiment: Optional[str] = None  # "positive", "negative", "neutral"

class NewsClient:
    """
    NewsData.io client optimized for the Free Tier (200 credits/day).

    Daily Budget Allocation (max ~20 credits, leaving 180 as buffer):
      Pre-market macro:       1 credit   (08:30 AM)
      Sector rotation scan:   5 credits  (08:35 AM) — IT, Banking, Pharma, Auto, Energy
      Global commodity risk:  2 credits  (08:35 AM) — crude oil, gold
      Top movers catalyst:   10 credits  (09:30 AM) — specific stocks from scanner
      Mid-session pulse:      2 credits  (12:00 PM) — Nifty + negative sentiment
      Total:                 20 credits / 200 daily limit = 10% usage
    """

    BASE_URL = "https://newsdata.io/api/1/news"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("NEWDATA_API_KEY")
        if not self.api_key:
            logger.warning("NewsClient: NEWDATA_API_KEY not set; operating in stub mode.")

    def _fetch(self, params: dict) -> List[NewsItem]:
        """Core fetch method. 1 API credit per call."""
        if not self.api_key:
            return []
        params["apikey"] = self.api_key
        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=12)
            data = resp.json()
            if data.get("status") != "success":
                logger.error(f"NewsData API error: {data.get('results', data)}")
                return []
            results = []
            for item in data.get("results", [])[:10]:
                results.append(NewsItem(
                    symbol=params.get("q"),
                    source=item.get("source_id", "Unknown"),
                    headline=item.get("title", ""),
                    summary=item.get("description", "") or "",
                    url=item.get("link", ""),
                    published_at=datetime.now(),
                    sentiment=item.get("sentiment"),
                ))
            return results
        except Exception as e:
            logger.error(f"NewsData fetch error: {e}")
            return []

    # ── Pre-market (08:30 AM) ───────────────────────────────────────────
    def fetch_indian_macro_summary(self) -> List[NewsItem]:
        """1 credit: Broad Indian market headlines (NSE/BSE/Nifty)."""
        return self._fetch({
            "q": "NSE OR BSE OR Nifty",
            "country": "in",
            "category": "business",
            "language": "en",
        })

    # ── Sector rotation (08:35 AM) ──────────────────────────────────────
    def fetch_sector_news(self, sector_query: str) -> List[NewsItem]:
        """1 credit per sector: e.g. 'IT services Infosys TCS', 'banking HDFC SBI'."""
        return self._fetch({
            "q": sector_query,
            "country": "in",
            "category": "business",
            "language": "en",
        })

    # ── Global commodity risk (08:35 AM) ────────────────────────────────
    def fetch_global_commodity_news(self) -> List[NewsItem]:
        """1 credit: Track crude oil / gold / OPEC — affects Indian markets heavily."""
        return self._fetch({
            "q": "crude oil price OR gold price OR OPEC",
            "language": "en",
        })

    def fetch_global_macro_risk(self) -> List[NewsItem]:
        """1 credit: Track USD, Fed, tariffs, geopolitical risk."""
        return self._fetch({
            "q": "US Federal Reserve OR dollar index OR tariff OR recession",
            "language": "en",
            "category": "business",
        })

    # ── Stock-specific (09:30 AM, after scanner) ────────────────────────
    def fetch_stock_eod_news(self, symbol: str) -> List[NewsItem]:
        """1 credit: News for a specific NSE stock ticker."""
        return self._fetch({
            "q": symbol,
            "country": "in",
            "language": "en",
        })

    # ── Mid-session pulse (12:00 PM) ────────────────────────────────────
    def fetch_negative_market_pulse(self) -> List[NewsItem]:
        """1 credit: Only NEGATIVE sentiment Indian business news — detect panic early."""
        return self._fetch({
            "q": "market OR Nifty OR Sensex",
            "country": "in",
            "category": "business",
            "language": "en",
            "sentiment": "negative",
        })

    def fetch_positive_market_pulse(self) -> List[NewsItem]:
        """1 credit: Only POSITIVE sentiment — detect euphoria."""
        return self._fetch({
            "q": "market OR Nifty OR Sensex",
            "country": "in",
            "category": "business",
            "language": "en",
            "sentiment": "positive",
        })
