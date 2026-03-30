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

    def __post_init__(self):
        if self.added_at is None:
            self.added_at = datetime.now()


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
