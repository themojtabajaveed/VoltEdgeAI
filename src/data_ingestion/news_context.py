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
    sentiment: Optional[str] = None  # "pos", "neg", "neu"

class NewsClient:
    def __init__(self, api_key: Optional[str] = None):
        """
        If api_key is None, operates in 'disabled' mode returning empty arrays safely.
        """
        self.api_key = api_key or os.getenv("NEWS_API_KEY")
        if not self.api_key:
            logger.warning("NewsClient initialized without NEWS_API_KEY; operating in stub mode.")
            
    def search_symbol(self, symbol: str, since: datetime) -> List[NewsItem]:
        """Fetch news items tagged to this symbol explicitly."""
        if not self.api_key:
            return []
            
        logger.info(f"NewsClient searching for symbol {symbol}. Stub simulated return.")
        return []

    def search_query(self, query: str, since: datetime) -> List[NewsItem]:
        """Free-text search over macroeconomic or thematic vectors."""
        if not self.api_key:
            return []
            
        logger.info(f"NewsClient searching query '{query}'. Stub simulated return.")
        return []
