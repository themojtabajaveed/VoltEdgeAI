"""
live_trade_tracker.py — Phase 7 live-order persistence layer.

Records every REAL order placed via Zerodha into data/live_trades_YYYY-MM-DD.json,
and exposes counters used by the 5-gate safety system (daily trade count,
open-position count). Dry-run orders are NOT written here.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import zoneinfo

logger = logging.getLogger(__name__)

try:
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except zoneinfo.ZoneInfoNotFoundError:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

DATA_DIR = "data"


def _today_path(today: Optional[datetime] = None) -> Path:
    today = today or datetime.now(IST)
    return Path(DATA_DIR) / f"live_trades_{today.strftime('%Y-%m-%d')}.json"


def _read(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"[live_trade_tracker] read {path} failed: {e}")
        return []


def _write(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(records, f, indent=2)


def record_live_trade(
    symbol: str,
    order_id: str,
    quantity: int,
    entry_price: float,
    signal: Any = None,
    route: str = "UNROUTED",
    confidence: float = 0.0,
    conviction_score: float = 0.0,
) -> None:
    """Append a live-order record to today's file. Creates the file if missing."""
    path = _today_path()
    records = _read(path)

    if signal is not None:
        try:
            route = getattr(signal, "route", route) or route
            confidence = float(getattr(signal, "confidence", confidence) or confidence)
            cs = getattr(signal, "conviction_score", None)
            if cs is not None:
                conviction_score = float(getattr(cs, "total", conviction_score) or conviction_score)
        except (AttributeError, TypeError, ValueError):
            pass

    record = {
        "symbol": symbol,
        "order_id": str(order_id),
        "quantity": int(quantity),
        "entry_price": float(entry_price),
        "route": route,
        "confidence": float(confidence),
        "conviction_score": float(conviction_score),
        "placed_at": datetime.now(IST).isoformat(),
        "status": "OPEN",
        "exit_price": None,
        "exit_at": None,
        "pnl_pct": None,
    }
    records.append(record)
    _write(path, records)


def get_trades_placed_today() -> int:
    """Count of live orders placed today."""
    return len(_read(_today_path()))


def get_open_positions() -> int:
    """Count of live orders still OPEN today."""
    return sum(1 for r in _read(_today_path()) if r.get("status") == "OPEN")


def get_open_trades_today() -> List[Dict[str, Any]]:
    """Full records of still-open live trades for today."""
    return [r for r in _read(_today_path()) if r.get("status") == "OPEN"]


def get_all_trades_today() -> List[Dict[str, Any]]:
    """Every live-order record written today, OPEN or CLOSED."""
    return list(_read(_today_path()))


def update_trade_status(
    order_id: str,
    exit_price: float,
    exit_at: Optional[str] = None,
) -> bool:
    """
    Close a trade by order_id: sets status=CLOSED, exit_price, exit_at, and
    computes pnl_pct = (exit_price - entry_price) / entry_price * 100.
    Returns True if a record was updated, False if not found.
    """
    path = _today_path()
    records = _read(path)
    if not records:
        return False

    target = str(order_id)
    updated = False
    for rec in records:
        if str(rec.get("order_id")) == target:
            entry = float(rec.get("entry_price") or 0.0)
            rec["status"] = "CLOSED"
            rec["exit_price"] = float(exit_price)
            rec["exit_at"] = exit_at or datetime.now(IST).isoformat()
            if entry > 0:
                rec["pnl_pct"] = round(((float(exit_price) - entry) / entry) * 100.0, 4)
            else:
                rec["pnl_pct"] = 0.0
            updated = True
            break

    if updated:
        _write(path, records)
    return updated
