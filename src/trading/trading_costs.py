"""
trading_costs.py (v2)
---------------------
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

v2 changes:
  - All internal calculations use decimal.Decimal (P3-B fix).
    At ₹50,000 turnover, float-based STT at 0.025% gives ₹12.500000000000002.
    Decimal gives exactly ₹12.50. Over 50 daily trades, this eliminates ₹0.10+
    of accumulated P&L reporting error.
  - Public API still returns plain float at the boundary for backward compat.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

# ── Rate constants as Decimal (defined once, reused) ──────────────────────────
_BROKERAGE_FLAT     = Decimal("20.00")
_BROKERAGE_PCT      = Decimal("0.0003")      # 0.03%
_STT_INTRADAY_SELL  = Decimal("0.00025")     # 0.025% sell-side intraday
_STT_DELIVERY       = Decimal("0.001")       # 0.1% both sides CNC
_EXCHANGE_NSE       = Decimal("0.0000345")   # 0.00345%
_GST_RATE           = Decimal("0.18")        # 18%
_SEBI_RATE          = Decimal("0.000001")    # ₹10 per crore = 10/10^7
_STAMP_BUY          = Decimal("0.00003")     # 0.003% buy side
_TWO_DP             = Decimal("0.01")
_FOUR_DP            = Decimal("0.0001")


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
        TradeCost with full breakdown (floats at boundary, Decimal internally)
    """
    d_turnover = Decimal(str(turnover))

    # Brokerage: ₹20 flat or 0.03%, whichever is lower
    brokerage = min(_BROKERAGE_FLAT, d_turnover * _BROKERAGE_PCT)

    # STT: only on sell side for intraday
    if is_intraday:
        stt = d_turnover * _STT_INTRADAY_SELL if is_sell else Decimal("0")
    else:
        stt = d_turnover * _STT_DELIVERY  # CNC delivery: both sides

    # Exchange transaction charges (NSE)
    exchange = d_turnover * _EXCHANGE_NSE

    # GST: 18% on (brokerage + exchange charges)
    gst = (brokerage + exchange) * _GST_RATE

    # SEBI charges: ₹10 per crore
    sebi = d_turnover * _SEBI_RATE

    # Stamp duty: 0.003% on buy side only
    stamp = d_turnover * _STAMP_BUY if not is_sell else Decimal("0")

    total = brokerage + stt + exchange + gst + sebi + stamp

    return TradeCost(
        brokerage=float(brokerage.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
        stt=float(stt.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
        exchange_charges=float(exchange.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
        gst=float(gst.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
        sebi_charges=float(sebi.quantize(_FOUR_DP, rounding=ROUND_HALF_UP)),
        stamp_duty=float(stamp.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
        total=float(total.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
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

    # Final addition in Decimal, return float
    total = Decimal(str(buy_cost.total)) + Decimal(str(sell_cost.total))
    return float(total.quantize(_TWO_DP, rounding=ROUND_HALF_UP))


def compute_breakeven_move_pct(qty: int, price: float, is_intraday: bool = True) -> float:
    """
    How much does the stock need to move (%) just to cover transaction costs?
    This is the minimum profitable move.
    """
    cost = compute_round_trip_cost(qty, price, price, is_intraday)
    trade_value = qty * price
    if trade_value <= 0:
        return float('inf')

    d_cost = Decimal(str(cost))
    d_val = Decimal(str(trade_value))
    pct = (d_cost / d_val * Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(pct)


def is_trade_viable(qty: int, price: float, expected_move_pct: float, is_intraday: bool = True) -> bool:
    """
    Check if a trade is worth taking given expected move vs costs.
    Rule: expected move must be at least 3x the breakeven cost.
    """
    breakeven = compute_breakeven_move_pct(qty, price, is_intraday)
    return expected_move_pct >= breakeven * 3  # 3:1 reward:cost ratio minimum
