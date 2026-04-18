"""
counterfactual.py — Phase 5 counterfactual analysis engine.

For each symbol in data/hydra_shadows_YYYY-MM-DD.json, determine what actually
happened EOD and whether routing to HYDRA (instead of DAWN) was the right call.

Verdicts
--------
CORRECT_ROUTE       Stock dropped ≤ sl_pct — HYDRA routing was correct, DAWN would have been stopped.
MISSED_OPPORTUNITY  Stock moved up ≥ target_pct — DAWN would likely have captured it.
NEUTRAL             Neither threshold hit — inconclusive.

Thresholds come from config.yaml:counterfactual (never hardcoded).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config_loader import get_cf_sl_pct, get_cf_target_pct

logger = logging.getLogger(__name__)

_DATA_DIR = Path("data")


def load_shadows(date_str: str) -> List[Dict[str, Any]]:
    """Load data/hydra_shadows_{date_str}.json. Missing file → []."""
    path = _DATA_DIR / f"hydra_shadows_{date_str}.json"
    if not path.exists():
        logger.warning(f"[Counterfactual] Shadow file not found: {path}")
        return []
    try:
        with open(path) as f:
            data = json.load(f) or []
        if not isinstance(data, list):
            logger.warning(f"[Counterfactual] {path} is not a list — returning []")
            return []
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"[Counterfactual] Failed to read {path}: {e}")
        return []


def fetch_eod_result(symbol: str, date_str: str) -> Dict[str, Any]:
    """
    Fetch EOD OHLCV for symbol on date_str. Uses yfinance (NSE .NS suffix),
    which is already a codebase dependency (see post_market_pipeline._fetch_eod_close_yfinance).

    Returns dict with keys: symbol, date, open, high, low, close, volume.
    On failure: {symbol, date, error: "fetch_failed"}.
    """
    try:
        import yfinance as yf
        from datetime import datetime, timedelta

        start = datetime.strptime(date_str, "%Y-%m-%d")
        end = start + timedelta(days=1)
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=False,
        )
        if hist is None or hist.empty:
            logger.warning(f"[Counterfactual] Empty yfinance history for {symbol} on {date_str}")
            return {"symbol": symbol, "date": date_str, "error": "fetch_failed"}
        row = hist.iloc[-1]
        return {
            "symbol": symbol,
            "date": date_str,
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]) if row.get("Volume") is not None else 0,
        }
    except Exception as e:
        logger.error(f"[Counterfactual] EOD fetch failed for {symbol} on {date_str}: {e}")
        return {"symbol": symbol, "date": date_str, "error": "fetch_failed"}


def score_shadow(shadow: Dict[str, Any], eod: Dict[str, Any]) -> Dict[str, Any]:
    """
    Score a single shadow against its EOD outcome.

    Returns an enriched dict with pct_move, direction, would_have_hit_target,
    would_have_hit_sl, and verdict.
    """
    target_pct = get_cf_target_pct()
    sl_pct = get_cf_sl_pct()

    symbol = shadow.get("symbol") or eod.get("symbol", "?")
    base: Dict[str, Any] = {
        "symbol": symbol,
        "confidence": float(shadow.get("confidence", 0.0) or 0.0),
        "route": shadow.get("route", "HYDRA"),
        "filing_category": shadow.get("filing_category"),
    }

    if eod.get("error") or "open" not in eod or "close" not in eod:
        base.update({
            "pct_move": None,
            "direction": "UNKNOWN",
            "would_have_hit_target": False,
            "would_have_hit_sl": False,
            "verdict": "UNKNOWN",
            "error": eod.get("error", "missing_eod_fields"),
        })
        return base

    open_p = float(eod["open"])
    close_p = float(eod["close"])
    if open_p <= 0:
        base.update({
            "pct_move": None,
            "direction": "UNKNOWN",
            "would_have_hit_target": False,
            "would_have_hit_sl": False,
            "verdict": "UNKNOWN",
            "error": "zero_open_price",
        })
        return base

    pct_move = (close_p - open_p) / open_p * 100.0
    direction = "UP" if pct_move > 0 else "DOWN"
    hit_target = pct_move >= target_pct
    hit_sl = pct_move <= sl_pct

    if hit_sl:
        verdict = "CORRECT_ROUTE"
    elif hit_target:
        verdict = "MISSED_OPPORTUNITY"
    else:
        verdict = "NEUTRAL"

    base.update({
        "open": open_p,
        "close": close_p,
        "high": float(eod.get("high", 0.0)),
        "low": float(eod.get("low", 0.0)),
        "volume": int(eod.get("volume", 0) or 0),
        "pct_move": round(pct_move, 3),
        "direction": direction,
        "would_have_hit_target": hit_target,
        "would_have_hit_sl": hit_sl,
        "verdict": verdict,
    })
    return base


def run_counterfactual(date_str: str) -> Dict[str, Any]:
    """
    Orchestrate: load_shadows → fetch_eod_result → score_shadow per symbol.

    Returns a summary dict:
      { date, total_shadows, correct_routes, missed_opportunities, neutral,
        router_accuracy, details: [scored shadow dicts] }
    """
    shadows = load_shadows(date_str)
    details: List[Dict[str, Any]] = []

    for sh in shadows:
        sym = sh.get("symbol")
        if not sym:
            continue
        eod = fetch_eod_result(sym, date_str)
        scored = score_shadow(sh, eod)
        details.append(scored)

    total = len(details)
    correct = sum(1 for d in details if d.get("verdict") == "CORRECT_ROUTE")
    missed = sum(1 for d in details if d.get("verdict") == "MISSED_OPPORTUNITY")
    neutral = sum(1 for d in details if d.get("verdict") == "NEUTRAL")
    router_accuracy = (correct / total) if total > 0 else 0.0

    summary = {
        "date": date_str,
        "total_shadows": total,
        "correct_routes": correct,
        "missed_opportunities": missed,
        "neutral": neutral,
        "router_accuracy": round(router_accuracy, 4),
        "details": details,
    }
    return summary


def persist_counterfactual(summary: Dict[str, Any]) -> Optional[str]:
    """Write summary to data/counterfactual_YYYY-MM-DD.json. Returns the path or None."""
    date_str = summary.get("date")
    if not date_str:
        return None
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _DATA_DIR / f"counterfactual_{date_str}.json"
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        return str(out_path)
    except OSError as e:
        logger.error(f"[Counterfactual] Persistence failed for {date_str}: {e}")
        return None


def load_counterfactual(date_str: str) -> Optional[Dict[str, Any]]:
    """Load a previously persisted counterfactual summary. None if missing."""
    path = _DATA_DIR / f"counterfactual_{date_str}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"[Counterfactual] Failed to read {path}: {e}")
        return None
