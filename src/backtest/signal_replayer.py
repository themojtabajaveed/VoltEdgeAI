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
from src.trading.trading_costs import compute_round_trip_cost
from src.config_loader import get_per_trade_risk, get_backtest_slippage_config

logger = logging.getLogger(__name__)


def build_watchlist_entry(
    symbol: str, candle: dict, prev_candles: list[dict]
) -> WatchlistEntry:
    """Build a WatchlistEntry from a historical daily candle.

    momentum_score  = (open - prev_close) / prev_close  (pre-market gap, decimal)
    avg_volume_20d  = mean of prev_candles volumes; 0 if fewer than 5 samples
    """
    open_p = float(candle["open"])
    
    prev_close_p = float(prev_candles[-1]["close"]) if prev_candles and "close" in prev_candles[-1] else open_p
    prev_vol = int(prev_candles[-1].get("volume", 0)) if prev_candles else 0

    momentum = (open_p - prev_close_p) / prev_close_p if prev_close_p != 0.0 else 0.0

    volumes = [int(c["volume"]) for c in prev_candles if "volume" in c]
    avg_vol = int(mean(volumes)) if len(volumes) >= 5 else 0

    if avg_vol > 0:
        ratio = prev_vol / avg_vol
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

    entry.route = route
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

    qty = max(1, int(get_per_trade_risk() / open_p))
    cost_inr = compute_round_trip_cost(
        qty=qty,
        entry_price=open_p,
        exit_price=exit_price,
        is_intraday=True,
    )
    cost_pct = (cost_inr / (qty * open_p)) * 100.0

    slip_cfg = get_backtest_slippage_config()
    entry_slip_pct = slip_cfg["slippage_entry_bps"] / 100.0
    if result == "TARGET_HIT":
        exit_slip_pct = slip_cfg["slippage_exit_target_bps"] / 100.0
    elif result == "SL_HIT":
        exit_slip_pct = slip_cfg["slippage_exit_stop_bps"] / 100.0
    else:
        exit_slip_pct = slip_cfg["slippage_exit_neutral_bps"] / 100.0
    
    total_slip_pct = entry_slip_pct + exit_slip_pct

    gross_pnl_pct = (exit_price - open_p) / open_p * 100.0
    pnl_pct = round(gross_pnl_pct - cost_pct - total_slip_pct, 4)

    return {
        "symbol": symbol, "date": date_str,
        "route": route, "confidence": confidence,
        "conviction": conviction_score.total,
        "skipped": False, "result": result,
        "entry_price": open_p,
        "target_price": round(target_price, 4),
        "sl_price": round(sl_price, 4),
        "exit_price": round(exit_price, 4),
        "gross_pnl_pct": round(gross_pnl_pct, 4),
        "cost_pct": round(cost_pct, 4),
        "slip_pct": round(total_slip_pct, 4),
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
