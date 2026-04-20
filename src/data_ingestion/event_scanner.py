"""
event_scanner.py — Unified Corporate Event Aggregator
------------------------------------------------------
Aggregates events from multiple sources into a single event stream:
  1. NSE Corporate Announcements (via jugaad_data)
  2. Bulk/Block Deals (via nse_scraper.py)
  3. NewsData.io stock headlines (via news_context.py)

Scans events since previous market close (15:30 IST = ~15.5 hours of data).
Each event is classified for urgency via Groq Llama-3.3-70B.

After classification, events pass through the 4-layer event-evaluation gate
(apply_event_evaluation_gate) before injection into HYDRA watchlist:
  Layer 1 — market-time freshness (NSE session minutes since filing)
  Layer 2 — price reaction gate (already moved beyond threshold?)
  Layer 3 — event quality score (magnitude × deal size × surprise × ...)
  Layer 4 — market confirmation (volume / price velocity after N market-min)
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


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

    # ── 4-Layer Event Evaluation (populated by apply_event_evaluation_gate) ──
    # Filing fundamentals (optional — caller may pre-populate from pre-market cache)
    price_at_filing: Optional[float] = None     # LTP at filing_ts, for Layer-2 reaction
    deal_size_inr: Optional[float] = None       # parsed from subject/body if available
    market_cap_inr: Optional[float] = None      # from pre-market cache
    # Market-confirmation inputs (optional — caller may supply live tape data)
    volume_current_15min: Optional[float] = None
    volume_avg_15min: Optional[float] = None
    price_velocity_pct: Optional[float] = None
    # Gate outputs (populated on survivors; discarded events never reach callers)
    filing_freshness: Optional[str] = None      # "PRISTINE"/"FRESH"/"WARM"/"STALE"
    event_quality_score: Optional[float] = None
    market_confirmation: Optional[str] = None   # "CONFIRMED"/"INDIFFERENT"/"TOO_EARLY"/"NO_DATA"
    conviction_modifier: int = 0                # net bonus/penalty for downstream use

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

    @property
    def filed_at_dt(self) -> Optional[datetime]:
        """IST-aware datetime for the filing. Uses `timestamp` as source of truth.
        Naive timestamps are assumed IST (matches the rest of the event pipeline)."""
        ts = self.timestamp
        if ts is None:
            return None
        if ts.tzinfo is None:
            return ts.replace(tzinfo=IST)
        return ts


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
            from src.data_ingestion.exchange_filings import _parse_deal_size_inr
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
                    deal_size_inr=_parse_deal_size_inr(ann.headline),
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
            from src.data_ingestion.exchange_filings import _parse_deal_size_inr
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
                    deal_size_inr=_parse_deal_size_inr(ann.headline),
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


# ── 4-Layer Event Evaluation gate (module-level helper) ──────────────────────
# Applied after classify_events(), before injection into HYDRA watchlist.
# Runner calls: hot = [e for e in classified if e.urgency >= 7.0]
#               survivors = apply_event_evaluation_gate(scanner, hot)
# Each discard emits a structured [EVENT SCANNER] log line that grep-parses for
# post-market audit.

def _discard_log(symbol: str, layer: str, reason: str,
                 market_min: int, quality: Optional[float],
                 freshness: Optional[str]) -> None:
    """Emit the canonical discard log line."""
    q = f"{quality:.1f}" if isinstance(quality, (int, float)) else "None"
    f = freshness if freshness else "N/A"
    logger.info(
        f"[EVENT SCANNER] {symbol} DISCARDED | {layer}: {reason} | "
        f"market_min={market_min} | quality={q} | freshness={f}"
    )


def _inject_log(symbol: str, freshness: Optional[str], quality: Optional[float],
                l4_status: Optional[str], conviction_modifier: int) -> None:
    """Emit the canonical injection log line."""
    q = f"{quality:.1f}" if isinstance(quality, (int, float)) else "None"
    sign = "+" if conviction_modifier >= 0 else ""
    logger.info(
        f"[EVENT SCANNER] {symbol} INJECTED → HYDRA | freshness={freshness or 'N/A'} "
        f"| quality={q}/100 | L4={l4_status or 'N/A'} "
        f"| conviction_modifier={sign}{conviction_modifier}"
    )


def _count_recent_same_category(
    scanner_seen: set, symbol: str, category: str
) -> int:
    """Count prior (symbol, category) pairs observed in this scanner session.
    Deliberately conservative: the seen-headlines cache is the only in-memory
    history the scanner keeps, so this counts session-level repetition, not
    5-day-lookback repetition. Full cross-session lookback lives in Step 6."""
    if not category:
        return 0
    category_key = category.upper()
    count = 0
    for key in scanner_seen:
        # seen cache entries look like "SYMBOL:HEADLINE_PREFIX" (see scan_*).
        # Match by symbol prefix AND category keyword in the headline.
        if not isinstance(key, str):
            continue
        if not key.startswith(f"{symbol}:"):
            continue
        if category_key in key.upper():
            count += 1
    # Subtract 1 for the current event's own entry, if already added.
    return max(0, count - 1)


def apply_event_evaluation_gate(
    events: List[MarketEvent],
    seen_headlines: Optional[set] = None,
    now: Optional[datetime] = None,
) -> List[MarketEvent]:
    """Apply the 4-layer event-evaluation gate to classified events.

    Each event is evaluated against L1/L2 freshness, L3 event-quality, and L4
    market-confirmation. Survivors are tagged with filing_freshness,
    event_quality_score, market_confirmation, and conviction_modifier.
    Discards emit a structured [EVENT SCANNER] DISCARDED log line.

    Args:
        events:          classified MarketEvents (already have urgency/direction/category).
        seen_headlines:  the scanner's dedup cache — used for session-level
                         same-category repetition counting (conservative).
        now:             override for datetime.now(IST), for testability.

    Returns:
        Survivors (discards are filtered out). Original list is not mutated.
    """
    if not events:
        return []

    # Lazy imports so scanner-only callers don't pay the config cost.
    from src.config_loader import (
        get_event_quality_config,
        get_filing_freshness_config,
        get_market_confirmation_config,
    )
    from src.utils.event_quality import (
        classify_market_confirmation,
        passes_quality_gate,
        score_event_quality,
    )
    from src.utils.filing_freshness import (
        FilingFreshness,
        classify_filing_freshness,
    )
    from src.utils.market_calendar import market_minutes_of_exposure

    fresh_cfg = get_filing_freshness_config()
    quality_cfg = get_event_quality_config()
    l4_cfg = get_market_confirmation_config()

    as_of = now or datetime.now(IST)
    seen = seen_headlines or set()

    survivors: List[MarketEvent] = []
    for evt in events:
        filing_ts = evt.filed_at_dt
        if filing_ts is None:
            # No timestamp → cannot run any layer that depends on market time.
            # Skip L1/L2/L4 and fall through to L3 only.
            market_min = 0
            freshness_tier: Optional[FilingFreshness] = None
        else:
            market_min = market_minutes_of_exposure(filing_ts, as_of)

            # ── Layer 1 + 2 ──────────────────────────────────────────────
            # event_scanner has no direct Kite ticker reference, so live LTP is
            # not available here. Force price_at_filing=None → classifier skips
            # L2 gracefully (prints "L2 skipped" in reason). Layer-2 enforcement
            # happens in router.py and conviction_engine where LTP is available.
            tier, fresh_reason = classify_filing_freshness(
                filing_ts=filing_ts,
                as_of=as_of,
                price_at_filing=None,
                price_now=0.0,
                config=fresh_cfg,
            )
            freshness_tier = tier
            evt.filing_freshness = tier.value
            if tier == FilingFreshness.STALE:
                _discard_log(
                    evt.symbol, "L1/L2", fresh_reason,
                    market_min, None, tier.value,
                )
                continue

        # ── Layer 3: event quality ───────────────────────────────────────
        category = (evt.category or evt.event_type or "UNKNOWN").upper()
        recent_count = _count_recent_same_category(seen, evt.symbol, category)
        quality_score, _breakdown = score_event_quality(
            category=category,
            filing_subject=evt.headline,
            deal_size_inr=evt.deal_size_inr,
            market_cap_inr=evt.market_cap_inr,
            earnings_surprise_pct=None,   # intraday scanner doesn't parse results yet
            sector_followthrough=None,    # wired in Step 6
            recent_same_category_count=recent_count,
            config=quality_cfg,
        )
        evt.event_quality_score = quality_score
        quality_ok, quality_reason = passes_quality_gate(quality_score, quality_cfg)
        if not quality_ok:
            _discard_log(
                evt.symbol, "L3", quality_reason,
                market_min, quality_score, evt.filing_freshness,
            )
            continue

        # ── Layer 4: market confirmation ─────────────────────────────────
        l4_status = "NO_DATA"
        l4_reason = "market confirmation skipped — no filing timestamp"
        if filing_ts is not None:
            l4_status, l4_reason = classify_market_confirmation(
                market_minutes_elapsed=market_min,
                volume_current_15min=evt.volume_current_15min,
                volume_avg_15min=evt.volume_avg_15min,
                price_velocity_pct=evt.price_velocity_pct,
                config=l4_cfg,
            )
        evt.market_confirmation = l4_status
        if l4_status == "INDIFFERENT":
            _discard_log(
                evt.symbol, "L4", l4_reason,
                market_min, quality_score, evt.filing_freshness,
            )
            continue

        # ── Conviction modifier ──────────────────────────────────────────
        modifier = 0
        if evt.filing_freshness == "PRISTINE":
            modifier += int(fresh_cfg.get("pristine_conviction_bonus", 8))
        elif evt.filing_freshness == "FRESH":
            modifier += int(fresh_cfg.get("fresh_conviction_bonus", 4))
        # WARM → 0 (neutral); STALE already discarded.
        if l4_status == "CONFIRMED":
            modifier += int(l4_cfg.get("confirmed_conviction_bonus", 10))
        elif l4_status == "NO_DATA":
            modifier -= int(l4_cfg.get("indifferent_conviction_penalty", 20))
        # TOO_EARLY → 0 (fair pass-through for PRISTINE filings).
        evt.conviction_modifier = modifier

        _inject_log(
            evt.symbol, evt.filing_freshness, quality_score,
            l4_status, modifier,
        )
        survivors.append(evt)

    return survivors
