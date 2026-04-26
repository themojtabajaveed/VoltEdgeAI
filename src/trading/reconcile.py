"""
reconcile.py — Kite position reconciliation on runner startup.

On startup, queries Kite for any open MIS positions and injects them into
the PositionBook so ExitEngine can monitor and stop them.  This closes the
crash-recovery gap: if the runner dies mid-session with open positions, a
restart will pick those positions back up rather than leaving them unmonitored
until Kite's own 15:30 MIS auto-squareoff.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from src.trading.positions import PositionBook

if TYPE_CHECKING:
    from src.brokers.zerodha_client import ZerodhaClient

logger = logging.getLogger(__name__)


def reconcile_positions(
    positions: PositionBook,
    zerodha_client: "ZerodhaClient | None",
    dry_run: bool,
) -> None:
    """
    On runner startup, query Kite for any open MIS positions and inject them
    into the PositionBook so ExitEngine can monitor and stop them.

    Only runs when dry_run=False and zerodha_client is not None.
    Safe to call with an already-populated PositionBook (skips symbols
    already present).
    """
    if dry_run or zerodha_client is None:
        logger.info("[RECONCILE] Skipped (dry_run=True or no live client).")
        return

    open_kite = zerodha_client.get_open_positions()
    if not open_kite:
        logger.info("[RECONCILE] No open Kite MIS positions found.")
        return

    recovered = 0
    for kp in open_kite:
        symbol = kp.get("tradingsymbol", "")
        qty    = int(kp.get("quantity", 0))
        price  = float(kp.get("average_price", 0.0))

        # Skip if already tracked
        if positions.get_position(symbol) is not None:
            logger.info(f"[RECONCILE] {symbol} already in PositionBook — skip.")
            continue

        if qty == 0 or price == 0.0:
            logger.warning(f"[RECONCILE] {symbol} has qty={qty} price={price} — skip.")
            continue

        side = "LONG" if qty > 0 else "SHORT"
        now  = datetime.now()

        # Build a minimal Position and inject it directly using from_dict so
        # all fields are typed correctly.  PositionBook.from_dict expects
        # {symbol: pos_dict} (flat — no extra nesting key).
        pos_dict = {
            "id":               f"RECOVERED_{symbol}_{now.strftime('%H%M%S')}",
            "symbol":           symbol,
            "side":             side,
            "total_qty":        abs(qty),
            "avg_price":        price,
            "realized_pnl":     0.0,
            "entry_time":       now.isoformat(),
            "last_update_time": now.isoformat(),
            "mode":             "INTRADAY",
            "strategy":         "RECOVERED",
            "broker_order_ids": [],
        }
        recovered_book = PositionBook.from_dict({symbol: pos_dict})
        recovered_pos  = recovered_book.get_position(symbol)

        if recovered_pos is not None:
            positions._positions[symbol] = recovered_pos
            logger.warning(
                f"[RECONCILE] RECOVERED {side} {abs(qty)} {symbol} "
                f"@ avg ₹{price:.2f} — now monitored by ExitEngine."
            )
            recovered += 1

    logger.info(f"[RECONCILE] Done. {recovered} position(s) recovered.")
