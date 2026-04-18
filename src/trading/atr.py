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
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


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


def compute_daily_atr_from_cache(symbol: str, ltp: float = 0.0) -> Optional[float]:
    """
    Phase 8e: best-effort ATR estimate for DAWN's 09:15 blind entry,
    before any intraday bars are available.

    Tries in order:
      1. SQLite history DB (data/history.db): compute Wilder ATR(14) from
         the last 20 daily bars.
      2. PreMarketSignals high_30d proxy: rough ATR ≈ abs(high_30d - prev_close) × 0.25
         (a conservative fraction of the 30-day range).
      3. Hard fallback: ltp × 1.5% — always returns a non-zero number.

    Returns None only if ltp <= 0 and no data source is available.
    """
    if ltp < 0:
        ltp = 0.0

    # ── 1. SQLite history DB ─────────────────────────────────────────────
    try:
        import sqlite3, os
        from datetime import datetime, timedelta
        db_path = "data/history.db"
        if os.path.exists(db_path):
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT high, low, close FROM ohlcv "
                    "WHERE symbol=? AND interval='day' "
                    "ORDER BY timestamp DESC LIMIT 21",
                    (symbol,),
                ).fetchall()
            if len(rows) >= 15:
                rows.reverse()  # oldest first

                class _Bar:
                    def __init__(self, h, l, c):
                        self.high = h; self.low = l; self.close = c

                bars = [_Bar(*r) for r in rows]
                atr = compute_atr(bars, period=14)
                if atr > 0:
                    logger.debug(
                        f"[ATR] {symbol} daily ATR(14) from history.db = {atr:.2f}"
                    )
                    return atr
    except Exception as hist_e:
        logger.debug(f"[ATR] {symbol} history.db failed: {hist_e}")

    # ── 2. PreMarketSignals high_30d proxy ───────────────────────────────
    try:
        from src.data_ingestion.pre_market_data import fetch_all_premarket_data
        cache = fetch_all_premarket_data([symbol])
        sig = cache.get(symbol)
        if sig is not None:
            h30 = getattr(sig, "high_30d", 0.0) or 0.0
            prev_c = getattr(sig, "prev_close", 0.0) or 0.0
            if h30 > 0 and prev_c > 0:
                atr_proxy = abs(h30 - prev_c) * 0.25
                if atr_proxy > 0:
                    logger.debug(
                        f"[ATR] {symbol} 30d-range proxy ATR = {atr_proxy:.2f}"
                    )
                    return atr_proxy
    except Exception as pm_e:
        logger.debug(f"[ATR] {symbol} premarket proxy failed: {pm_e}")

    # ── 3. Hard fallback: 1.5% of LTP ────────────────────────────────────
    if ltp > 0:
        fallback = round(ltp * 0.015, 2)
        logger.debug(
            f"[ATR] {symbol} using LTP×1.5% fallback ATR = {fallback:.2f}"
        )
        return fallback

    return None
