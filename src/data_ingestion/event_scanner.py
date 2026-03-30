"""
event_scanner.py — Unified Corporate Event Aggregator
------------------------------------------------------
Aggregates events from multiple sources into a single event stream:
  1. NSE Corporate Announcements (via jugaad_data)
  2. Bulk/Block Deals (via nse_scraper.py)
  3. NewsData.io stock headlines (via news_context.py)

Scans events since previous market close (15:30 IST = ~15.5 hours of data).
Each event is classified for urgency via Groq Llama-3.3-70B.
"""
import os
import time
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MarketEvent:
    """Unified market event from any source."""
    symbol: str
    headline: str
    body: str = ""
    category: str = ""          # "EARNINGS", "DEAL", "REGULATORY", etc.
    source: str = ""            # "NSE_ANNOUNCEMENTS", "BULK_DEALS", "NEWS"
    timestamp: Optional[datetime] = None

    # Fields populated after Groq classification
    urgency: float = 0.0       # 1-10
    direction: str = "NEUTRAL" # "BUY", "SHORT", "NEUTRAL"
    event_type: str = ""       # From Groq classification
    summary: str = ""          # One-line summary
    material: bool = False     # Will this move the stock >1%?

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class EventScanner:
    """
    Aggregates corporate events from all sources since last market close.
    
    Called at 09:00 IST for morning watchlist scan, then every 2 min
    during market hours for new events.
    """

    def __init__(self):
        self._last_scan_time: Optional[datetime] = None
        self._seen_headlines: set = set()  # dedup

    def scan_since_close(self) -> List[MarketEvent]:
        """
        Fetch ALL events since previous market close (15:30 IST yesterday).
        Called once at 09:00 IST for the morning watchlist.
        
        Returns deduplicated, merged event list.
        """
        import zoneinfo
        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        now = datetime.now(IST)

        # Previous close = yesterday 15:30 IST (or Friday if today is Monday)
        close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if now.hour < 15 or (now.hour == 15 and now.minute < 30):
            close_time -= timedelta(days=1)
        # If Monday, go back to Friday
        while close_time.weekday() > 4:
            close_time -= timedelta(days=1)

        since_dt = close_time.replace(tzinfo=None)  # Make naive for comparisons
        logger.info(f"[EventScanner] Scanning events since {since_dt}")

        events: List[MarketEvent] = []

        # ── Source 1: NSE Corporate Announcements ──────────────
        try:
            from src.data_ingestion.corporate_actions import NSEAnnouncementsClient
            client = NSEAnnouncementsClient()
            announcements = client.fetch_since(since_dt)

            for ann in announcements:
                key = f"{ann.symbol}:{ann.headline[:60]}"
                if key in self._seen_headlines:
                    continue
                self._seen_headlines.add(key)

                events.append(MarketEvent(
                    symbol=ann.symbol,
                    headline=ann.headline,
                    body=ann.body[:500],
                    category=ann.category,
                    source="NSE_ANNOUNCEMENTS",
                    timestamp=ann.announced_at,
                ))

            logger.info(f"[EventScanner] NSE announcements: {len(announcements)} found")
        except Exception as e:
            logger.warning(f"[EventScanner] NSE announcements fetch failed: {e}")

        # ── Source 2: Bulk/Block Deals ─────────────────────────
        try:
            from src.data_ingestion.nse_scraper import fetch_bulk_block_deals
            deals = fetch_bulk_block_deals()

            for deal in deals:
                key = f"DEAL:{deal.symbol}:{deal.client_name}:{deal.buy_sell}"
                if key in self._seen_headlines:
                    continue
                self._seen_headlines.add(key)

                headline = (
                    f"{deal.deal_type} Deal: {deal.client_name} "
                    f"{deal.buy_sell} {int(deal.quantity):,} shares @ ₹{float(deal.price):.2f}"
                )
                events.append(MarketEvent(
                    symbol=deal.symbol,
                    headline=headline,
                    body=f"Deal type: {deal.deal_type}, Client: {deal.client_name}",
                    category="DEAL",
                    source="BULK_BLOCK_DEALS",
                    timestamp=datetime.now(),
                ))

            logger.info(f"[EventScanner] Bulk/block deals: {len(deals)} found")
        except Exception as e:
            logger.warning(f"[EventScanner] Deals fetch failed: {e}")

        self._last_scan_time = datetime.now()
        logger.info(f"[EventScanner] Total events aggregated: {len(events)}")
        return events

    def scan_new_events(self) -> List[MarketEvent]:
        """
        Incremental scan for new events since last check.
        Called every 2 minutes during market hours.
        """
        if self._last_scan_time is None:
            return self.scan_since_close()

        since = self._last_scan_time
        events: List[MarketEvent] = []

        try:
            from src.data_ingestion.corporate_actions import NSEAnnouncementsClient
            client = NSEAnnouncementsClient()
            announcements = client.fetch_since(since)

            for ann in announcements:
                key = f"{ann.symbol}:{ann.headline[:60]}"
                if key in self._seen_headlines:
                    continue
                self._seen_headlines.add(key)
                events.append(MarketEvent(
                    symbol=ann.symbol,
                    headline=ann.headline,
                    body=ann.body[:500],
                    category=ann.category,
                    source="NSE_ANNOUNCEMENTS",
                    timestamp=ann.announced_at,
                ))
        except Exception as e:
            logger.warning(f"[EventScanner] Incremental NSE fetch failed: {e}")

        self._last_scan_time = datetime.now()
        if events:
            logger.info(f"[EventScanner] {len(events)} new events since last scan")
        return events

    def classify_events(self, events: List[MarketEvent]) -> List[MarketEvent]:
        """
        Classify events for urgency via Groq Llama-3.3-70B (fast, free).
        
        Returns events with urgency, direction, event_type populated.
        """
        if not events:
            return events

        try:
            from src.llm.groq_client import classify_events_batch
        except ImportError:
            logger.error("[EventScanner] Groq client not available — events unclassified")
            return events

        # Prepare dicts for batch processing
        events_dicts = [
            {
                "symbol": e.symbol,
                "headline": e.headline,
                "category": e.category,
                "body": e.body
            }
            for e in events
        ]

        try:
            batch_results = classify_events_batch(events_dicts)
        except Exception as e:
            logger.error(f"[EventScanner] Batch classification failed: {e}")
            batch_results = [{"urgency": 0}] * len(events)

        classified = []
        for event, result in zip(events, batch_results):
            event.urgency = float(result.get("urgency", 0))
            event.direction = result.get("direction", "NEUTRAL")
            event.event_type = result.get("event_type", "UNKNOWN")
            event.summary = result.get("summary", event.headline)
            event.material = result.get("material", False)
            classified.append(event)

        # Sort by urgency descending
        classified.sort(key=lambda e: e.urgency, reverse=True)
        return classified

    def get_hot_events(self, min_urgency: float = 6.0) -> List[MarketEvent]:
        """
        Full pipeline: scan → classify → filter by urgency threshold.
        
        Returns only events with urgency >= min_urgency, sorted highest first.
        """
        events = self.scan_since_close()
        if not events:
            return []

        classified = self.classify_events(events)
        hot = [e for e in classified if e.urgency >= min_urgency]
        logger.info(f"[EventScanner] Hot events (urgency≥{min_urgency}): {len(hot)} of {len(classified)}")
        return hot
