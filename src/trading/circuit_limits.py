"""
circuit_limits.py
-----------------
Indian NSE circuit limit awareness.

NSE stocks have daily price bands (circuit limits):
  - Most stocks: ±5%, ±10%, or ±20% from previous close
  - Index stocks (Nifty 50, Nifty Next 50): No circuit limits (but market-wide
    circuit breaker at 10%, 15%, 20% Nifty movement)
  - F&O stocks: No individual circuit limits

When a stock hits lower circuit:
  - Zero buy liquidity → your sell stop-loss order WILL NOT FILL
  - Stock can gap through multiple circuits across days

This module provides:
  1. Check if a stock is near circuit limits before entry
  2. Monitor distance to circuit during open positions
  3. Force-exit warnings when approaching circuit
"""
from dataclasses import dataclass
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)

# F&O stocks don't have individual circuit limits
# This is a subset — in production, load from NSE's circulars
FNO_STOCKS = {
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL",
    "ITC", "KOTAKBANK", "LT", "HINDUNILVR", "AXISBANK", "BAJFINANCE",
    "MARUTI", "NTPC", "ONGC", "TATAMOTORS", "TATASTEEL", "SUNPHARMA",
    "WIPRO", "HCLTECH", "ADANIENT", "ADANIPORTS", "ASIANPAINT",
    "BAJAJFINSV", "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB",
    "DRREDDY", "EICHERMOT", "GRASIM", "HEROMOTOCO", "HINDALCO",
    "INDUSINDBK", "JSWSTEEL", "M&M", "NESTLEIND", "POWERGRID",
    "SBILIFE", "SHRIRAMFIN", "TATACONSUM", "TECHM", "TITAN",
    "ULTRACEMCO", "UPL", "WIPRO", "APOLLOHOSP", "BAJAJ-AUTO",
}


@dataclass
class CircuitInfo:
    symbol: str
    prev_close: float
    band_pct: float          # 5, 10, or 20
    upper_circuit: float
    lower_circuit: float
    has_circuit: bool         # False for F&O stocks
    distance_to_upper_pct: float  # from current price
    distance_to_lower_pct: float  # from current price


def get_circuit_band(symbol: str) -> float:
    """
    Return the circuit band percentage for a symbol.
    F&O stocks: no individual circuit → return 0.
    Others: default to 20% (most common for listed stocks).

    In production, this should query NSE's daily circuit file.
    """
    if symbol.upper() in FNO_STOCKS:
        return 0.0  # No individual circuit
    # Default bands — should be loaded from NSE data in production
    return 20.0


def compute_circuit_limits(
    symbol: str,
    prev_close: float,
    current_price: float,
) -> CircuitInfo:
    """
    Compute circuit limits and distance from current price.
    """
    band = get_circuit_band(symbol)

    if band == 0:
        return CircuitInfo(
            symbol=symbol,
            prev_close=prev_close,
            band_pct=0,
            upper_circuit=0,
            lower_circuit=0,
            has_circuit=False,
            distance_to_upper_pct=99.0,
            distance_to_lower_pct=99.0,
        )

    upper = round(prev_close * (1 + band / 100), 2)
    lower = round(prev_close * (1 - band / 100), 2)

    dist_upper = ((upper - current_price) / current_price * 100) if current_price > 0 else 0
    dist_lower = ((current_price - lower) / current_price * 100) if current_price > 0 else 0

    return CircuitInfo(
        symbol=symbol,
        prev_close=prev_close,
        band_pct=band,
        upper_circuit=upper,
        lower_circuit=lower,
        has_circuit=True,
        distance_to_upper_pct=round(dist_upper, 2),
        distance_to_lower_pct=round(dist_lower, 2),
    )


def is_safe_to_enter_long(
    symbol: str,
    prev_close: float,
    current_price: float,
    min_distance_pct: float = 3.0,
) -> tuple:
    """
    Check if it's safe to enter a LONG position.
    Returns (is_safe, reason).

    Unsafe if:
      - Stock already near upper circuit (can't go higher → trapped)
      - Stock near lower circuit (if it hits, can't exit)
    """
    info = compute_circuit_limits(symbol, prev_close, current_price)

    if not info.has_circuit:
        return True, "F&O stock, no individual circuit limits"

    if info.distance_to_upper_pct < min_distance_pct:
        return False, f"Too close to upper circuit ({info.distance_to_upper_pct:.1f}% away). Max upside limited."

    if info.distance_to_lower_pct < min_distance_pct:
        return False, f"Too close to lower circuit ({info.distance_to_lower_pct:.1f}% away). Exit risk if circuit hits."

    return True, f"Circuit safe: {info.distance_to_lower_pct:.1f}% to lower, {info.distance_to_upper_pct:.1f}% to upper"


def is_safe_to_enter_short(
    symbol: str,
    prev_close: float,
    current_price: float,
    min_distance_pct: float = 3.0,
) -> tuple:
    """
    Check if it's safe to enter a SHORT position.
    """
    info = compute_circuit_limits(symbol, prev_close, current_price)

    if not info.has_circuit:
        return True, "F&O stock, no individual circuit limits"

    if info.distance_to_lower_pct < min_distance_pct:
        return False, f"Too close to lower circuit ({info.distance_to_lower_pct:.1f}% away). Max downside limited."

    if info.distance_to_upper_pct < min_distance_pct:
        return False, f"Too close to upper circuit ({info.distance_to_upper_pct:.1f}% away). Cover risk if circuit hits."

    return True, f"Circuit safe: {info.distance_to_lower_pct:.1f}% to lower, {info.distance_to_upper_pct:.1f}% to upper"
