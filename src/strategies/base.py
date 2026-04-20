"""
base.py — Strategy Head Abstract Base Class
---------------------------------------------
Every Dragon Head (HYDRA, VIPER, etc.) inherits from StrategyHead.
Provides a common interface for the runner to orchestrate strategies.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List

logger = logging.getLogger(__name__)


@dataclass
class ConvictionScore:
    """
    Unified conviction score for trade decisions.
    Range: 0-100. Threshold: 70 to trade.
    """
    strategy: str               # "HYDRA", "VIPER", etc.
    symbol: str
    direction: str              # "BUY" or "SHORT"
    total: float = 0.0          # Final score (0-100)

    # Component breakdown
    event_strength: float = 0.0     # How strong is the catalyst (0-70 for HYDRA)
    technical_confirm: float = 0.0  # TA confirmation (0-22)
    depth_signal: float = 0.0       # Order book intelligence (0-10)
    context_bonus: float = 0.0      # Macro, sector, or time-of-day context bonus (0-10)
    llm_conviction: float = 0.0     # Grok 4.20 analysis (0-20, weighted)

    # Metadata
    reasoning: str = ""
    timestamp: Optional[datetime] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

    @property
    def should_trade(self) -> bool:
        return self.total >= 70.0

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "direction": self.direction,
            "total": round(self.total, 1),
            "event_strength": round(self.event_strength, 1),
            "technical_confirm": round(self.technical_confirm, 1),
            "depth_signal": round(self.depth_signal, 1),
            "context_bonus": round(self.context_bonus, 1),
            "llm_conviction": round(self.llm_conviction, 1),
            "reasoning": self.reasoning,
            "timestamp": str(self.timestamp),
        }


@dataclass
class WatchlistEntry:
    """A stock on a strategy's watchlist with its current scores."""
    symbol: str
    direction: str              # "BUY" or "SHORT"
    event_summary: str = ""     # Why this stock is on the watchlist
    urgency: float = 0.0        # 1-10 from Groq classification
    conviction: Optional[ConvictionScore] = None
    added_at: Optional[datetime] = None
    last_checked: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)  # Strategy-specific extra data
    # DAWN scoring fields — enriched by HYDRA at scan() time
    catalyst_strength: str = "LOW"      # "HIGH" / "MEDIUM" / "LOW"
    gap_pct: float = 0.0                # estimated pre-market gap % (0.0 = unknown)
    volume_signal: str = "NORMAL"       # "SURGE" / "ELEVATED" / "NORMAL"
    sector_momentum: str = "NEUTRAL"    # "BULLISH" / "BEARISH" / "NEUTRAL"
    technical_setup: str = "NEUTRAL"    # "BREAKOUT" / "SUPPORT" / "NEUTRAL"
    fii_flow: str = "NEUTRAL"           # "BUYING" / "SELLING" / "NEUTRAL"
    # Filing metadata — used by DawnHydraRouter
    filing_category: str = ""           # e.g. "ORDER_WIN", "PRESS_RELEASE"
    filing_urgency: float = 0.0         # 0-10 from filing classifier
    avg_volume_20d: int = 0             # 20d average daily volume, for liquidity gate
    filed_at: Optional[str] = None      # ISO timestamp of the filing (if known)
    # 4-layer event-evaluation inputs (R4 / event_quality). All Optional so legacy
    # callers that don't populate them don't break; scorers treat None as neutral.
    filing_subject: Optional[str] = None        # raw filing subject line (keyword enrichment)
    price_at_filing_time: Optional[float] = None  # LTP at filing_ts, for Layer-2 reaction check
    deal_size_inr: Optional[float] = None       # order / deal value INR, drives deal_size_ratio
    market_cap_inr: Optional[float] = None      # company market cap INR, denominator of ratio
    earnings_surprise_pct: Optional[float] = None  # PAT/revenue YoY surprise % (results only)
    event_quality_score: Optional[float] = None   # populated by router after Layer-3 scoring
    filing_freshness: Optional[str] = None        # populated by router: PRISTINE/FRESH/WARM/STALE
    # Pre-packed conviction adjustment from event_scanner (freshness + L4 confirmation).
    # 0 means neutral / no adjustment; positive → boost, negative → penalty.
    conviction_modifier: int = 0
    # DawnHydraRouter decision metadata — set by router after classification
    route: str = ""                     # "DAWN" or "HYDRA"
    confidence: float = 0.0             # router confidence 0.0-1.0 (rules_passed/6)
    routed_at: Optional[str] = None     # ISO timestamp when routing was decided (IST)

    def __post_init__(self):
        if self.added_at is None:
            self.added_at = datetime.now()

    @property
    def momentum_score(self) -> float:
        """Intraday move as a decimal fraction (gap_pct / 100)."""
        return self.gap_pct / 100.0


class StrategyHead(ABC):
    """
    Abstract base class for all Dragon Heads.

    Each head must implement:
      - scan(): Discover candidate stocks
      - evaluate(): Compute conviction score for a candidate
      - get_watchlist(): Return current watchlist
    """

    def __init__(self, name: str, max_watchlist: int = 5):
        self.name = name
        self.watchlist: List[WatchlistEntry] = []
        self.max_watchlist = max_watchlist
        self.trade_placed_today = False
        self._last_scan_time: Optional[datetime] = None
        logger.info(f"[{self.name}] Strategy head initialized")

    @abstractmethod
    def scan(self) -> List[WatchlistEntry]:
        """
        Scan for candidate stocks.
        Returns a list of WatchlistEntry objects ranked by urgency.
        """
        ...

    @abstractmethod
    def evaluate(self, entry: WatchlistEntry, snapshot, depth_analysis) -> ConvictionScore:
        """
        Evaluate a watchlist entry against technical and depth data.
        Returns a ConvictionScore.
        """
        ...

    def update_watchlist(self, entries: List[WatchlistEntry]):
        """Replace watchlist with new ranked entries (top N)."""
        self.watchlist = sorted(entries, key=lambda e: e.urgency, reverse=True)[:self.max_watchlist]
        logger.info(f"[{self.name}] Watchlist updated: {[e.symbol for e in self.watchlist]}")

    def get_watchlist(self) -> List[WatchlistEntry]:
        return self.watchlist

    def mark_trade_placed(self):
        self.trade_placed_today = True

    def reset_daily(self):
        """Reset state at start of new trading day."""
        self.trade_placed_today = False
        self.watchlist = []
        self._last_scan_time = None
        logger.info(f"[{self.name}] Daily reset complete")
