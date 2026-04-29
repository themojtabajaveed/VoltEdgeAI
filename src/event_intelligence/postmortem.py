"""Nightly postmortem — directional accuracy of shadow decisions.

For each shadow `RouteDecision` written today, fetch the symbol's
next-trading-day open vs filing-time price (or close vs close if the
event landed after-hours) and score the `direction_hint`:

    BUY  → correct iff next-day return > +0.5%
    SHORT → correct iff next-day return < -0.5%
    NEUTRAL → noise — excluded from accuracy denominator

Writes `data/event_intel_accuracy_YYYY-MM-DD.json` summarizing:
    {
      "date": "...",
      "total_decisions": N,
      "scored": M,                 # excludes NEUTRAL / no-price
      "directional_accuracy": 0.XX,
      "false_positive_dawn": K,    # would-have-routed DAWN where direction wrong
      "details": [...]
    }
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.event_intelligence.writers import atomic_write_json, daily_path, read_records

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def _load_shadow_decisions(date_str: str, data_dir: str) -> List[Dict[str, Any]]:
    path = os.path.join(data_dir, f"event_intel_shadow_router_{date_str}.json")
    return read_records(path)


def _next_day_return(symbol: str, filed_at: str) -> Optional[float]:
    """Best-effort yfinance lookup of next-trading-day return.

    Returns the percentage change, or None if data unavailable. We use a
    very narrow window (filed_at date through filed_at + 5 calendar days)
    and skip if yfinance is not installed — postmortem then logs the
    missing scoring and proceeds.
    """
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        logger.warning("[Postmortem] yfinance not installed — accuracy will be skipped")
        return None

    try:
        filed_dt = datetime.fromisoformat(filed_at)
    except ValueError:
        return None
    if filed_dt.tzinfo is None:
        filed_dt = filed_dt.replace(tzinfo=IST)

    start = filed_dt.date()
    end = start + timedelta(days=5)

    ticker_yf = symbol if "." in symbol else f"{symbol}.NS"
    try:
        df = yf.Ticker(ticker_yf).history(
            start=start.isoformat(), end=end.isoformat()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[Postmortem] yfinance fetch failed for %s: %s", symbol, e)
        return None

    if df is None or df.empty or len(df) < 2:
        return None

    # Reference price: closest bar at/after filed_dt (or close on filing day)
    # Compare against next-day close.
    closes = df["Close"].tolist()
    if len(closes) < 2:
        return None
    ref = closes[0]
    nxt = closes[1]
    if ref <= 0:
        return None
    return ((nxt - ref) / ref) * 100.0


def score_decision(decision: Dict[str, Any]) -> Optional[bool]:
    """Return True/False if direction was correct, None if not scorable."""
    direction = decision.get("direction_hint")
    if direction not in ("BUY", "SHORT"):
        return None

    ret = _next_day_return(decision["symbol"], decision["filed_at"])
    if ret is None:
        return None

    if direction == "BUY":
        return ret > 0.5
    return ret < -0.5


def run(date_str: Optional[str] = None, data_dir: str = "data") -> Dict[str, Any]:
    if date_str is None:
        date_str = datetime.now(IST).strftime("%Y-%m-%d")

    decisions = _load_shadow_decisions(date_str, data_dir)
    scored = 0
    correct = 0
    false_positive_dawn = 0
    details: List[Dict[str, Any]] = []

    for d in decisions:
        result = score_decision(d)
        was_dawn = (
            isinstance(d.get("decision"), dict)
            and d["decision"].get("route") == "DAWN"
        )
        if result is not None:
            scored += 1
            if result:
                correct += 1
            elif was_dawn:
                false_positive_dawn += 1
        details.append({
            "event_id": d.get("event_id"),
            "symbol": d.get("symbol"),
            "direction_hint": d.get("direction_hint"),
            "would_be_route": (d.get("decision") or {}).get("route"),
            "confidence": (d.get("decision") or {}).get("confidence"),
            "scored": result is not None,
            "correct": result if result is not None else None,
        })

    summary = {
        "date": date_str,
        "total_decisions": len(decisions),
        "scored": scored,
        "directional_accuracy": (correct / scored) if scored else None,
        "false_positive_dawn": false_positive_dawn,
        "details": details,
    }

    out_path = daily_path("event_intel_accuracy", data_dir=data_dir, date_str=date_str)
    atomic_write_json(out_path, summary)
    logger.info(
        "[Postmortem] %s: %d decisions, %d scored, accuracy=%s, false_positive_dawn=%d",
        date_str, len(decisions), scored,
        f"{summary['directional_accuracy']:.2%}" if summary["directional_accuracy"] is not None else "N/A",
        false_positive_dawn,
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
