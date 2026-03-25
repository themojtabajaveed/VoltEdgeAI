"""
trading_costs.py
----------------
Calculates realistic Indian stock market transaction costs.

Zerodha (Kite) cost structure for INTRADAY (MIS) equity:
  Brokerage:    ₹20 flat per executed order (or 0.03%, whichever is lower)
  STT:          0.025% on sell side (Securities Transaction Tax)
  Exchange:     0.00345% on turnover (NSE)
  GST:          18% on brokerage + exchange charges
  SEBI:         ₹10 per crore turnover
  Stamp:        0.003% on buy side (varies by state)

For a round-trip (buy + sell):
  Total estimated cost ≈ 0.05% to 0.12% of turnover depending on trade size.
"""
from dataclasses import dataclass


@dataclass
class TradeCost:
    """Breakdown of all charges for a single leg (buy or sell)."""
    brokerage: float
    stt: float
    exchange_charges: float
    gst: float
    sebi_charges: float
    stamp_duty: float
    total: float

    @property
    def summary(self) -> str:
        return (f"Brokerage=₹{self.brokerage:.2f}, STT=₹{self.stt:.2f}, "
                f"Exchange=₹{self.exchange_charges:.2f}, GST=₹{self.gst:.2f}, "
                f"Stamp=₹{self.stamp_duty:.2f}, Total=₹{self.total:.2f}")


def compute_leg_cost(
    turnover: float,
    is_sell: bool = False,
    is_intraday: bool = True,
) -> TradeCost:
    """
    Compute costs for a single trade leg (either buy or sell).

    Args:
        turnover: qty × price (₹ value of the leg)
        is_sell: True for sell/short-cover legs (STT applies on sell side)
        is_intraday: True for MIS orders (lower STT than CNC)

    Returns:
        TradeCost with full breakdown
    """
    # Brokerage: ₹20 flat or 0.03%, whichever is lower
    brokerage = min(20.0, turnover * 0.0003)

    # STT: only on sell side for intraday
    if is_intraday:
        stt = turnover * 0.00025 if is_sell else 0.0  # 0.025% sell-side
    else:
        # CNC delivery: 0.1% on both sides
        stt = turnover * 0.001

    # Exchange transaction charges (NSE)
    exchange = turnover * 0.0000345  # 0.00345%

    # GST: 18% on (brokerage + exchange charges)
    gst = (brokerage + exchange) * 0.18

    # SEBI charges: ₹10 per crore
    sebi = turnover * 0.000001  # 10/10^7

    # Stamp duty: 0.003% on buy side (approx, varies by state)
    stamp = turnover * 0.00003 if not is_sell else 0.0

    total = brokerage + stt + exchange + gst + sebi + stamp

    return TradeCost(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_charges=round(exchange, 2),
        gst=round(gst, 2),
        sebi_charges=round(sebi, 4),
        stamp_duty=round(stamp, 2),
        total=round(total, 2),
    )


def compute_round_trip_cost(qty: int, entry_price: float, exit_price: float, is_intraday: bool = True) -> float:
    """
    Compute total round-trip cost (buy + sell) for a completed trade.
    Returns total cost in ₹.
    """
    buy_turnover = qty * entry_price
    sell_turnover = qty * exit_price

    buy_cost = compute_leg_cost(buy_turnover, is_sell=False, is_intraday=is_intraday)
    sell_cost = compute_leg_cost(sell_turnover, is_sell=True, is_intraday=is_intraday)

    return buy_cost.total + sell_cost.total


def compute_breakeven_move_pct(qty: int, price: float, is_intraday: bool = True) -> float:
    """
    How much does the stock need to move (%) just to cover transaction costs?
    This is the minimum profitable move.
    """
    cost = compute_round_trip_cost(qty, price, price, is_intraday)
    trade_value = qty * price
    if trade_value <= 0:
        return float('inf')
    return round(cost / trade_value * 100, 4)


def is_trade_viable(qty: int, price: float, expected_move_pct: float, is_intraday: bool = True) -> bool:
    """
    Check if a trade is worth taking given expected move vs costs.
    Rule: expected move must be at least 3x the breakeven cost.
    """
    breakeven = compute_breakeven_move_pct(qty, price, is_intraday)
    return expected_move_pct >= breakeven * 3  # 3:1 reward:cost ratio minimum
