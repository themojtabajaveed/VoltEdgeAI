"""
exit_engine.py (v2)
-------------------
Smart exit logic with ATR-based trailing stops.

Exit conditions per position (checked every tick):

LONG position:
  1. Time exit at 15:20 (intraday close).
  2. Hard stop: ltp <= initial_stop_price (prevents massive gap-down losses).
  3. Breakeven activation: when unrealized P&L >= 1R (one risk unit), move
     trailing_stop_price to avg_price (entry). Trade is now risk-free.
  4. Trail: once breakeven is active, update highest_price on new highs.
     trailing_stop_price = highest_price - (1.0 * ATR). Always move up, never down.
  5. Trigger: ltp <= trailing_stop_price → exit.

SHORT position:
  1. Time exit at 15:20.
  2. Hard stop: ltp >= initial_stop_price.
  3. Breakeven activation: when unrealized P&L >= 1R, move
     trailing_stop_price to avg_price.
  4. Trail: update lowest_price on new lows.
     trailing_stop_price = lowest_price + (1.0 * ATR). Always move down, never up.
  5. Trigger: ltp >= trailing_stop_price → exit (cover short).
"""
import logging
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import List, Optional

from src.trading.positions import PositionBook, Position
from src.config.risk import RiskConfig
from src.data_ingestion.market_live import KiteLiveClient

logger = logging.getLogger(__name__)

TRAIL_ATR_MULTIPLIER = 1.0   # trail at 1×ATR from the running extreme


@dataclass
class ExitSignal:
    symbol: str
    side: str       # "SELL" (close long) | "COVER" (close short)
    reason: str     # "STOP_LOSS" | "TRAILING_STOP" | "BREAKEVEN_STOP" | "TIME_EXIT"
    ltp: float
    stop_price: Optional[float]
    mode: str
    strategy: str


class ExitEngine:
    def __init__(
        self,
        positions: PositionBook,
        live_client: KiteLiveClient,
        risk: RiskConfig,
    ) -> None:
        self.positions = positions
        self.live_client = live_client
        self.risk = risk
        self._exit_time = self._parse_exit_time()

    def _parse_exit_time(self) -> dt_time:
        try:
            h, m = self.risk.intraday_exit_time.split(":")
            return dt_time(int(h), int(m))
        except Exception:
            return dt_time(15, 20)

    def tick(self, now: datetime) -> List[ExitSignal]:
        signals: List[ExitSignal] = []
        current_time = now.time()

        for pos in self.positions.get_open_positions():
            tick = self.live_client.get_last_tick(pos.symbol)
            if not tick:
                continue
            ltp = tick.ltp

            # ── 1. Time exit ──────────────────────────────────────────────
            if pos.mode == "INTRADAY" and current_time >= self._exit_time:
                signals.append(self._make_signal(pos, ltp, "TIME_EXIT"))
                continue

            # ── 2. Update running extremes & trailing stop ─────────────────
            self._update_trail(pos, ltp)

            # ── 3. Hard stop (before breakeven is active) ─────────────────
            if not pos.breakeven_activated and pos.initial_stop_price is not None:
                if pos.side == "LONG" and ltp <= pos.initial_stop_price:
                    signals.append(self._make_signal(pos, ltp, "STOP_LOSS"))
                    continue
                if pos.side == "SHORT" and ltp >= pos.initial_stop_price:
                    signals.append(self._make_signal(pos, ltp, "STOP_LOSS"))
                    continue

            # ── 4. Trailing stop check ─────────────────────────────────────
            if pos.trailing_stop_price is not None:
                if pos.side == "LONG" and ltp <= pos.trailing_stop_price:
                    reason = "BREAKEVEN_STOP" if pos.breakeven_activated else "TRAILING_STOP"
                    signals.append(self._make_signal(pos, ltp, reason))
                elif pos.side == "SHORT" and ltp >= pos.trailing_stop_price:
                    reason = "BREAKEVEN_STOP" if pos.breakeven_activated else "TRAILING_STOP"
                    signals.append(self._make_signal(pos, ltp, reason))

        return signals

    def _update_trail(self, pos: Position, ltp: float) -> None:
        """
        Update trailing stop price based on the latest LTP.
        Called every tick for every open position.
        """
        atr = pos.atr or 0.0
        one_r = pos.risk_unit()

        if pos.side == "LONG":
            unrealized = (ltp - pos.avg_price) * pos.total_qty

            # Breakeven activation: price moved 1R in our favour
            if not pos.breakeven_activated and one_r > 0 and unrealized >= one_r:
                pos.trailing_stop_price = pos.avg_price
                pos.breakeven_activated = True
                logger.info(
                    f"[TRAIL] {pos.symbol} LONG breakeven activated @ {pos.avg_price:.2f}"
                )

            # Trail on new highs
            if pos.highest_price is None or ltp > pos.highest_price:
                pos.highest_price = ltp
                if pos.breakeven_activated and atr > 0:
                    new_trail = ltp - (TRAIL_ATR_MULTIPLIER * atr)
                    # Only ratchet upward — never lower the stop
                    if pos.trailing_stop_price is None or new_trail > pos.trailing_stop_price:
                        pos.trailing_stop_price = round(new_trail, 2)
                        logger.info(
                            f"[TRAIL] {pos.symbol} trail raised → {pos.trailing_stop_price:.2f}"
                        )

        elif pos.side == "SHORT":
            unrealized = (pos.avg_price - ltp) * pos.total_qty

            # Breakeven activation
            if not pos.breakeven_activated and one_r > 0 and unrealized >= one_r:
                pos.trailing_stop_price = pos.avg_price
                pos.breakeven_activated = True
                logger.info(
                    f"[TRAIL] {pos.symbol} SHORT breakeven activated @ {pos.avg_price:.2f}"
                )

            # Trail on new lows
            if pos.lowest_price is None or ltp < pos.lowest_price:
                pos.lowest_price = ltp
                if pos.breakeven_activated and atr > 0:
                    new_trail = ltp + (TRAIL_ATR_MULTIPLIER * atr)
                    # Only ratchet downward — never raise the stop on a short
                    if pos.trailing_stop_price is None or new_trail < pos.trailing_stop_price:
                        pos.trailing_stop_price = round(new_trail, 2)
                        logger.info(
                            f"[TRAIL] {pos.symbol} SHORT trail lowered → {pos.trailing_stop_price:.2f}"
                        )

    def _make_signal(self, pos: Position, ltp: float, reason: str) -> ExitSignal:
        side = "SELL" if pos.side == "LONG" else "COVER"
        return ExitSignal(
            symbol=pos.symbol,
            side=side,
            reason=reason,
            ltp=ltp,
            stop_price=pos.trailing_stop_price,
            mode=pos.mode,
            strategy=pos.strategy,
        )
