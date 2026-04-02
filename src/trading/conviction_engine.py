"""
conviction_engine.py — Dynamic Multi-Layer Conviction System
--------------------------------------------------------------
Replaces the single-shot conviction score with a 5-layer dynamic
system that recomputes every 15 minutes.

Layers:
  A (25%) — Market State: phase-derived, direction-aware
  B (15%) — Sector Context: sector relative strength
  C (30%) — Catalyst Quality: signal quality, FROZEN at creation
  D (20%) — Technical Confirmation: VWAP, ORB, volume, price action
  E (10%) — Historical Pattern Match: cold start at 50

Key concepts:
  - ActiveSignal: lives on the watchboard, recomputed every cycle
  - Watchboard: in-memory dict, reset daily
  - Signals wait for conditions to align, then fire automatically
  - Catalyst (Layer C) is immutable — timing layers change around it
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.trading.market_phase import (
    MarketPhase, MarketSnapshot, PhaseState,
    compute_layer_a, update_phase,
)
from src.trading.sector_guard import get_sector
from src.trading.pattern_db import (
    PatternDB, PatternOutcome, PatternFingerprint,
    build_fingerprint, classify_catalyst_type, classify_time_bucket,
    classify_vix_regime,
)

logger = logging.getLogger(__name__)

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

# ── Layer weights ──────────────────────────────────────────────────────────
W_A = 0.25  # Market state
W_B = 0.15  # Sector context
W_C = 0.30  # Catalyst quality
W_D = 0.20  # Technical confirmation
W_E = 0.10  # Pattern match

CONVICTION_THRESHOLD = 70.0
SIGNAL_MAX_AGE_HOURS = 4.0
SIGNAL_EXPIRY_TIME = (14, 30)  # 14:30 IST — no new entries last hour


@dataclass
class ActiveSignal:
    """A signal on the conviction watchboard, recomputed every cycle."""
    symbol: str
    direction: str              # "BUY" or "SHORT"
    strategy: str               # "HYDRA", "VIPER", "V2_DISCOVERY"
    layer_c_score: float        # Catalyst quality (0–100, FROZEN)
    layer_e_score: float = 50.0 # Pattern match (cold start at 50)
    event_summary: str = ""
    created_at: Optional[datetime] = None
    last_evaluated_at: Optional[datetime] = None
    conviction_history: List[Tuple[str, float, str]] = field(default_factory=list)
    # Each entry: (timestamp_str, conviction, phase_name)
    status: str = "WATCHING"    # WATCHING, TRIGGERED, EXPIRED
    last_conviction: float = 0.0
    metadata: dict = field(default_factory=dict)  # strategy-specific extras

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(IST)


def compute_layer_b(symbol: str, snapshot: MarketSnapshot, direction: str) -> float:
    """
    Layer B: Sector Context (0–100).

    Scores sector relative strength: how is the stock's sector
    performing vs Nifty today?

    For LONG: sector outperformance = boost, underperformance = penalty.
    For SHORT: inverted.
    """
    sector = get_sector(symbol)
    sector_chg = snapshot.sector_changes.get(sector, None)
    nifty_chg = snapshot.nifty_pct

    # If no sector data, return neutral
    if sector_chg is None:
        return 50.0

    # Relative strength: sector vs Nifty
    rel_strength = sector_chg - nifty_chg

    # For LONG: positive RS = good
    if direction == "BUY":
        if rel_strength > 1.0:
            score = 85.0
        elif rel_strength > 0.3:
            score = 70.0
        elif rel_strength > -0.3:
            score = 50.0
        elif rel_strength > -1.0:
            score = 30.0
        else:
            score = 15.0
    else:  # SHORT
        # Inverted: negative RS = sector underperforming = good for shorts
        if rel_strength < -1.0:
            score = 85.0
        elif rel_strength < -0.3:
            score = 70.0
        elif rel_strength < 0.3:
            score = 50.0
        elif rel_strength < 1.0:
            score = 30.0
        else:
            score = 15.0

    return score


def compute_layer_d(snapshot, direction: str) -> float:
    """
    Layer D: Technical Confirmation (0–100).

    Uses the existing TechnicalSnapshot from TechnicalBody to score
    whether price action confirms the signal direction.

    Args:
        snapshot: TechnicalSnapshot from TechnicalBody.compute_or_stream()
        direction: "BUY" or "SHORT"
    """
    # If no snapshot data, return neutral
    if snapshot is None:
        return 50.0

    score = 0.0

    if direction == "BUY":
        # Above VWAP: +25
        if getattr(snapshot, 'above_vwap', False):
            score += 25.0

        # Above ORB high: +25
        if getattr(snapshot, 'orb_breakout', False):
            score += 25.0
        elif getattr(snapshot, 'orb_high', 0) > 0 and getattr(snapshot, 'last_price', 0) > 0:
            # Inside range but not breakdown
            if not getattr(snapshot, 'orb_breakdown', False):
                score += 10.0  # inside ORB range = partial credit

        # Volume above 1.5x: +20
        vol_ratio = getattr(snapshot, 'volume_spike_ratio', 0)
        if vol_ratio >= 1.5:
            score += 20.0
        elif vol_ratio >= 1.0:
            score += 10.0

        # RSI recovering from oversold: +10
        rsi = getattr(snapshot, 'rsi14', 50.0)
        if 30 <= rsi <= 50:
            score += 10.0  # recovering from oversold
        elif rsi > 50:
            score += 5.0   # neutral territory

        # Price direction (EMA9 alignment as proxy for 2-candle direction): +20
        ema9 = getattr(snapshot, 'ema9', 0)
        ltp = getattr(snapshot, 'last_price', 0)
        if ema9 > 0 and ltp > ema9:
            score += 15.0  # price above short-term trend
        # MACD histogram improving
        macd_hist = getattr(snapshot, 'macd_hist', 0)
        macd_hist_prev = getattr(snapshot, 'macd_histogram_prev', 0)
        if macd_hist > macd_hist_prev:
            score += 5.0

    else:  # SHORT — inverted
        # Below VWAP: +25
        if not getattr(snapshot, 'above_vwap', True):
            score += 25.0

        # Below ORB low: +25
        if getattr(snapshot, 'orb_breakdown', False):
            score += 25.0
        elif getattr(snapshot, 'orb_low', 0) > 0 and getattr(snapshot, 'last_price', 0) > 0:
            if not getattr(snapshot, 'orb_breakout', False):
                score += 10.0

        # Panic volume: +20
        vol_ratio = getattr(snapshot, 'volume_spike_ratio', 0)
        if vol_ratio >= 1.5:
            score += 20.0
        elif vol_ratio >= 1.0:
            score += 10.0

        # RSI declining from overbought: +10
        rsi = getattr(snapshot, 'rsi14', 50.0)
        if 50 <= rsi <= 70:
            score += 10.0
        elif rsi < 50:
            score += 5.0

        # Price below EMA9 (bearish trend): +20
        ema9 = getattr(snapshot, 'ema9', 0)
        ltp = getattr(snapshot, 'last_price', 0)
        if ema9 > 0 and ltp < ema9:
            score += 15.0
        # MACD histogram worsening
        macd_hist = getattr(snapshot, 'macd_hist', 0)
        macd_hist_prev = getattr(snapshot, 'macd_histogram_prev', 0)
        if macd_hist < macd_hist_prev:
            score += 5.0

    return min(100.0, score)


def _compute_conviction(
    signal: ActiveSignal,
    layer_a: float,
    layer_b: float,
    layer_d: float,
) -> float:
    """
    Compute the weighted conviction score from all 5 layers.

    Returns: float 0–100
    """
    raw = (
        layer_a * W_A
        + layer_b * W_B
        + signal.layer_c_score * W_C
        + layer_d * W_D
        + signal.layer_e_score * W_E
    )
    return max(0.0, min(100.0, raw))


class ConvictionEngine:
    """
    Dynamic multi-layer conviction engine.

    Owns the watchboard of ActiveSignals. Recomputes conviction
    for all signals every cycle using live market data.
    """

    def __init__(self, threshold: float = CONVICTION_THRESHOLD):
        self._watchboard: Dict[str, ActiveSignal] = {}  # keyed by symbol
        self._phase_state = PhaseState()
        self._threshold = threshold
        self._prev_snapshot: Optional[MarketSnapshot] = None
        self._morning_regime_bias: float = 0.0  # from Grok, -10 to +10
        self._pattern_db = PatternDB()
        logger.info(f"[ConvEng] Conviction engine initialised, watchboard empty | {self._pattern_db.get_summary()}")

    @property
    def watchboard_size(self) -> int:
        return len(self._watchboard)

    @property
    def phase(self) -> MarketPhase:
        return self._phase_state.current_phase

    @property
    def phase_state(self) -> PhaseState:
        return self._phase_state

    def set_morning_regime_bias(self, bias: float) -> None:
        """Set the Grok morning regime bias (-10 to +10)."""
        self._morning_regime_bias = max(-10.0, min(10.0, bias))
        logger.info(f"[ConvEng] Morning regime bias set: {self._morning_regime_bias:+.0f}")

    def add_signal(self, signal: ActiveSignal) -> bool:
        """
        Add a signal to the watchboard.
        Returns True if added, False if duplicate symbol already present.
        Computes Layer E from pattern DB if enough historical data exists.
        """
        key = f"{signal.symbol}_{signal.direction}"
        if key in self._watchboard:
            existing = self._watchboard[key]
            # Update if new signal has higher catalyst score
            if signal.layer_c_score > existing.layer_c_score:
                logger.info(
                    f"[ConvEng] Upgrading {signal.symbol} {signal.direction} "
                    f"catalyst C={existing.layer_c_score:.0f}→{signal.layer_c_score:.0f}"
                )
                self._watchboard[key] = signal
                return True
            return False

        # Compute Layer E from historical pattern matches
        try:
            vix = self._prev_snapshot.vix if self._prev_snapshot else 15.0
            fp = build_fingerprint(
                signal,
                phase_value=self._phase_state.current_phase.value,
                vix=vix,
            )
            layer_e = self._pattern_db.compute_layer_e(fp)
            signal.layer_e_score = layer_e
        except Exception as e:
            logger.warning(f"[ConvEng] Layer E computation failed for {signal.symbol}: {e}")
            # Keep default 50.0

        self._watchboard[key] = signal
        logger.info(
            f"[ConvEng] Added {signal.symbol} {signal.direction} [{signal.strategy}] "
            f"C={signal.layer_c_score:.0f} E={signal.layer_e_score:.0f} — {signal.event_summary[:60]}"
        )
        return True

    def tick(
        self,
        market_snapshot: MarketSnapshot,
        tech_snapshots: Dict[str, object],
    ) -> List[ActiveSignal]:
        """
        Main recomputation cycle. Called every 15 minutes during market hours.

        1. Update market phase from snapshot
        2. For each signal on watchboard: recompute conviction
        3. Return list of signals that crossed threshold (≥70)

        Args:
            market_snapshot: Live Nifty/VIX/A/D/sector data
            tech_snapshots: Dict of symbol → TechnicalSnapshot for each watchboard symbol

        Returns:
            List of ActiveSignals that crossed the conviction threshold.
        """
        now = datetime.now(IST)

        # 1. Update market phase
        self._phase_state = update_phase(market_snapshot, self._phase_state)
        phase = self._phase_state.current_phase

        # 2. Expire old signals
        self._expire_stale_signals(now)

        # 3. Recompute conviction for all active signals
        triggered: List[ActiveSignal] = []
        for key, signal in list(self._watchboard.items()):
            if signal.status != "WATCHING":
                continue

            # Layer A: market state (shared, direction-aware)
            layer_a = compute_layer_a(phase, signal.direction, market_snapshot)
            # Apply morning regime bias
            layer_a = max(0.0, min(100.0, layer_a + self._morning_regime_bias))

            # Layer B: sector context (per-sector)
            layer_b = compute_layer_b(signal.symbol, market_snapshot, signal.direction)

            # Layer C: catalyst quality (FROZEN — from signal)
            # Layer E: pattern match (FROZEN — from signal)

            # Layer D: technical confirmation (per-symbol, live)
            tech_snap = tech_snapshots.get(signal.symbol)
            layer_d = compute_layer_d(tech_snap, signal.direction)

            # Weighted sum
            prev_conviction = signal.last_conviction
            new_conviction = _compute_conviction(signal, layer_a, layer_b, layer_d)

            delta = new_conviction - prev_conviction
            signal.last_conviction = new_conviction
            signal.last_evaluated_at = now
            signal.conviction_history.append(
                (now.strftime("%H:%M"), round(new_conviction, 1), phase.value)
            )

            # Log every recomputation (single line, greppable)
            logger.info(
                f"[ConvEng] {signal.symbol} {signal.direction} | "
                f"A={layer_a:.0f} B={layer_b:.0f} C={signal.layer_c_score:.0f} "
                f"D={layer_d:.0f} E={signal.layer_e_score:.0f} "
                f"→ conviction={new_conviction:.0f} | phase={phase.value} "
                f"| prev={prev_conviction:.0f} | Δ={delta:+.0f}"
            )

            # Check threshold
            if new_conviction >= self._threshold:
                signal.status = "TRIGGERED"
                triggered.append(signal)
                logger.info(
                    f"[ConvEng] *** TRIGGERED *** {signal.symbol} {signal.direction} "
                    f"conviction={new_conviction:.0f} >= {self._threshold:.0f} "
                    f"| waited {len(signal.conviction_history)} cycles"
                )

        self._prev_snapshot = market_snapshot

        if triggered:
            print(
                f"  ⚡ ConvictionEngine: {len(triggered)} signal(s) triggered "
                f"— {', '.join(s.symbol for s in triggered)}"
            )

        return triggered

    def _expire_stale_signals(self, now: datetime) -> None:
        """Expire signals that are too old or past the entry cutoff."""
        cutoff_time = now.replace(
            hour=SIGNAL_EXPIRY_TIME[0], minute=SIGNAL_EXPIRY_TIME[1], second=0
        )

        for key, signal in list(self._watchboard.items()):
            if signal.status != "WATCHING":
                continue

            # Time-based expiry
            if signal.created_at and now > cutoff_time:
                signal.status = "EXPIRED"
                logger.info(
                    f"[ConvEng] Expired {signal.symbol} {signal.direction} "
                    f"— past {SIGNAL_EXPIRY_TIME[0]}:{SIGNAL_EXPIRY_TIME[1]:02d} cutoff "
                    f"(last conviction={signal.last_conviction:.0f})"
                )
                continue

            # Age-based expiry (>4 hours)
            if signal.created_at:
                age_hours = (now - signal.created_at).total_seconds() / 3600
                if age_hours > SIGNAL_MAX_AGE_HOURS:
                    signal.status = "EXPIRED"
                    logger.info(
                        f"[ConvEng] Expired {signal.symbol} {signal.direction} "
                        f"— {age_hours:.1f}h old (max {SIGNAL_MAX_AGE_HOURS}h)"
                    )
                    continue

            # Weak catalyst expiry: if Layer C < 50, don't wait forever
            if signal.layer_c_score < 50 and signal.last_conviction < 50:
                if signal.conviction_history and len(signal.conviction_history) >= 3:
                    signal.status = "EXPIRED"
                    logger.info(
                        f"[ConvEng] Expired {signal.symbol} {signal.direction} "
                        f"— weak catalyst C={signal.layer_c_score:.0f}, "
                        f"conviction stuck at {signal.last_conviction:.0f}"
                    )

    def get_watchboard_summary(self) -> str:
        """Human-readable watchboard status for periodic logging."""
        active = [s for s in self._watchboard.values() if s.status == "WATCHING"]
        if not active:
            return "Watchboard: empty"
        parts = []
        for s in sorted(active, key=lambda x: x.last_conviction, reverse=True):
            parts.append(f"{s.symbol}({s.direction[0]})={s.last_conviction:.0f}")
        return f"Watchboard: {len(active)} signals — {', '.join(parts[:5])}"

    def get_active_signals(self) -> List[ActiveSignal]:
        """Return all WATCHING signals."""
        return [s for s in self._watchboard.values() if s.status == "WATCHING"]

    def record_eod_outcomes(self, trade_records: List[dict]) -> None:
        """
        Record pattern outcomes for all signals on the watchboard at EOD.
        Called before reset_daily().

        Args:
            trade_records: List of dicts with {symbol, direction, pnl, entry_price}
                           from today's trades (from DB or positions).
        """
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        vix = self._prev_snapshot.vix if self._prev_snapshot else 15.0
        phase_value = self._phase_state.current_phase.value

        trade_lookup = {}
        for tr in trade_records:
            key = f"{tr.get('symbol', '')}_{tr.get('direction', '')}"
            trade_lookup[key] = tr

        recorded = 0
        for key, signal in self._watchboard.items():
            try:
                fp = build_fingerprint(signal, phase_value=phase_value, vix=vix)
                trade = trade_lookup.get(key)

                if signal.status == "TRIGGERED" and trade:
                    pnl = trade.get("pnl", 0)
                    entry_price = trade.get("entry_price", 1)
                    pnl_pct = (pnl / (entry_price * trade.get("qty", 1))) * 100 if entry_price > 0 else 0
                    outcome = PatternOutcome(
                        fingerprint=fp,
                        triggered=True,
                        pnl_pct=round(pnl_pct, 2),
                        max_favorable=0.0,  # TODO: track MFE from positions
                        max_adverse=0.0,    # TODO: track MAE from positions
                        outcome="WIN" if pnl > 0 else "LOSS",
                        date=today_str,
                    )
                else:
                    outcome = PatternOutcome(
                        fingerprint=fp,
                        triggered=signal.status == "TRIGGERED",
                        pnl_pct=0.0,
                        max_favorable=0.0,
                        max_adverse=0.0,
                        outcome="EXPIRED" if signal.status != "TRIGGERED" else "WIN",
                        date=today_str,
                    )

                self._pattern_db.record_outcome(outcome)
                recorded += 1
            except Exception as e:
                logger.warning(f"[ConvEng] Failed to record outcome for {signal.symbol}: {e}")

        logger.info(f"[ConvEng] Recorded {recorded} pattern outcomes to Layer E DB")

    def reset_daily(self) -> None:
        """Clear all state for new trading day."""
        count = len(self._watchboard)
        self._watchboard.clear()
        self._phase_state = PhaseState()
        self._prev_snapshot = None
        self._morning_regime_bias = 0.0
        logger.info(f"[ConvEng] Daily reset: cleared {count} signals from watchboard")
