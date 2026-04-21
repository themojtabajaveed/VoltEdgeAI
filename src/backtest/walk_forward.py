"""
walk_forward.py — train/test split backtest for VoltEdgeAI.

Splits the requested period into:
  - In-sample (IS):  first split_pct% of days (default 67%)
  - Out-of-sample (OOS): remaining days

Runs run_backtest() on each period independently.
Reports IS metrics, OOS metrics, and IS-OOS delta for key metrics.
The delta is the system's overfit estimate.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

from src.backtest.engine import run_backtest

logger = logging.getLogger(__name__)


def run_walk_forward(
    total_days: int = 90,
    split_pct: float = 0.67,
    persist: bool = True,
) -> dict:
    """
    Returns dict with keys: is_result, oos_result, delta
    delta contains: win_rate_delta, avg_pnl_delta, profit_factor_delta
    Positive delta = IS better than OOS (overfit signal)
    Negative delta = OOS better than IS (unusual, check for bugs)
    """
    is_days = int(total_days * split_pct)
    oos_days = total_days - is_days

    today = date.today()
    is_start_date = today - timedelta(days=is_days)

    logger.info("[WALK-FORWARD] total_days=%d, split_pct=%.2f (IS=%dd, OOS=%dd)", total_days, split_pct, is_days, oos_days)

    is_result = run_backtest(days=is_days, persist=False)
    oos_result = run_backtest(days=oos_days, end_date=is_start_date, persist=False)

    def safe_diff(a: dict, b: dict, key: str) -> float:
        v1 = a.get(key)
        v2 = b.get(key)
        if v1 is None or v2 is None:
            return 0.0
        return float(v1) - float(v2)

    delta = {
        "win_rate_delta": safe_diff(is_result, oos_result, "win_rate"),
        "avg_pnl_delta": safe_diff(is_result, oos_result, "avg_pnl_pct"),
    }

    summary = {
        "is_result": is_result,
        "oos_result": oos_result,
        "delta": delta,
    }

    if persist:
        os.makedirs("data", exist_ok=True)
        path = f"data/walk_forward_{today.isoformat()}.json"
        try:
            with open(path, "w") as f:
                json.dump(summary, f, indent=2, default=str)
            logger.info("[WALK-FORWARD] Persisted to %s", path)
        except Exception as e:
            logger.error("[WALK-FORWARD] Failed to persist summary: %s", e)

    return summary
