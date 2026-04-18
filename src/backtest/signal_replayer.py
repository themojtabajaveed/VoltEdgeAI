"""
signal_replayer.py — Historical signal replay pipeline
-------------------------------------------------------
Constructs WatchlistEntry objects from daily candles and runs each through
the router + conviction gate to simulate a live trading day.
"""
from __future__ import annotations

import logging
from statistics import mean

from src.strategies.base import ConvictionScore, WatchlistEntry

logger = logging.getLogger(__name__)


def build_watchlist_entry(
    symbol: str, candle: dict, prev_candles: list[dict]
) -> WatchlistEntry:
    """Build a WatchlistEntry from a historical daily candle.

    momentum_score  = (close - open) / open  (intraday move, decimal)
    avg_volume_20d  = mean of prev_candles volumes; 0 if fewer than 5 samples
    """
    open_p = float(candle["open"])
    close_p = float(candle["close"])
    curr_vol = int(candle.get("volume", 0))

    momentum = (close_p - open_p) / open_p if open_p != 0.0 else 0.0

    volumes = [int(c["volume"]) for c in prev_candles if "volume" in c]
    avg_vol = int(mean(volumes)) if len(volumes) >= 5 else 0

    if avg_vol > 0:
        ratio = curr_vol / avg_vol
        vol_signal = "SURGE" if ratio >= 2.0 else "ELEVATED" if ratio >= 1.5 else "NORMAL"
    else:
        vol_signal = "NORMAL"

    entry = WatchlistEntry(
        symbol=symbol,
        direction="BUY",
        event_summary="",
        urgency=round(abs(momentum) * 10, 2),
        # gap_pct stores the intraday move as %; momentum_score property derives from it
        gap_pct=round(momentum * 100.0, 4),
        avg_volume_20d=avg_vol,
        volume_signal=vol_signal,
        filing_category="",
        filing_urgency=0.0,
        catalyst_strength="LOW",
        sector_momentum="NEUTRAL",
        technical_setup="NEUTRAL",
        fii_flow="NEUTRAL",
    )
    return entry


def replay_day(
    symbol: str,
    candle: dict,
    prev_candles: list[dict],
    router,
    conviction_engine,
) -> dict:
    """Simulate one trading day for *symbol*.

    Router and conviction_engine are duck-typed:
      router.classify(entry)          → (route: str, confidence: float)
      conviction_engine.score(entry)  → ConvictionScore with .total

    Returns a result dict.  skipped=True means no trade was taken.
    """
    from src.config_loader import (
        get_conviction_threshold,
        get_backtest_target_pct,
        get_backtest_sl_pct,
    )

    date_str = str(candle.get("date", ""))
    entry = build_watchlist_entry(symbol, candle, prev_candles)

    route, confidence = router.classify(entry)

    if route == "UNROUTED":
        return {
            "symbol": symbol, "date": date_str,
            "route": "UNROUTED", "confidence": confidence,
            "skipped": True,
        }

    conviction_score: ConvictionScore = conviction_engine.score(entry)
    threshold = get_conviction_threshold()

    if conviction_score.total < threshold:
        return {
            "symbol": symbol, "date": date_str,
            "route": route, "confidence": confidence,
            "conviction": conviction_score.total,
            "skipped": True, "skip_reason": "CONVICTION_GATE",
        }

    target_pct = get_backtest_target_pct()
    sl_pct = get_backtest_sl_pct()

    open_p = float(candle["open"])
    high_p = float(candle["high"])
    low_p = float(candle["low"])
    close_p = float(candle["close"])

    target_price = open_p * (1.0 + target_pct / 100.0)
    sl_price = open_p * (1.0 - sl_pct / 100.0)

    if high_p >= target_price:
        result, exit_price = "TARGET_HIT", target_price
    elif low_p <= sl_price:
        result, exit_price = "SL_HIT", sl_price
    else:
        result, exit_price = "NEUTRAL", close_p

    pnl_pct = round((exit_price - open_p) / open_p * 100.0, 4)

    return {
        "symbol": symbol, "date": date_str,
        "route": route, "confidence": confidence,
        "conviction": conviction_score.total,
        "skipped": False, "result": result,
        "entry_price": open_p,
        "target_price": round(target_price, 4),
        "sl_price": round(sl_price, 4),
        "exit_price": round(exit_price, 4),
        "pnl_pct": pnl_pct,
    }


def replay_symbol(
    symbol: str,
    candles: list[dict],
    router,
    conviction_engine,
) -> list[dict]:
    """Replay all trading days for *symbol* in chronological order."""
    results: list[dict] = []
    for i, candle in enumerate(candles):
        prev_candles = candles[max(0, i - 20):i]
        results.append(replay_day(symbol, candle, prev_candles, router, conviction_engine))
    return results
