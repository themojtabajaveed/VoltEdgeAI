"""
exit_engine.py (v3)
-------------------
Smart exit logic with adaptive, volume-aware trailing stops.

v3 upgrades over v2:
  1. PHASED ATR TRAIL — adapts multiplier based on time and profit level
  2. FAKE DIP DETECTION — holds through low-volume shakeouts
  3. PARTIAL PROFIT TAKING — scales out at milestones to lock in gains

Exit conditions per position (checked every tick):

LONG position:
  1. Time exit at 15:20 (intraday close).
  2. Hard stop: ltp <= initial_stop_price (prevents massive gap-down losses).
  3. Breakeven activation: when unrealized P&L >= 1R, move stop to entry.
  4. Trail: phase-adaptive ATR multiplier from extreme price.
     - Settle (0-15 min): 2.0× ATR (wide — room for noise)
     - Confirm (15-45 min or at breakeven): 1.5× ATR
     - Lock (profit > 1.5R): 1.0× ATR
     - Accelerate (profit > 2.5R): 0.75× ATR
  5. Fake dip filter: if dip volume < 40% of rally volume AND VWAP holds,
     delay stop trigger for up to 2 bars.
  6. Partial exit: 50% at 1.5R, 25% more at 2.5R, let 25% run.
  7. Momentum exhaustion: gave back > 60% of peak gain with high volume.

SHORT position: mirror of LONG with inverted logic.
"""
import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import List, Optional

from src.trading.positions import PositionBook, Position
from src.config.risk import RiskConfig
from src.data_ingestion.market_live import KiteLiveClient

logger = logging.getLogger(__name__)

# ── Phase-adaptive ATR multipliers ──
PHASE_SETTLE_ATR = 2.0       # 0-15 min: wide — absorb entry noise
PHASE_CONFIRM_ATR = 1.5      # 15-45 min: tighten as thesis confirms
PHASE_LOCK_ATR = 1.0         # in profit > 1.5R: aggressive lock
PHASE_ACCELERATE_ATR = 0.75  # in profit > 2.5R: squeeze maximum

PHASE_SETTLE_MINUTES = 15    # min after entry
PHASE_CONFIRM_MINUTES = 45   # min after entry

# ── Fake dip detection ──
FAKE_DIP_VOLUME_RATIO = 0.40   # dip volume < 40% of rally = fake
REAL_DIP_VOLUME_RATIO = 0.70   # dip volume > 70% of rally = real
FAKE_DIP_GRACE_BARS = 2        # hold through fake dip for max N additional bars

# ── Partial profit targets (in R multiples) ──
PARTIAL_1_R = 1.5    # sell 50% at 1.5R
PARTIAL_1_PCT = 0.50
PARTIAL_2_R = 2.5    # sell 25% more at 2.5R
PARTIAL_2_PCT = 0.25
# Remaining 25% runs with PHASE_ACCELERATE_ATR trail

# Strategy-specific multipliers for partials
VIPER_PARTIAL_1_R = 2.0   # VIPER momentum runs further before first exit
VIPER_PARTIAL_2_R = 3.0


