"""
atr.py
------
Computes the Average True Range (ATR) from a list of intraday bars.
Used to set volatility-adjusted stop-losses and position sizes.

Each bar object is expected to have: .high, .low, .close attributes.

Formula (Wilder's ATR):
    True Range (TR) = max(
        high - low,
        abs(high - prev_close),
        abs(low  - prev_close)
    )
    ATR_n = Wilder's smoothed average of last n TR values
"""
from typing import List


def compute_atr(bars: List, period: int = 14) -> float:
    """
    Compute ATR over `period` bars.
    `bars` should be sorted oldest-first.
    Returns 0.0 if there are not enough bars.
    """
    if len(bars) < period + 1:
        return 0.0

    # Calculate True Ranges
    trs = []
    for i in range(1, len(bars)):
        high  = bars[i].high
        low   = bars[i].low
        prev_close = bars[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if len(trs) < period:
        return 0.0

    # Wilder's smoothing: start with simple average, then smooth
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period

    return round(atr, 4)


def compute_stop_distance(atr: float, multiplier: float = 1.5) -> float:
    """Stop distance = multiplier × ATR. Standard for intraday: 1.5×."""
    return round(atr * multiplier, 4)


def compute_atr_position_size(
    capital_at_risk: float,
    stop_distance: float,
) -> int:
    """
    Risk-normalised position size.
    shares = capital_at_risk / stop_distance

    Example: risk ₹500 per trade, stop_distance = ₹10 → 50 shares.
    """
    if stop_distance <= 0:
        return 0
    return max(1, int(capital_at_risk / stop_distance))
