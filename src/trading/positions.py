"""
positions.py (v3)
-----------------
Supports both LONG and SHORT positions.
Tracks trailing stop state internally — the ExitEngine updates it on each tick.

v3 changes:
  - Added threading.RLock to all mutating methods (P0 safety fix).
    Required because ExitMonitorThread and main runner thread both access
    the PositionBook. RLock (re-entrant) is used so the same thread can
    re-acquire the lock without deadlocking (e.g., in nested calls).
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional
import threading
import uuid


@dataclass
class Position:
    id: str
    symbol: str
    side: str                       # "LONG" | "SHORT"
    total_qty: int
    avg_price: float
    realized_pnl: float
    entry_time: datetime
    last_update_time: datetime
    mode: str                       # "INTRADAY" | "SWING"
    strategy: str
    broker_order_ids: List[str]
    initial_stop_price: Optional[float] = None   # hard stop at entry
    trailing_stop_price: Optional[float] = None  # updated dynamically
    highest_price: Optional[float] = None        # for LONG trailing
    lowest_price: Optional[float] = None         # for SHORT trailing
    atr: Optional[float] = None                  # ATR at time of entry
    breakeven_activated: bool = False
    # v3: Adaptive SL/TP fields
    partial_sold_pct: float = 0.0                # How much % of original qty sold (0.0 to 1.0)
    original_qty: int = 0                        # Qty at entry (before partials)
    rally_avg_volume: float = 0.0                # Avg volume during favorable move (for fake dip detection)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        d["last_update_time"] = self.last_update_time.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        data = data.copy()
        data["entry_time"] = datetime.fromisoformat(data["entry_time"])
        data["last_update_time"] = datetime.fromisoformat(data["last_update_time"])
        return cls(**data)

    def unrealized_pnl(self, ltp: float) -> float:
        """Returns current unrealized P&L given ltp."""
        if self.side == "LONG":
            return (ltp - self.avg_price) * self.total_qty
        else:  # SHORT
            return (self.avg_price - ltp) * self.total_qty

    def risk_unit(self) -> float:
        """
        1R = distance between entry and initial stop.
        Used to decide when to activate breakeven.
        """
        if self.initial_stop_price is None:
            return 0.0
        return abs(self.avg_price - self.initial_stop_price)


class PositionBook:
    """
    Thread-safe in-memory position ledger.

    All public methods acquire self._lock (RLock) before mutating state.
    The ExitMonitorThread reads & writes positions concurrently with the
    main runner thread, so locking is mandatory.

    RLock (re-entrant) is used instead of Lock to allow the same thread
    to call get_open_positions() while holding the lock internally.
    """

    def __init__(self) -> None:
        self._positions: Dict[str, Position] = {}  # keyed by symbol
        self._lock = threading.RLock()

    def get_position(self, symbol: str) -> Optional[Position]:
        with self._lock:
            return self._positions.get(symbol)

    def get_open_positions(self) -> List[Position]:
        with self._lock:
            return list(self._positions.values())

    def on_buy_fill(
        self,
        symbol: str,
        qty: int,
        price: float,
        mode: str,
        strategy: str,
        initial_stop_price: Optional[float] = None,
        atr: Optional[float] = None,
        broker_order_id: Optional[str] = None,
    ) -> Position:
        """Open or add to a LONG position."""
        now = datetime.now()
        with self._lock:
            if symbol not in self._positions:
                pos = Position(
                    id=str(uuid.uuid4()),
                    symbol=symbol,
                    side="LONG",
                    total_qty=qty,
                    avg_price=price,
                    realized_pnl=0.0,
                    entry_time=now,
                    last_update_time=now,
                    mode=mode,
                    strategy=strategy,
                    broker_order_ids=[broker_order_id] if broker_order_id else [],
                    initial_stop_price=initial_stop_price,
                    trailing_stop_price=initial_stop_price,
                    highest_price=price,
                    atr=atr,
                    original_qty=qty,
                )
                self._positions[symbol] = pos
            else:
                pos = self._positions[symbol]
                old_val = pos.total_qty * pos.avg_price
                pos.total_qty += qty
                pos.avg_price = (old_val + qty * price) / pos.total_qty
                pos.last_update_time = now
                if initial_stop_price is not None:
                    pos.initial_stop_price = initial_stop_price
                    pos.trailing_stop_price = initial_stop_price
                if broker_order_id:
                    pos.broker_order_ids.append(broker_order_id)
            return pos

    def on_short_fill(
        self,
        symbol: str,
        qty: int,
        price: float,
        mode: str,
        strategy: str,
        initial_stop_price: Optional[float] = None,
        atr: Optional[float] = None,
        broker_order_id: Optional[str] = None,
    ) -> Position:
        """Open or add to a SHORT position."""
        now = datetime.now()
        with self._lock:
            if symbol not in self._positions:
                pos = Position(
                    id=str(uuid.uuid4()),
                    symbol=symbol,
                    side="SHORT",
                    total_qty=qty,
                    avg_price=price,
                    realized_pnl=0.0,
                    entry_time=now,
                    last_update_time=now,
                    mode=mode,
                    strategy=strategy,
                    broker_order_ids=[broker_order_id] if broker_order_id else [],
                    initial_stop_price=initial_stop_price,
                    trailing_stop_price=initial_stop_price,
                    lowest_price=price,
                    atr=atr,
                    original_qty=qty,
                )
                self._positions[symbol] = pos
            else:
                pos = self._positions[symbol]
                old_val = pos.total_qty * pos.avg_price
                pos.total_qty += qty
                pos.avg_price = (old_val + qty * price) / pos.total_qty
                pos.last_update_time = now
                if initial_stop_price is not None:
                    pos.initial_stop_price = initial_stop_price
                    pos.trailing_stop_price = initial_stop_price
                if broker_order_id:
                    pos.broker_order_ids.append(broker_order_id)
            return pos

    def on_sell_fill(self, symbol: str, qty: int, price: float) -> Optional[Position]:
        """Close or reduce a LONG position."""
        with self._lock:
            if symbol not in self._positions:
                return None
            pos = self._positions[symbol]
            now = datetime.now()
            if qty >= pos.total_qty:
                pos.realized_pnl += _precise_pnl(
                    side="LONG", qty=pos.total_qty,
                    entry=pos.avg_price, exit_=price
                )
                pos.total_qty = 0
                pos.last_update_time = now
                del self._positions[symbol]
            else:
                pos.realized_pnl += _precise_pnl(
                    side="LONG", qty=qty,
                    entry=pos.avg_price, exit_=price
                )
                pos.total_qty -= qty
                pos.last_update_time = now
            return pos

    def on_cover_fill(self, symbol: str, qty: int, price: float) -> Optional[Position]:
        """Close or reduce a SHORT position (buy-to-cover)."""
        with self._lock:
            if symbol not in self._positions:
                return None
            pos = self._positions[symbol]
            now = datetime.now()
            if qty >= pos.total_qty:
                pos.realized_pnl += _precise_pnl(
                    side="SHORT", qty=pos.total_qty,
                    entry=pos.avg_price, exit_=price
                )
                pos.total_qty = 0
                pos.last_update_time = now
                del self._positions[symbol]
            else:
                pos.realized_pnl += _precise_pnl(
                    side="SHORT", qty=qty,
                    entry=pos.avg_price, exit_=price
                )
                pos.total_qty -= qty
                pos.last_update_time = now
            return pos

    def to_dict(self) -> dict:
        with self._lock:
            return {sym: pos.to_dict() for sym, pos in self._positions.items()}

    @classmethod
    def from_dict(cls, data: dict) -> "PositionBook":
        book = cls()
        for sym, pos_data in data.items():
            book._positions[sym] = Position.from_dict(pos_data)
        return book


# ── Decimal-precision P&L helper ─────────────────────────────────────────────

_TWO_DP = Decimal("0.01")


def _precise_pnl(side: str, qty: int, entry: float, exit_: float) -> float:
    """
    Calculate P&L using Decimal arithmetic to avoid float accumulation drift.
    Returns a plain float at the boundary (Python float is 64-bit IEEE 754,
    fine for storage; Decimal is used only during computation).

    For ₹50,000 turnover at 0.025% accumulation over 50 trades, float drift
    can compound to ~₹0.30. Decimal eliminates this entirely.
    """
    d_qty = Decimal(str(qty))
    d_entry = Decimal(str(entry))
    d_exit = Decimal(str(exit_))

    if side == "LONG":
        pnl = (d_exit - d_entry) * d_qty
    else:  # SHORT
        pnl = (d_entry - d_exit) * d_qty

    return float(pnl.quantize(_TWO_DP, rounding=ROUND_HALF_UP))
