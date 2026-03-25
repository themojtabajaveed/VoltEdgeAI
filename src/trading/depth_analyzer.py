"""
depth_analyzer.py
-----------------
Analyzes Kite Level-2 order book depth (bid-ask) to assess
institutional support/resistance at current price levels.

Kite sends 5 levels of depth in full mode:
  depth.buy  = [{price, quantity, orders}, ...]  (best 5 bids)
  depth.sell = [{price, quantity, orders}, ...]  (best 5 asks)

What we analyze:
  1. Bid-ask spread: tight spread = liquid, wide = avoid
  2. Buy vs sell wall: big bid wall = support, big ask wall = resistance
  3. Order count imbalance: many small orders vs few large ones
"""
import logging
from dataclasses import dataclass
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class DepthLevel:
    price: float
    quantity: int
    orders: int


@dataclass
class DepthAnalysis:
    symbol: str
    ltp: float
    bid_ask_spread_pct: float   # as % of LTP
    buy_depth_qty: int          # total bid quantity (5 levels)
    sell_depth_qty: int         # total ask quantity (5 levels)
    imbalance_ratio: float      # buy_qty / sell_qty (>1 = more buyers)
    buy_wall_detected: bool     # single bid level has >40% of total bid
    sell_wall_detected: bool    # single ask level has >40% of total ask
    is_liquid: bool             # spread < 0.1% and reasonable depth
    signal: str                 # "strong_bid" | "balanced" | "strong_ask" | "illiquid"

    @property
    def summary(self) -> str:
        return (f"{self.symbol}: spread={self.bid_ask_spread_pct:.3f}% "
                f"imbalance={self.imbalance_ratio:.2f} "
                f"({self.signal})")


def analyze_depth(symbol: str, tick_data: dict, ltp: float = 0) -> Optional[DepthAnalysis]:
    """
    Analyze order book depth from a Kite tick.

    Args:
        symbol: Stock symbol
        tick_data: Raw tick dict from Kite WebSocket (must have 'depth' key)
        ltp: Last traded price

    Returns:
        DepthAnalysis or None if depth data unavailable
    """
    depth = tick_data if isinstance(tick_data, dict) else None
    if depth is None:
        return None

    buy_levels = depth.get("buy", [])
    sell_levels = depth.get("sell", [])

    if not buy_levels or not sell_levels:
        return None

    # Parse levels
    bids = [DepthLevel(
        price=float(l.get("price", 0)),
        quantity=int(l.get("quantity", 0)),
        orders=int(l.get("orders", 0))
    ) for l in buy_levels if l.get("price", 0) > 0]

    asks = [DepthLevel(
        price=float(l.get("price", 0)),
        quantity=int(l.get("quantity", 0)),
        orders=int(l.get("orders", 0))
    ) for l in sell_levels if l.get("price", 0) > 0]

    if not bids or not asks:
        return None

    # 1. Bid-ask spread
    best_bid = bids[0].price
    best_ask = asks[0].price
    if ltp <= 0:
        ltp = (best_bid + best_ask) / 2
    spread_pct = ((best_ask - best_bid) / ltp * 100) if ltp > 0 else 99.0

    # 2. Total depth quantities
    buy_qty = sum(b.quantity for b in bids)
    sell_qty = sum(a.quantity for a in asks)

    # 3. Imbalance ratio
    imbalance = buy_qty / sell_qty if sell_qty > 0 else 10.0

    # 4. Wall detection (single level > 40% of total)
    buy_wall = any(b.quantity > buy_qty * 0.4 for b in bids) if buy_qty > 0 else False
    sell_wall = any(a.quantity > sell_qty * 0.4 for a in asks) if sell_qty > 0 else False

    # 5. Liquidity check
    is_liquid = spread_pct < 0.1 and (buy_qty + sell_qty) > 1000

    # 6. Signal
    if spread_pct > 0.5:
        signal = "illiquid"
    elif imbalance > 2.0:
        signal = "strong_bid"
    elif imbalance < 0.5:
        signal = "strong_ask"
    else:
        signal = "balanced"

    return DepthAnalysis(
        symbol=symbol,
        ltp=ltp,
        bid_ask_spread_pct=round(spread_pct, 4),
        buy_depth_qty=buy_qty,
        sell_depth_qty=sell_qty,
        imbalance_ratio=round(imbalance, 3),
        buy_wall_detected=buy_wall,
        sell_wall_detected=sell_wall,
        is_liquid=is_liquid,
        signal=signal,
    )


def get_depth_score_modifier(analysis: Optional[DepthAnalysis], direction: str = "LONG") -> float:
    """
    Returns a score modifier based on order book depth.

    For LONG:
      strong_bid → 1.10 (buyers stacked up — supportive)
      strong_ask → 0.85 (sellers stacked — resistance ahead)
      illiquid   → 0.70 (too wide spread — dangerous)

    For SHORT:
      strong_ask → 1.10 (sellers dominant — shorts confirmed)
      strong_bid → 0.85 (bid wall — shorts risky)
    """
    if analysis is None:
        return 1.0

    if analysis.signal == "illiquid":
        return 0.70  # NEVER trade illiquid stocks regardless of direction

    if direction == "LONG":
        if analysis.signal == "strong_bid":
            return 1.10
        elif analysis.signal == "strong_ask":
            return 0.85
        elif analysis.sell_wall_detected:
            return 0.90
    elif direction == "SHORT":
        if analysis.signal == "strong_ask":
            return 1.10
        elif analysis.signal == "strong_bid":
            return 0.85
        elif analysis.buy_wall_detected:
            return 0.90

    return 1.0


def should_skip_illiquid(analysis: Optional[DepthAnalysis]) -> bool:
    """Hard check: skip any stock with spread > 0.3%."""
    if analysis is None:
        return False  # No data — don't block
    return analysis.bid_ask_spread_pct > 0.3
