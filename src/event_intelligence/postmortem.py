"""Nightly postmortem — directional accuracy + parse coverage of shadow decisions.

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
      "rolling_5d_accuracy": 0.XX, # 5-day MA; None if < 5 days of data
      "false_positive_dawn": K,    # would-have-routed DAWN where direction wrong
      "parse_coverage": {
        "xbrl_pct": 0.XX,          # % of signals with parse_status XBRL
        "pdf_table_pct": 0.XX,
        "heuristic_pct": 0.XX,
        "total_signals": N,
      },
      "alarms": [...],             # e.g. ["100_PCT_HEURISTIC", "ACCURACY_BELOW_50"]
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


def _load_earnings_signals(date_str: str, data_dir: str) -> List[Dict[str, Any]]:
    path = os.path.join(data_dir, f"earnings_signals_{date_str}.json")
    return read_records(path)


def _compute_parse_coverage(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Count parse_status distribution across all signals for the day."""
    if not signals:
        return {"xbrl_pct": None, "pdf_table_pct": None, "heuristic_pct": None, "total_signals": 0}
    n = len(signals)
    counts: Dict[str, int] = {}
    for s in signals:
        status = (s.get("parse_status") or "HEURISTIC").upper()
        counts[status] = counts.get(status, 0) + 1

    def pct(*keys: str) -> float:
        return sum(counts.get(k, 0) for k in keys) / n

    return {
        # PDF_TABLE/PDF_REGEX are current values (from 2026-05-03+).
        # Legacy SUCCESS/PARTIAL (pre-rename) are counted separately in
        # by_status — we do NOT merge them into pdf_table_pct because
        # legacy SUCCESS could be either table or regex sourced.
        # XBRL is always 0: NSE /api/xbrl metadata enrichment never writes
        # parse_status; only the PDF extraction path does.
        "xbrl_pct": 0.0,
        "pdf_table_pct": pct("PDF_TABLE"),
        "pdf_regex_pct": pct("PDF_REGEX"),
        "legacy_structured_pct": pct("SUCCESS", "PARTIAL"),  # pre-rename records
        "heuristic_pct": pct("HEURISTIC"),
        "total_signals": n,
        "by_status": counts,
    }


def _check_heuristic_alarm(signals: List[Dict[str, Any]], window_hours: int = 4) -> bool:
    """Return True if ALL signals in the last `window_hours` are heuristic-only."""
    if not signals:
        return False
    cutoff = datetime.now(IST) - timedelta(hours=window_hours)
    recent = []
    for s in signals:
        ts_str = s.get("classified_at") or s.get("filed_at") or ""
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts >= cutoff:
                recent.append(s)
        except (ValueError, TypeError):
            pass
    if not recent:
        return False
    return all(
        (s.get("parse_status") or "HEURISTIC").upper() == "HEURISTIC"
        for s in recent
    )


def _compute_rolling_accuracy(data_dir: str, n_days: int = 5) -> Optional[float]:
    """Load last n_days of accuracy files and return the rolling MA.

    Returns None if fewer than n_days of data exist.
    """
    accuracies: List[float] = []
    today = datetime.now(IST).date()
    for offset in range(1, n_days + 1):
        day_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = daily_path("event_intel_accuracy", data_dir=data_dir, date_str=day_str)
        try:
            with open(path) as f:
                data = json.load(f)
            acc = data.get("directional_accuracy")
            if acc is not None:
                accuracies.append(float(acc))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    if len(accuracies) < n_days:
        return None
    return sum(accuracies) / len(accuracies)


def run(date_str: Optional[str] = None, data_dir: str = "data") -> Dict[str, Any]:
    if date_str is None:
        date_str = datetime.now(IST).strftime("%Y-%m-%d")

    decisions = _load_shadow_decisions(date_str, data_dir)
    signals = _load_earnings_signals(date_str, data_dir)

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

    today_accuracy: Optional[float] = (correct / scored) if scored else None
    parse_coverage = _compute_parse_coverage(signals)
    rolling_acc = _compute_rolling_accuracy(data_dir)

    # ── Alarms ────────────────────────────────────────────────────────
    alarms: List[str] = []

    if _check_heuristic_alarm(signals, window_hours=4):
        alarms.append("100_PCT_HEURISTIC_4H")
        logger.warning("[Postmortem] ALARM: all signals in last 4h are heuristic-only")

    if today_accuracy is not None and today_accuracy < 0.50:
        alarms.append("ACCURACY_BELOW_50")
        logger.warning("[Postmortem] ALARM: today accuracy %.1f%% < 50%%", today_accuracy * 100)

    if rolling_acc is not None and rolling_acc < 0.50:
        alarms.append("ROLLING_5D_ACCURACY_BELOW_50")
        logger.warning("[Postmortem] ALARM: 5-day rolling accuracy %.1f%% < 50%%", rolling_acc * 100)

    heuristic_pct = parse_coverage.get("heuristic_pct") or 0.0
    if heuristic_pct >= 1.0 and parse_coverage.get("total_signals", 0) >= 5:
        if "100_PCT_HEURISTIC_4H" not in alarms:
            alarms.append("100_PCT_HEURISTIC_DAY")
            logger.warning("[Postmortem] ALARM: 100%% of today's signals are heuristic-only")

    summary = {
        "date": date_str,
        "total_decisions": len(decisions),
        "scored": scored,
        "directional_accuracy": today_accuracy,
        "rolling_5d_accuracy": rolling_acc,
        "false_positive_dawn": false_positive_dawn,
        "parse_coverage": parse_coverage,
        "alarms": alarms,
        "details": details,
    }

    out_path = daily_path("event_intel_accuracy", data_dir=data_dir, date_str=date_str)
    atomic_write_json(out_path, summary)
    logger.info(
        "[Postmortem] %s: %d decisions, %d scored, accuracy=%s, rolling_5d=%s, "
        "heuristic_pct=%.0f%%, alarms=%s",
        date_str, len(decisions), scored,
        f"{today_accuracy:.2%}" if today_accuracy is not None else "N/A",
        f"{rolling_acc:.2%}" if rolling_acc is not None else "N/A",
        (heuristic_pct * 100),
        alarms or "none",
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