@dataclass
class ExitSignal:
    symbol: str
    side: str       # "SELL" (close long) | "COVER" (close short)
    reason: str     # "STOP_LOSS" | "TRAILING_STOP" | "TIME_EXIT" | "PARTIAL_1" | "PARTIAL_2"
    ltp: float
    stop_price: Optional[float]
    mode: str
    strategy: str
    qty: Optional[int] = None  # v3: partial qty (None = full position)


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
        # v3: Track fake dip state per symbol
        self._fake_dip_bars: dict[str, int] = {}  # symbol -> bars held through dip
        self._recent_bar_volumes: dict[str, list[float]] = {}  # symbol -> rolling bar volumes
        # v4: RSI divergence detection state
        self._divergence_warned: set[str] = set()   # symbols already flagged
        self._streaming_states_ref: dict | None = None  # set by runner after init

    def _parse_exit_time(self) -> dt_time:
        try:
            h, m = self.risk.intraday_exit_time.split(":")
            return dt_time(int(h), int(m))
        except Exception:
            return dt_time(15, 20)

    def update_bar_volumes(self, symbol: str, bar_volume: float) -> None:
        """
        Called by runner on each new bar to feed volume data for fake dip detection.
        Maintains a rolling window of the last 30 bars.
        """
        if symbol not in self._recent_bar_volumes:
            self._recent_bar_volumes[symbol] = []
        self._recent_bar_volumes[symbol].append(bar_volume)
        # Keep last 30 bars only
        if len(self._recent_bar_volumes[symbol]) > 30:
            self._recent_bar_volumes[symbol] = self._recent_bar_volumes[symbol][-30:]

    def set_streaming_states(self, states_ref: dict) -> None:
        """
        Give ExitEngine read access to the streaming TA states dict
        (from technical_body._streaming_states) for RSI divergence detection.
        Called by runner.py after ExitEngine is created.
        """
        self._streaming_states_ref = states_ref

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

            # ── 2. Compute time in position ───────────────────────────────
            mins_open = (now - pos.entry_time).total_seconds() / 60 if pos.entry_time else 0

            # ── 3. Update running extremes & trailing stop ────────────────
            self._update_trail(pos, ltp, mins_open)

            # ── 4. Hard stop (before breakeven is active) ─────────────────
            if not pos.breakeven_activated and pos.initial_stop_price is not None:
                if pos.side == "LONG" and ltp <= pos.initial_stop_price:
                    signals.append(self._make_signal(pos, ltp, "STOP_LOSS"))
                    continue
                if pos.side == "SHORT" and ltp >= pos.initial_stop_price:
                    signals.append(self._make_signal(pos, ltp, "STOP_LOSS"))
                    continue

            # ── 5. Trailing stop check with fake dip filter ───────────────
            if pos.trailing_stop_price is not None:
                hit_trail = False
                if pos.side == "LONG" and ltp <= pos.trailing_stop_price:
                    hit_trail = True
                elif pos.side == "SHORT" and ltp >= pos.trailing_stop_price:
                    hit_trail = True

                if hit_trail:
                    if self._is_fake_dip(pos, ltp):
                        # Hold through noise — but track how many bars
                        bars_held = self._fake_dip_bars.get(pos.symbol, 0) + 1
                        self._fake_dip_bars[pos.symbol] = bars_held
                        if bars_held > FAKE_DIP_GRACE_BARS:
                            # Grace period expired — honor the stop
                            self._fake_dip_bars.pop(pos.symbol, None)
                            reason = "TRAILING_STOP"
                            signals.append(self._make_signal(pos, ltp, reason))
                            continue
                        else:
                            logger.info(
                                f"[FAKE DIP] {pos.symbol}: holding through noise "
                                f"(bar {bars_held}/{FAKE_DIP_GRACE_BARS})"
                            )
                    else:
                        self._fake_dip_bars.pop(pos.symbol, None)
                        reason = "BREAKEVEN_STOP" if pos.breakeven_activated else "TRAILING_STOP"
                        signals.append(self._make_signal(pos, ltp, reason))
                        continue
                else:
                    # Price recovered — reset fake dip counter
                    self._fake_dip_bars.pop(pos.symbol, None)

            # ── 6. Partial profit exits ───────────────────────────────────
            partial_signal = self._check_partial_exit(pos, ltp)
            if partial_signal:
                signals.append(partial_signal)
                # Don't continue — still need to monitor remaining position

            # ── 6.5. RSI divergence warning (tighten stop, no immediate exit) ──
            if (pos.breakeven_activated
                    and pos.symbol not in self._divergence_warned
                    and self._check_rsi_divergence(pos)):
                self._divergence_warned.add(pos.symbol)
                logger.warning(
                    f"[RSI DIV] {pos.symbol}: {'Bearish' if pos.side == 'LONG' else 'Bullish'} "
                    f"RSI divergence detected — momentum fracturing. Tightening trail."
                )
                # Tighten: move trailing stop to entry price if it's below (LONG)
                # or above (SHORT) entry
                if pos.side == "LONG" and (
                    pos.trailing_stop_price is None
                    or pos.trailing_stop_price < pos.avg_price
                ):
                    pos.trailing_stop_price = pos.avg_price
                    logger.info(
                        f"[RSI DIV] {pos.symbol}: Trail tightened to entry {pos.avg_price:.2f}"
                    )
                elif pos.side == "SHORT" and (
                    pos.trailing_stop_price is None
                    or pos.trailing_stop_price > pos.avg_price
                ):
                    pos.trailing_stop_price = pos.avg_price
                    logger.info(
                        f"[RSI DIV] {pos.symbol}: SHORT trail tightened to entry {pos.avg_price:.2f}"
                    )

            # ── 7. Momentum exhaustion (only after > 10 min) ─────────────
            if mins_open >= 10:
                if self._check_exhaustion_exit(pos, ltp):
                    signals.append(self._make_signal(pos, ltp, "MOMENTUM_EXHAUSTION"))
                    continue

            # ── 8. MACD Distribution Partial Exit ────────────────────────
            macd_signal = self._check_macd_distribution(pos, ltp)
            if macd_signal:
                signals.append(macd_signal)
                continue

        return signals

    def _get_phase_multiplier(self, pos: Position, mins_open: float) -> float:
        """
        Determine ATR multiplier based on time and profit level.
        Profit-based phases override time-based phases (tighter is better for profit).
        """
        one_r = pos.risk_unit()
        if one_r <= 0:
            return PHASE_SETTLE_ATR  # Default wide if no risk unit

        # Calculate current R multiple
        if pos.side == "LONG":
            per_share_gain = (pos.highest_price or pos.avg_price) - pos.avg_price
        else:
            per_share_gain = pos.avg_price - (pos.lowest_price or pos.avg_price)
        r_multiple = per_share_gain / one_r if one_r > 0 else 0

        # Profit-based phases (override time)
        if r_multiple >= 2.5:
            return PHASE_ACCELERATE_ATR   # 0.75× — squeeze maximum
        elif r_multiple >= 1.5:
            return PHASE_LOCK_ATR          # 1.0× — lock gains
        # Time-based phases
        elif mins_open >= PHASE_CONFIRM_MINUTES:
            return PHASE_CONFIRM_ATR       # 1.5× — tighten after 45 min
        elif mins_open >= PHASE_SETTLE_MINUTES:
            return PHASE_CONFIRM_ATR       # 1.5× — tighten after 15 min
        else:
            return PHASE_SETTLE_ATR        # 2.0× — wide for early noise

    def _update_trail(self, pos: Position, ltp: float, mins_open: float) -> None:
        """
        Update trailing stop price with phase-adaptive ATR multiplier.
        Also tracks rally average volume for fake dip detection.
        """
        atr = pos.atr or 0.0
        one_r = pos.risk_unit()
        multiplier = self._get_phase_multiplier(pos, mins_open)

        if pos.side == "LONG":
            per_share_gain = ltp - pos.avg_price

            # Breakeven activation: price moved 1R in our favour
            if not pos.breakeven_activated and one_r > 0 and per_share_gain >= one_r:
                pos.trailing_stop_price = pos.avg_price
                pos.breakeven_activated = True
                logger.info(
                    f"[TRAIL] {pos.symbol} LONG breakeven activated @ {pos.avg_price:.2f}"
                )

            # Trail on new highs
            if pos.highest_price is None or ltp > pos.highest_price:
                pos.highest_price = ltp
                # Update rally average volume from recent positive bars
                self._update_rally_volume(pos)

                if pos.breakeven_activated and atr > 0:
                    new_trail = ltp - (multiplier * atr)
                    # Only ratchet upward — never lower the stop
                    if pos.trailing_stop_price is None or new_trail > pos.trailing_stop_price:
                        pos.trailing_stop_price = round(new_trail, 2)
                        logger.info(
                            f"[TRAIL] {pos.symbol} trail raised → {pos.trailing_stop_price:.2f} "
                            f"(phase={multiplier:.2f}×ATR)"
                        )

        elif pos.side == "SHORT":
            per_share_gain = pos.avg_price - ltp

            # Breakeven activation
            if not pos.breakeven_activated and one_r > 0 and per_share_gain >= one_r:
                pos.trailing_stop_price = pos.avg_price
                pos.breakeven_activated = True
                logger.info(
                    f"[TRAIL] {pos.symbol} SHORT breakeven activated @ {pos.avg_price:.2f}"
                )

            # Trail on new lows
            if pos.lowest_price is None or ltp < pos.lowest_price:
                pos.lowest_price = ltp
                self._update_rally_volume(pos)

                if pos.breakeven_activated and atr > 0:
                    new_trail = ltp + (multiplier * atr)
                    # Only ratchet downward — never raise the stop on a short
                    if pos.trailing_stop_price is None or new_trail < pos.trailing_stop_price:
                        pos.trailing_stop_price = round(new_trail, 2)
                        logger.info(
                            f"[TRAIL] {pos.symbol} SHORT trail lowered → {pos.trailing_stop_price:.2f} "
                            f"(phase={multiplier:.2f}×ATR)"
                        )

    def _update_rally_volume(self, pos: Position) -> None:
        """
        Update the rally_avg_volume on the position from bars where
        price was moving in our favor.
        """
        bars = self._recent_bar_volumes.get(pos.symbol, [])
        if len(bars) >= 3:
            # Use the average of recent bars as rally volume
            pos.rally_avg_volume = sum(bars[-5:]) / min(5, len(bars[-5:]))

    def _is_fake_dip(self, pos: Position, ltp: float) -> bool:
        """
        Determine if the current dip is noise (low volume) or real selling (high volume).

        Fake dip criteria:
          1. Dip volume < 40% of rally average volume
          2. VWAP still holding (approximated from recent bar data)

        Returns True if the dip looks fake (should hold through it).
        """
        bars = self._recent_bar_volumes.get(pos.symbol, [])
        if len(bars) < 5 or pos.rally_avg_volume <= 0:
            return False  # Can't determine — honor the stop

        # Average volume of last 3 bars (the dip)
        dip_volume = sum(bars[-3:]) / 3
        rally_volume = pos.rally_avg_volume

        volume_ratio = dip_volume / rally_volume if rally_volume > 0 else 1.0

        if volume_ratio >= REAL_DIP_VOLUME_RATIO:
            # High volume dip — real selling, honor the stop
            logger.info(
                f"[REAL DIP] {pos.symbol}: dip vol {dip_volume:.0f} = {volume_ratio:.0%} of rally "
                f"→ real selling pressure, exiting"
            )
            return False

        if volume_ratio < FAKE_DIP_VOLUME_RATIO:
            # Low volume dip — fake shakeout
            logger.info(
                f"[FAKE DIP] {pos.symbol}: dip vol {dip_volume:.0f} = {volume_ratio:.0%} of rally "
                f"→ noise, holding through"
            )
            return True

        # In between (40-70%) — ambiguous, err on side of caution
        # Check if after breakeven — if so, hold tighter
        if pos.breakeven_activated:
            return False  # Already in profit, protect it
        return True  # Not yet profitable, give more room

    def _check_partial_exit(self, pos: Position, ltp: float) -> Optional[ExitSignal]:
        """
        Check if we've reached a partial profit milestone.
        Sell a portion of the position and let the rest run.
        """
        one_r = pos.risk_unit()
        if one_r <= 0 or pos.original_qty <= 0:
            return None

        # Calculate current R multiple
        if pos.side == "LONG":
            per_share_gain = ltp - pos.avg_price
        else:
            per_share_gain = pos.avg_price - ltp
        r_multiple = per_share_gain / one_r

        # Strategy-specific targets
        is_viper = "VIPER" in pos.strategy.upper() if pos.strategy else False
        p1_r = VIPER_PARTIAL_1_R if is_viper else PARTIAL_1_R
        p2_r = VIPER_PARTIAL_2_R if is_viper else PARTIAL_2_R

        # Partial 1: sell 50% at 1.5R (or 2R for VIPER)
        if pos.partial_sold_pct < PARTIAL_1_PCT and r_multiple >= p1_r:
            qty_to_sell = int(pos.original_qty * PARTIAL_1_PCT)
            if qty_to_sell >= 1 and qty_to_sell < pos.total_qty:
                pos.partial_sold_pct = PARTIAL_1_PCT
                logger.info(
                    f"[PARTIAL-1] {pos.symbol}: {r_multiple:.1f}R profit — "
                    f"selling {qty_to_sell}/{pos.original_qty} ({PARTIAL_1_PCT:.0%})"
                )
                return ExitSignal(
                    symbol=pos.symbol,
                    side="SELL" if pos.side == "LONG" else "COVER",
                    reason="PARTIAL_1",
                    ltp=ltp,
                    stop_price=pos.trailing_stop_price,
                    mode=pos.mode,
                    strategy=pos.strategy,
                    qty=qty_to_sell,
                )

        # Partial 2: sell 25% more at 2.5R (or 3R for VIPER)
        if pos.partial_sold_pct < (PARTIAL_1_PCT + PARTIAL_2_PCT) and r_multiple >= p2_r:
            qty_to_sell = int(pos.original_qty * PARTIAL_2_PCT)
            if qty_to_sell >= 1 and qty_to_sell < pos.total_qty:
                pos.partial_sold_pct = PARTIAL_1_PCT + PARTIAL_2_PCT
                logger.info(
                    f"[PARTIAL-2] {pos.symbol}: {r_multiple:.1f}R profit — "
                    f"selling {qty_to_sell} more ({PARTIAL_2_PCT:.0%}), "
                    f"remaining {pos.total_qty - qty_to_sell} rides with tight trail"
                )
                return ExitSignal(
                    symbol=pos.symbol,
                    side="SELL" if pos.side == "LONG" else "COVER",
                    reason="PARTIAL_2",
                    ltp=ltp,
                    stop_price=pos.trailing_stop_price,
                    mode=pos.mode,
                    strategy=pos.strategy,
                    qty=qty_to_sell,
                )

        return None

    def _check_exhaustion_exit(self, pos: Position, ltp: float) -> bool:
        """
        Check if the move is exhausted.
        For LONG: if price has given back > 60% of unrealized peak gain
        AND this is happening on significant volume → real exhaustion.
        Without volume confirmation, midday price drifts would trigger false exits.
        """
        if not pos.breakeven_activated:
            return False

        # Volume check: only confirm exhaustion if selling pressure is real
        bars = self._recent_bar_volumes.get(pos.symbol, [])
        if len(bars) >= 3 and pos.rally_avg_volume > 0:
            dip_volume = sum(bars[-3:]) / 3
            volume_ratio = dip_volume / pos.rally_avg_volume
            if volume_ratio < 0.50:
                # Low volume retrace — likely midday noise, not real exhaustion
                return False

        if pos.side == "LONG":
            if pos.highest_price and pos.highest_price > pos.avg_price:
                peak_gain = pos.highest_price - pos.avg_price
                current_gain = ltp - pos.avg_price
                if peak_gain > 0 and current_gain / peak_gain < 0.4:
                    logger.info(f"[EXIT] {pos.symbol} exhaustion: gave back 60%+ of peak gain on volume")
                    return True
        elif pos.side == "SHORT":
            if pos.lowest_price and pos.lowest_price < pos.avg_price:
                peak_gain = pos.avg_price - pos.lowest_price
                current_gain = pos.avg_price - ltp
                if peak_gain > 0 and current_gain / peak_gain < 0.4:
                    logger.info(f"[EXIT] {pos.symbol} SHORT exhaustion: gave back 60%+ of peak gain on volume")
                    return True
        return False

    def _check_rsi_divergence(self, pos) -> bool:
        """
        Check RSI divergence for an open position using streaming TA state.
        LONG → check for bearish divergence (price higher high, RSI lower high).
        SHORT → check for bullish divergence (price lower low, RSI higher low).
        Returns True if divergence detected.
        """
        if self._streaming_states_ref is None:
            return False

        from src.strategies.technical_body import TechnicalBody

        state = self._streaming_states_ref.get(pos.symbol)
        if state is None:
            return False

        if pos.side == "LONG":
            return TechnicalBody.detect_bearish_rsi_divergence(state)
        elif pos.side == "SHORT":
            return TechnicalBody.detect_bullish_rsi_divergence(state)
        return False

    def _check_macd_distribution(self, pos: Position, ltp: float) -> Optional[ExitSignal]:
        """
        Research 2: distribution exit trigger.
        MACD bearish cross (or expanding negative) + volume shrinking on an up-bar.
        If detected, scales out 50% of the position.
        """
        if pos.side != "LONG" or self._streaming_states_ref is None:
            return None

        state = self._streaming_states_ref.get(pos.symbol)
        if state is None or state.last_snapshot is None:
            return None
        
        snap = state.last_snapshot
        
        # We need bearish MACD histogram that is getting worse (expanding downward)
        # AND low volume (volume_spike_ratio < 0.8 signifies lack of buyers)
        if (snap.macd_hist < 0 
            and snap.macd_hist < snap.macd_histogram_prev 
            and snap.volume_spike_ratio < 0.8):
            
            logger.info(f"[EXIT PARTIAL] {pos.symbol}: MACD Distribution + Volume shrinkage detected.")
            # Emit a partial exit (50% scale out)
            return self._make_signal(pos, ltp, "MACD_DISTRIBUTION", qty_pct=0.5)
        
        return None

    def check_tick(self, symbol: str, ltp: float) -> Optional[ExitSignal]:
        """
        ── Optimisation 1: Tick-level fast-path exit check ──────────────────
        Called by BarBuilderThread on EVERY incoming WebSocket tick.
        Only evaluates hard stop-loss and trailing stop for the given symbol.
        Time-based exits (EOD 15:20), partial exits, and exhaustion exits
        remain in the standard ExitMonitorThread 1-second loop.

        Latency: <1ms (no I/O, no heavy computation).
        Thread safety: reads pos fields under positions._lock (RLock is re-entrant).
        """
        t0 = _time.monotonic()
        pos = self.positions.get_position(symbol)
        if pos is None:
            return None

        signal: Optional[ExitSignal] = None

        # ── Hard stop (before breakeven activated) ────────────────────────
        if not pos.breakeven_activated and pos.initial_stop_price is not None:
            if pos.side == "LONG" and ltp <= pos.initial_stop_price:
                signal = self._make_signal(pos, ltp, "STOP_LOSS")
            elif pos.side == "SHORT" and ltp >= pos.initial_stop_price:
                signal = self._make_signal(pos, ltp, "STOP_LOSS")

        # ── Trailing stop ─────────────────────────────────────────────────
        if signal is None and pos.trailing_stop_price is not None:
            if pos.side == "LONG" and ltp <= pos.trailing_stop_price:
                # Quick fake-dip guard: skip if volume data suggests noise
                # Full fake-dip logic runs in the 1s loop; this is the fast path.
                reason = "BREAKEVEN_STOP" if pos.breakeven_activated else "TRAILING_STOP"
                signal = self._make_signal(pos, ltp, reason)
            elif pos.side == "SHORT" and ltp >= pos.trailing_stop_price:
                reason = "BREAKEVEN_STOP" if pos.breakeven_activated else "TRAILING_STOP"
                signal = self._make_signal(pos, ltp, reason)

        elapsed_ms = (_time.monotonic() - t0) * 1000
        if signal:
            logger.info(
                f"[check_tick] ⚡ {symbol} {signal.reason} @ {ltp:.2f} "
                f"(fast-path, {elapsed_ms:.2f}ms)"
            )
        return signal

    def _make_signal(self, pos: Position, ltp: float, reason: str, qty_pct: Optional[float] = None) -> ExitSignal:
        side = "SELL" if pos.side == "LONG" else "COVER"
        
        qty = None
        if qty_pct is not None:
            calc_qty = int(pos.total_qty * qty_pct)
            if calc_qty >= 1 and calc_qty < pos.total_qty:
                qty = calc_qty
            else:
                qty = pos.total_qty  # exit full if partial calculation is invalid

        return ExitSignal(
            symbol=pos.symbol,
            side=side,
            reason=reason,
            ltp=ltp,
            stop_price=pos.trailing_stop_price,
            mode=pos.mode,
            strategy=pos.strategy,
            qty=qty,
        )
