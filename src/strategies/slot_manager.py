"""
slot_manager.py — Global Trade Budget & Symbol Lock
----------------------------------------------------
Manages the trade budget across all strategy heads.
Features:
  - Dynamic conviction-based gating (no rigid cap)
  - Max 5 open positions at any time (safety rail)
  - Cross-head confluence detection (+15 bonus, 150% capital)
  - Symbol locking to prevent duplicates
"""
import logging
from datetime import datetime, date
from typing import Optional, Dict, Set, List
from dataclasses import dataclass, field

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except ImportError:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

logger = logging.getLogger(__name__)

# Trade conviction thresholds
CONVICTION_THRESHOLD = 70.0         # Minimum to trade
CONFLUENCE_BONUS = 15.0             # Added when multiple heads agree
MAX_OPEN_POSITIONS = 5              # Safety rail: max simultaneous positions


@dataclass
class TradeSlot:
    """Record of a trade slot allocation."""
    strategy: str
    symbol: str
    direction: str      # "BUY" or "SHORT"
    conviction: float
    allocated_at: datetime
    capital_pct: float = 1.0  # 1.0 = 100% of per-trade capital
    is_confluence: bool = False  # True if multiple heads agree


class SlotManager:
    """
    Global trade budget controller with dynamic conviction-based gating.
    
    No rigid trade cap. Instead:
      - conviction >= 85 -> full capital
      - conviction 70-84 -> 70% capital
      - conviction < 70 -> no trade
      - Max 5 open positions at any time (safety rail)
      - Cross-head confluence -> +15 bonus, 150% capital
      - No kill switch: each trade manages its own risk via adaptive SL
    """

    def __init__(self, max_trades: int = MAX_OPEN_POSITIONS):
        self.max_trades = max_trades
        self._slots: list[TradeSlot] = []
        self._locked_symbols: Dict[str, TradeSlot] = {}  # symbol -> slot
        self._current_date: Optional[date] = None
        self._confluence_symbols: Set[str] = set()  # symbols in multiple heads

    def reset_daily(self):
        """Reset all slots at start of new trading day."""
        self._slots = []
        self._locked_symbols = {}
        self._confluence_symbols = set()
        self._current_date = datetime.now(IST).date()
        logger.info(f"[SlotManager] Daily reset — max {self.max_trades} open positions")

    @property
    def trades_today(self) -> int:
        return len(self._slots)

    @property
    def remaining(self) -> int:
        return max(0, self.max_trades - len(self._slots))

    @property
    def locked_symbols(self) -> Set[str]:
        return set(self._locked_symbols.keys())

    def register_confluence(self, symbols: List[str]) -> None:
        """
        Register symbols that appear in multiple strategy watchlists.
        Called by runner when cross-head overlap is detected.
        """
        if symbols:
            self._confluence_symbols.update(symbols)
            logger.info(f"[SlotManager] CONFLUENCE registered: {symbols}")

    def is_confluence(self, symbol: str) -> bool:
        """Check if a symbol has cross-head confluence."""
        return symbol in self._confluence_symbols

    def get_capital_allocation(self, conviction: float, symbol: str) -> float:
        """
        Dynamic capital allocation based on conviction level.
        Returns fraction of per-trade capital (0.0 to 1.0).

        Note: Confluence bonus is already added to the conviction score
        (+15 via CONFLUENCE_BONUS), so we do NOT apply a separate
        capital multiplier here to avoid double-counting risk.
        """
        if conviction >= 85:
            return 1.0   # Full capital for exceptional setups
        elif conviction >= 70:
            return 0.7   # 70% for strong setups
        else:
            return 0.0   # Below threshold

    def can_trade(self, symbol: str, direction: str) -> tuple[bool, str]:
        """
        Check if a trade is allowed.
        
        Returns:
            (allowed: bool, reason: str)
        """
        today = datetime.now(IST).date()
        if self._current_date != today:
            self.reset_daily()

        if len(self._slots) >= self.max_trades:
            return False, f"Max open positions reached ({self.max_trades}/{self.max_trades})"

        if symbol in self._locked_symbols:
            existing = self._locked_symbols[symbol]
            if existing.direction != direction:
                return False, (
                    f"{symbol} already has a {existing.direction} position "
                    f"from {existing.strategy} — cannot take opposite direction"
                )
            return False, f"{symbol} already claimed by {existing.strategy}"

        return True, "OK"

    def allocate(self, strategy: str, symbol: str, direction: str, conviction: float) -> bool:
        """
        Allocate a trade slot for a strategy.
        Returns True if allocated, False if rejected.
        """
        allowed, reason = self.can_trade(symbol, direction)
        if not allowed:
            logger.warning(f"[SlotManager] REJECTED: {strategy} -> {direction} {symbol} — {reason}")
            return False

        is_conf = self.is_confluence(symbol)
        capital = self.get_capital_allocation(conviction, symbol)

        slot = TradeSlot(
            strategy=strategy,
            symbol=symbol,
            direction=direction,
            conviction=conviction,
            allocated_at=datetime.now(),
            capital_pct=capital,
            is_confluence=is_conf,
        )
        self._slots.append(slot)
        self._locked_symbols[symbol] = slot

        tag = " CONFLUENCE" if is_conf else ""
        logger.info(
            f"[SlotManager] ALLOCATED: {strategy} -> {direction} {symbol} "
            f"(conviction={conviction:.1f}, capital={capital:.0%}){tag} "
            f"[{self.trades_today}/{self.max_trades}]"
        )
        return True

    def release(self, symbol: str) -> None:
        """
        Release a slot when a position is fully closed.
        Called by the runner after a full exit (not partials).
        """
        if symbol in self._locked_symbols:
            freed_slot = self._locked_symbols.pop(symbol)
            # Remove from _slots list as well
            self._slots = [s for s in self._slots if s.symbol != symbol]
            logger.info(
                f"[SlotManager] RELEASED: {freed_slot.strategy} -> {symbol} "
                f"[{self.trades_today}/{self.max_trades}]"
            )
        else:
            logger.debug(f"[SlotManager] release({symbol}): not found in locked symbols")

    def get_status(self) -> dict:
        """Return current slot manager state for reporting."""
        return {
            "date": str(self._current_date),
            "trades_used": self.trades_today,
            "trades_remaining": self.remaining,
            "confluence_symbols": list(self._confluence_symbols),
            "locked_symbols": {
                sym: {
                    "strategy": s.strategy,
                    "direction": s.direction,
                    "conviction": s.conviction,
                    "capital_pct": s.capital_pct,
                    "is_confluence": s.is_confluence,
                }
                for sym, s in self._locked_symbols.items()
            },
        }
