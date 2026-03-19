from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional
import uuid

@dataclass
class Position:
    id: str
    symbol: str
    side: str          # "LONG" only for now
    total_qty: int
    avg_price: float
    realized_pnl: float
    entry_time: datetime
    last_update_time: datetime
    mode: str          # "INTRADAY" | "SWING"
    strategy: str      # e.g. "ANTIGRAVITY_NEWS"
    broker_order_ids: List[str]
    initial_stop_price: Optional[float] = None

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

class PositionBook:
    def __init__(self) -> None:
        self._positions: Dict[str, Position] = {}  # keyed by symbol

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def get_open_positions(self) -> List[Position]:
        return list(self._positions.values())

    def on_buy_fill(
        self,
        symbol: str,
        qty: int,
        price: float,
        mode: str,
        strategy: str,
        initial_stop_price: Optional[float] = None,
        broker_order_id: Optional[str] = None,
    ) -> Position:
        """Create or update a LONG position after a buy fill."""
        now = datetime.now()
        
        if symbol not in self._positions:
            order_ids = [broker_order_id] if broker_order_id else []
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
                broker_order_ids=order_ids,
                initial_stop_price=initial_stop_price
            )
            self._positions[symbol] = pos
            return pos
        else:
            pos = self._positions[symbol]
            old_qty = pos.total_qty
            old_val = old_qty * pos.avg_price
            new_val = qty * price
            
            pos.total_qty += qty
            pos.avg_price = (old_val + new_val) / pos.total_qty
            pos.last_update_time = now
            
            if broker_order_id and broker_order_id not in pos.broker_order_ids:
                pos.broker_order_ids.append(broker_order_id)
            if initial_stop_price is not None:
                pos.initial_stop_price = initial_stop_price
                
            return pos

    def on_sell_fill(
        self,
        symbol: str,
        qty: int,
        price: float,
    ) -> Optional[Position]:
        """Reduce or close a LONG position after a sell fill, updating realized P&L."""
        if symbol not in self._positions:
            return None
            
        pos = self._positions[symbol]
        now = datetime.now()
        
        if qty >= pos.total_qty:
            # Full close: Realize P&L on total existing quantity
            real_pnl = (price - pos.avg_price) * pos.total_qty
            pos.realized_pnl += real_pnl
            pos.total_qty = 0
            pos.last_update_time = now
            
            # Remove from active positions and return a snapshot of the closed position
            del self._positions[symbol]
            return pos
        else:
            # Partial close: Realize P&L only on the quantity sold
            real_pnl = (price - pos.avg_price) * qty
            pos.realized_pnl += real_pnl
            pos.total_qty -= qty
            pos.last_update_time = now
            return pos

    def to_dict(self) -> dict:
        return {sym: pos.to_dict() for sym, pos in self._positions.items()}

    @classmethod
    def from_dict(cls, data: dict) -> "PositionBook":
        book = cls()
        for sym, pos_data in data.items():
            book._positions[sym] = Position.from_dict(pos_data)
        return book
