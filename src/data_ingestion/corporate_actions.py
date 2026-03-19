from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
import logging
from dateutil import parser

try:
    from jugaad_data.nse import NSELive
except ImportError:
    NSELive = None

logger = logging.getLogger(__name__)

@dataclass
class Announcement:
    exchange: str         # "NSE" or "BSE"
    symbol: str
    headline: str
    body: str
    category: str         # e.g. "Results", "Board Meeting"
    url: str
    announced_at: datetime
    raw: Optional[dict] = None


class NSEAnnouncementsClient:
    def __init__(self, session=None):
        if not NSELive:
            logger.warning("jugaad_data is not installed; NSEAnnouncementsClient operating in stub mode.")
            self.nselive = None
        else:
            self.nselive = NSELive()
        
    def _parse_timestamp(self, ts_str: str) -> datetime:
        try:
            return parser.parse(ts_str)
        except Exception:
            return datetime.now()

    def fetch_recent(self, limit: int = 100) -> List[Announcement]:
        """
        Fetch most recent NSE corporate announcements.
        """
        if not self.nselive:
            return []
            
        try:
            data = self.nselive.corporate_announcements()
        except Exception as e:
            logger.error(f"Failed to fetch from NSE: {e}")
            return []
            
        if not data:
            return []
            
        announcements = []
        for item in data[:limit]:
            symbol = item.get("symbol", "")
            headline = item.get("desc", "")
            body = item.get("attchmntText", "")
            url = item.get("attchmntFile", "")
            if url and not url.startswith("http"):
                url = "https://www.nseindia.com" + url
                
            cat = item.get("smName", "General")
            
            date_str = item.get("anndate", "")
            announced_at = self._parse_timestamp(date_str) if date_str else datetime.now()
            
            ann = Announcement(
                exchange="NSE",
                symbol=symbol,
                headline=headline,
                body=body,
                category=cat,
                url=url,
                announced_at=announced_at,
                raw=item
            )
            announcements.append(ann)
            
        return announcements
        
    def fetch_since(self, since: datetime) -> List[Announcement]:
        """
        Fetch announcements with timestamp > since.
        """
        recent = self.fetch_recent(limit=100)
        is_naive = since.tzinfo is None
        
        filtered = []
        for ann in recent:
            ann_dt = ann.announced_at
            if is_naive and ann_dt.tzinfo is not None:
                ann_dt = ann_dt.replace(tzinfo=None)
                
            if ann_dt > since:
                filtered.append(ann)
                
        return filtered
