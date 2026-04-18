"""
shadow_book.py — Phase 8h dry-run shadow position tracker.

Mirrors dry-run trade entries into a separate PositionBook and logs
exit events to data/sl_shadow_YYYY-MM-DD.json for SL effectiveness analysis.

The shadow book is completely isolated from the live PositionBook so there
is zero chance of a live/dry-run mix. It writes through to disk on every
mutation so process restarts don't lose the day's shadow state.

Post-market Section 14 report reads sl_shadow_YYYY-MM-DD.json and emits:
  - % exits by reason (STOP_LOSS, TRAILING_STOP, TIME_EXIT, PARTIAL_*, ORB_STOP)
  - avg R captured
  - % exits that would have hit rr_target
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.trading.positions import PositionBook, Position

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SHADOWS_DIR = Path("data")


def _shadow_path(for_date: Optional[date] = None) -> Path:
    d = for_date or date.today()
    return _SHADOWS_DIR / f"sl_shadow_{d.isoformat()}.json"


class ShadowBook:
    """
    Isolated PositionBook for dry-run positions.
    All mutations are written through to sl_shadow_YYYY-MM-DD.json.
    """

    def __init__(self, shadow_date: Optional[date] = None) -> None:
        self._date = shadow_date or date.today()
        self._book = PositionBook()
        self._closed: List[Dict[str, Any]] = []
        self._path = _shadow_path(self._date)
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                with open(self._path) as f:
                    data = json.load(f)
                self._closed = data.get("closed", [])
        except Exception as e:
            logger.warning(f"[SHADOW] load failed for {self._path}: {e}")

    def _persist(self) -> None:
        try:
            _SHADOWS_DIR.mkdir(parents=True, exist_ok=True)
            open_dict = self._book.to_dict()
            payload = {"date": self._date.isoformat(), "open": open_dict, "closed": self._closed}
            tmp = str(self._path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f, default=str, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning(f"[SHADOW] persist failed: {e}")

    def on_entry(
        self,
        symbol: str,
        qty: int,
        price: float,
        strategy: str,
        initial_stop_price: Optional[float],
        atr: Optional[float],
        strategy_config: Optional[Dict[str, Any]] = None,
        time_stop_ist: Optional[Any] = None,
        conviction_at_entry: float = 0.0,
    ) -> None:
        with self._lock:
            self._book.on_buy_fill(
                symbol=symbol, qty=qty, price=price,
                mode="INTRADAY", strategy=strategy,
                initial_stop_price=initial_stop_price, atr=atr,
                strategy_config=strategy_config,
                time_stop_ist=time_stop_ist,
                conviction_at_entry=conviction_at_entry,
            )
            self._persist()

    def on_exit(
        self,
        symbol: str,
        qty: int,
        exit_price: float,
        exit_reason: str,
        exit_ts: Optional[datetime] = None,
    ) -> None:
        with self._lock:
            pos = self._book.get_position(symbol)
            if pos is None:
                return
            entry_price = float(pos.avg_price)
            initial_stop = pos.initial_stop_price
            rr = pos.strategy_config.get("rr_target", 0.0) if pos.strategy_config else 0.0
            one_r = pos.risk_unit()
            if one_r > 0 and entry_price > 0:
                r_captured = (exit_price - entry_price) / one_r
            else:
                r_captured = 0.0
            pnl_pct = (exit_price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            would_hit_target = bool(rr > 0 and r_captured >= rr)
            pnl_at_stop = (
                (entry_price - initial_stop) / entry_price * -100.0
                if initial_stop and entry_price > 0 else 0.0
            )
            record = {
                "symbol": symbol,
                "strategy": pos.strategy,
                "entry_ts": pos.entry_time.isoformat(),
                "entry_price": entry_price,
                "initial_stop": initial_stop,
                "exit_ts": (exit_ts or datetime.now()).isoformat(),
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "r_captured": round(r_captured, 3),
                "pnl_pct": round(pnl_pct, 3),
                "pnl_at_stop_hypothetical": round(pnl_at_stop, 3),
                "would_have_hit_target_rr": would_hit_target,
                "conviction_at_entry": pos.conviction_at_entry,
            }
            self._closed.append(record)
            self._book.on_sell_fill(symbol, qty, exit_price)
            self._persist()

    def get_closed(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._closed)

    def get_open(self) -> List[Position]:
        with self._lock:
            return self._book.get_open_positions()


def generate_shadow_report_section(shadow_date: Optional[date] = None) -> str:
    """
    Phase 8h Section 14: produce a markdown summary of the day's shadow SL activity.
    Returns an empty string if no shadow data exists.
    """
    path = _shadow_path(shadow_date)
    if not path.exists():
        return ""
    try:
        with open(path) as f:
            data = json.load(f)
        closed = data.get("closed", [])
        if not closed:
            return "## 14. Stop-Loss Shadow (Phase 8 validation)\n\n_No shadow exits recorded today._\n"

        total = len(closed)
        by_reason: Dict[str, int] = {}
        r_values: List[float] = []
        hits = 0
        for rec in closed:
            reason = rec.get("exit_reason", "UNKNOWN")
            by_reason[reason] = by_reason.get(reason, 0) + 1
            r_values.append(float(rec.get("r_captured", 0.0)))
            if rec.get("would_have_hit_target_rr"):
                hits += 1

        avg_r = sum(r_values) / len(r_values) if r_values else 0.0
        target_pct = hits / total * 100 if total else 0.0
        reason_lines = "\n".join(
            f"  - {r}: {n} ({n/total:.0%})" for r, n in sorted(by_reason.items(), key=lambda x: -x[1])
        )
        return (
            "## 14. Stop-Loss Shadow (Phase 8 validation)\n\n"
            f"| Metric | Value |\n|---|---|\n"
            f"| Total exits today | {total} |\n"
            f"| Avg R captured | {avg_r:.2f}R |\n"
            f"| % hitting rr_target | {target_pct:.0f}% |\n\n"
            f"**Exits by reason:**\n{reason_lines}\n"
        )
    except Exception as e:
        logger.warning(f"[SHADOW] report section failed: {e}")
        return ""
