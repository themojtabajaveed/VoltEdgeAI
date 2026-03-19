from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(str, Enum):
    MARKET = "MARKET"

@dataclass
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    timestamp: Optional[datetime] = None
    # Optional: stop_loss, target, etc. later

@dataclass
class OrderResult:
    success: bool
    broker_order_id: Optional[str]
    message: str
    filled_qty: int = 0
    avg_price: Optional[float] = None
