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
import json
import logging
import os
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

# Catalyst-driven signal weights (HYDRA, VIPER-COIL)
# Rationale: catalyst quality dominates; market phase matters less for
# event-driven stocks whose movement is independent of the index.
W_A_CATALYST = 0.10  # Market state (reduced from 0.25)
W_B_CATALYST = 0.15  # Sector context (unchanged)
W_C_CATALYST = 0.45  # Catalyst quality (increased from 0.30)
W_D_CATALYST = 0.20  # Technical confirmation (unchanged)
W_E_CATALYST = 0.10  # Pattern match (unchanged)

def _load_conviction_threshold() -> float:
    try:
        from src.config_loader import get_conviction_threshold
        return float(get_conviction_threshold())
    except Exception:
        return 70.0


CONVICTION_THRESHOLD = _load_conviction_threshold()
SIGNAL_MAX_AGE_HOURS = 4.0
SIGNAL_EXPIRY_TIME = (14, 30)  # 14:30 IST — no new entries last hour

# ── Asset Class Deduplication ──────────────────────────────────────────────
# Keyword-based groups: if a symbol contains a keyword, it belongs to that class.
# When multiple signals from the same class exist, keep only the highest conviction.
ASSET_CLASS_GROUPS = {
    "SILVER": ["SILVER", "SILVERBEES", "SILVRETF", "HDFCSILVER", "AXISILVER",
               "SBISILVER", "MOSILVER", "SILVERIETF"],
    "GOLD": ["GOLD", "GOLDBEES", "GOLDIETF", "AXISGOLD", "HDFCGOLD",
             "SBIGOLD", "LICMFGOLD"],
    "NIFTY_ETF": ["NIFTYBEES", "JUNIORBEES", "NIFTYIETF", "UTINIFTY"],
    "BANKNIFTY_ETF": ["BANKBEES", "BANKIETF"],
}
ASSET_CLASS_DEDUP = True


def _get_asset_class(symbol: str) -> Optional[str]:
    """Return asset class name if symbol belongs to a known group, else None."""
    sym_upper = symbol.upper()
    for class_name, members in ASSET_CLASS_GROUPS.items():
        if sym_upper in members:
            return class_name
    return None


def classify_signal_type(metadata: dict) -> str:
    """
    Classify signal by expected valid time window using ATR% and gap%.

    SCALP    — valid < 5 min: high gap + low ATR (fast mean-reversion)
    MOMENTUM — valid 5–30 min: typical momentum setup
    SWING    — valid > 30 min: high ATR or low-gap directional move
    """
    atr_pct = float(metadata.get("atr_pct", 2.0))
    gap_pct = abs(float(metadata.get("gap_pct", 0.0)))
    if gap_pct > 2.0 and atr_pct < 1.5:
        return "SCALP"
    if atr_pct > 3.0:
        return "SWING"
    return "MOMENTUM"


@dataclass
class ActiveSignal:
    """A signal on the conviction watchboard, recomputed every cycle."""
    symbol: str
    direction: str              # "BUY" or "SHORT"
    strategy: str               # "HYDRA", "VIPER", "V2_DISCOVERY", "VIPER-COIL"
    layer_c_score: float        # Catalyst quality (0–100, FROZEN)
    layer_e_score: float = 50.0 # Pattern match (cold start at 50)
    event_summary: str = ""
    created_at: Optional[datetime] = None
    last_evaluated_at: Optional[datetime] = None
    conviction_history: List[Tuple[str, float, str]] = field(default_factory=list)
    # Each entry: (timestamp_str, conviction, phase_name)
    status: str = "WATCHING"    # WATCHING, TRIGGERED, EXPIRED
    last_conviction: float = 0.0
    peak_conviction: float = 0.0   # Highest conviction ever recorded for this signal
    expiry_reason: str = ""        # Set at each expiry site for Section 5 display
    metadata: dict = field(default_factory=dict)  # strategy-specific extras
    is_dry_run: bool = False    # True for COIL/observation-only — NEVER executes live
    window_status: str = "ACTIVE"  # ACTIVE, EXPIRED, MISSED
    signal_type: str = "MOMENTUM"  # SCALP, MOMENTUM, SWING

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(IST)
        # Auto-classify on creation if metadata is available
        if self.metadata:
            self.signal_type = classify_signal_type(self.metadata)


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


def _is_catalyst_signal(signal: ActiveSignal) -> bool:
    """Catalyst-driven signals use adjusted weights favoring Layer C."""
    return signal.strategy in ("HYDRA", "VIPER-COIL")


def _compute_sector_independence_boost(
    signal: ActiveSignal, snapshot: MarketSnapshot, phase: MarketPhase,
) -> float:
    """
    Boost Layer A when stock's sector is independent of market weakness.

    Only applies in CHOPPY or TRENDING_BEAR (not PANIC — correlations
    spike in freefall). Returns a continuous 0–20 boost based on how
    much the stock's sector outperforms the weak market.

    Scale: +0.5% outperformance → +5, +1% → +10, +2% → +20 (capped).
    Returns 0.0 on any error or missing data — never crashes.
    """
    try:
        if phase not in (MarketPhase.CHOPPY, MarketPhase.TRENDING_BEAR):
            return 0.0
        sector = get_sector(signal.symbol)
        sector_chg = snapshot.sector_changes.get(sector)
        if sector_chg is None:
            return 0.0  # Unmapped sector or no data → no boost
        rel = sector_chg - snapshot.nifty_pct
        if rel <= 0:
            return 0.0
        return min(20.0, rel * 10.0)
    except Exception:
        return 0.0


def apply_filing_metadata_adjustment(
    base_score: float,
    signal_or_entry,
    config: Optional[dict] = None,
) -> Tuple[float, List[str]]:
    """
    Apply the additive filing-metadata adjustment on top of the weighted 5-layer
    conviction score. Duck-typed so the same helper works for both:
      * ActiveSignal — reads from .metadata dict
      * WatchlistEntry — reads first-class attributes

    Two contributions (in order):
      1. Scanner-stamped conviction_modifier (PRISTINE/FRESH freshness bonus
         + L4 CONFIRMED bonus / NO_DATA penalty, already packed in event_scanner).
      2. Quality bonus/penalty based on event_quality_score:
         quality ≥ 80 → +high_quality_conviction_bonus (default +6)
         quality < 55 → -low_quality_conviction_penalty (default -15, defensive)
         55 ≤ quality < 80 → 0 (neutral)

    Final score is clamped to [0, 100].

    Returns:
        (adjusted_score, reasons_list) — reasons is list of human-readable
        contributions for the greppable [ConvEng] log line.
    """
    if config is None:
        try:
            from src.config_loader import get_conviction_filing_metadata_config
            config = get_conviction_filing_metadata_config()
        except Exception:
            config = {
                "high_quality_conviction_bonus": 6,
                "low_quality_conviction_penalty": 15,
                "apply_enabled": True,
            }

    reasons: List[str] = []
    adjusted = float(base_score)

    if not bool(config.get("apply_enabled", True)):
        return adjusted, reasons

    # Duck-type read: first-class attrs first (WatchlistEntry), then .metadata (ActiveSignal).
    meta = getattr(signal_or_entry, "metadata", None) or {}

    def _lookup(name):
        val = getattr(signal_or_entry, name, None)
        # Dataclass defaults (e.g. conviction_modifier=0) count as "set" — only fall
        # through to metadata when the attribute simply doesn't exist or is None.
        if val is None:
            val = meta.get(name)
        elif name == "conviction_modifier" and val == 0 and "conviction_modifier" in meta:
            # Prefer metadata if first-class is still default-zero and metadata has a value.
            val = meta.get("conviction_modifier", 0)
        return val

    conviction_mod = _lookup("conviction_modifier")
    quality = _lookup("event_quality_score")
    freshness = _lookup("filing_freshness")

    # 1. Scanner-stamped modifier (freshness + L4 confirmation, already packed)
    if conviction_mod not in (None, 0):
        try:
            mod = int(conviction_mod)
        except (TypeError, ValueError):
            mod = 0
        if mod != 0:
            adjusted += mod
            tag = freshness or "filing"
            reasons.append(f"{tag}+L4 {mod:+d}")

    # 2. Quality bonus/penalty
    if quality is not None:
        try:
            q = float(quality)
        except (TypeError, ValueError):
            q = None  # type: ignore[assignment]
        if q is not None:
            if q >= 80.0:
                bonus = int(config.get("high_quality_conviction_bonus", 6))
                if bonus:
                    adjusted += bonus
                    reasons.append(f"quality={q:.0f} +{bonus}")
            elif q < 55.0:
                penalty = int(config.get("low_quality_conviction_penalty", 15))
                if penalty:
                    adjusted -= penalty
                    reasons.append(f"quality={q:.0f} -{penalty}")
            # 55 ≤ q < 80 → neutral, no reason line

    adjusted = max(0.0, min(100.0, adjusted))
    return adjusted, reasons


def _compute_conviction(
    signal: ActiveSignal,
    layer_a: float,
    layer_b: float,
    layer_d: float,
) -> float:
    """
    Compute the weighted conviction score from all 5 layers.

    Catalyst-driven signals (HYDRA, VIPER-COIL) use adjusted weights
    that favor Layer C (catalyst quality) over Layer A (market state).
    Momentum signals (VIPER) use the original balanced weights.

    Returns: float 0–100
    """
    if _is_catalyst_signal(signal):
        raw = (
            layer_a * W_A_CATALYST
            + layer_b * W_B_CATALYST
            + signal.layer_c_score * W_C_CATALYST
            + layer_d * W_D_CATALYST
            + signal.layer_e_score * W_E_CATALYST
        )
    else:
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

    def __init__(self, threshold: Optional[float] = None):
        self._watchboard: Dict[str, ActiveSignal] = {}  # keyed by symbol
        self._phase_state = PhaseState()
        self._threshold = float(threshold) if threshold is not None else _load_conviction_threshold()
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

        # Asset class deduplication: keep only highest conviction per asset class
        if ASSET_CLASS_DEDUP:
            new_class = _get_asset_class(signal.symbol)
            if new_class:
                same_class = [
                    (k, s) for k, s in self._watchboard.items()
                    if s.status == "WATCHING" and _get_asset_class(s.symbol) == new_class
                ]
                if same_class:
                    # Keep existing if it has higher catalyst score
                    best_existing = max(same_class, key=lambda x: x[1].layer_c_score)
                    if signal.layer_c_score <= best_existing[1].layer_c_score:
                        logger.info(
                            f"[ConvEng] Deduplicated {new_class}: rejecting {signal.symbol} "
                            f"{signal.direction} (C={signal.layer_c_score:.0f}) — "
                            f"keeping {best_existing[1].symbol} (C={best_existing[1].layer_c_score:.0f})"
                        )
                        return False
                    # New signal is better: remove all existing from same class
                    dropped = []
                    for old_key, old_sig in same_class:
                        dropped.append(old_sig.symbol)
                        del self._watchboard[old_key]
                    logger.info(
                        f"[ConvEng] Deduplicated {len(dropped)} {new_class} signals "
                        f"→ keeping {signal.symbol} {signal.direction} "
                        f"(highest C={signal.layer_c_score:.0f})"
                    )

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

        # Capture detection price for mid-session move tracking
        try:
            from cache.data_cache import CacheManager
            _cache = CacheManager()
            _ltp = _cache.read_ltp(signal.symbol, max_age_seconds=120)
            if _ltp:
                signal.metadata["detection_price"] = float(_ltp)
        except Exception:
            pass  # Non-critical — signal works fine without detection_price

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

            # Sector independence boost for catalyst signals in weak markets
            sec_boost = 0.0
            if _is_catalyst_signal(signal):
                sec_boost = _compute_sector_independence_boost(
                    signal, market_snapshot, phase
                )
                layer_a = min(100.0, layer_a + sec_boost)

            # Layer B: sector context (per-sector)
            layer_b = compute_layer_b(signal.symbol, market_snapshot, signal.direction)

            # Layer C: catalyst quality (FROZEN — from signal)
            # Layer E: pattern match (FROZEN — from signal)

            # Layer D: technical confirmation (per-symbol, live)
            tech_snap = tech_snapshots.get(signal.symbol)
            layer_d = compute_layer_d(tech_snap, signal.direction)

            # Weighted sum
            prev_conviction = signal.last_conviction
            raw_conviction = _compute_conviction(signal, layer_a, layer_b, layer_d)

            # Additive filing-metadata adjustment (freshness + L4 + quality).
            # No-op for signals that don't carry filing metadata (pre-4-layer callers).
            new_conviction, adj_reasons = apply_filing_metadata_adjustment(
                raw_conviction, signal,
            )
            adj_delta = new_conviction - raw_conviction

            delta = new_conviction - prev_conviction
            signal.last_conviction = new_conviction
            if new_conviction > signal.peak_conviction:
                signal.peak_conviction = new_conviction
            signal.last_evaluated_at = now
            signal.conviction_history.append(
                (now.strftime("%H:%M"), round(new_conviction, 1), phase.value)
            )

            # Log every recomputation (single line, greppable)
            weight_tag = "CAT" if _is_catalyst_signal(signal) else "MOM"
            boost_str = f" boost={sec_boost:+.0f}" if sec_boost > 0 else ""
            if adj_reasons:
                adj_str = (
                    f" | filing_adj={adj_delta:+.0f} ["
                    + ", ".join(adj_reasons) + "]"
                )
            else:
                adj_str = ""
            logger.info(
                f"[ConvEng] {signal.symbol} {signal.direction} [{weight_tag}] | "
                f"A={layer_a:.0f}{boost_str} B={layer_b:.0f} C={signal.layer_c_score:.0f} "
                f"D={layer_d:.0f} E={signal.layer_e_score:.0f} "
                f"→ conv={new_conviction:.0f} | phase={phase.value} "
                f"| prev={prev_conviction:.0f} | Δ={delta:+.0f}"
                f"{adj_str}"
            )

            # Check threshold — dry-run signals NEVER trigger execution
            if new_conviction >= self._threshold and not signal.is_dry_run:
                signal.status = "TRIGGERED"
                triggered.append(signal)
                logger.info(
                    f"[ConvEng] *** TRIGGERED *** {signal.symbol} {signal.direction} "
                    f"conviction={new_conviction:.0f} >= {self._threshold:.0f} "
                    f"| waited {len(signal.conviction_history)} cycles"
                )
            elif new_conviction >= self._threshold and signal.is_dry_run:
                logger.info(
                    f"[ConvEng] [DRY-RUN] {signal.symbol} {signal.direction} "
                    f"conviction={new_conviction:.0f} would trigger but is_dry_run=True — observing only"
                )
            else:
                try:
                    from src.config_loader import get_conviction_gate_log
                    _gate_log_on = get_conviction_gate_log()
                except Exception:
                    _gate_log_on = True
                if _gate_log_on:
                    logger.info(
                        f"[CONVICTION GATE] {signal.symbol} dropped — score {new_conviction:.0f} < {self._threshold:.0f}"
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
                signal.expiry_reason = "Aged out (never triggered)"
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
                    signal.expiry_reason = "Aged out (never triggered)"
                    logger.info(
                        f"[ConvEng] Expired {signal.symbol} {signal.direction} "
                        f"— {age_hours:.1f}h old (max {SIGNAL_MAX_AGE_HOURS}h)"
                    )
                    continue

            # Weak catalyst expiry: if Layer C < 50, don't wait forever
            if signal.layer_c_score < 50 and signal.last_conviction < 50:
                if signal.conviction_history and len(signal.conviction_history) >= 3:
                    signal.status = "EXPIRED"
                    signal.expiry_reason = "Weak catalyst (conviction stuck)"
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

    def record_eod_outcomes(
        self,
        trade_records: List[dict],
        price_map: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Record pattern outcomes for all signals on the watchboard at EOD.
        Called before reset_daily().

        Args:
            trade_records: List of dicts with {symbol, direction, pnl, entry_price}
                           from today's trades (from DB or positions).
            price_map: Dict of symbol → current EOD price for evaluating expired signals.
                       If not provided, expired signals get outcome=NO_DATA_EXPIRED.
        """
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        vix = self._prev_snapshot.vix if self._prev_snapshot else 15.0
        phase_value = self._phase_state.current_phase.value
        price_map = price_map or {}

        trade_lookup = {}
        for tr in trade_records:
            tkey = f"{tr.get('symbol', '')}_{tr.get('direction', '')}"
            trade_lookup[tkey] = tr

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
                        max_favorable=0.0,
                        max_adverse=0.0,
                        outcome="WIN" if pnl > 0 else "LOSS",
                        date=today_str,
                    )
                else:
                    # Expired signal: evaluate actual price movement
                    entry_price = float(signal.metadata.get("entry_price", 0) or 0)
                    current_price = price_map.get(signal.symbol, 0)

                    if entry_price > 0 and current_price > 0:
                        move_pct = (current_price - entry_price) / entry_price * 100
                        directional_move = move_pct if signal.direction == "BUY" else -move_pct

                        if directional_move >= 1.0:
                            outcome_str = "CORRECT_NO_TRADE"
                        elif directional_move <= -1.0:
                            outcome_str = "WRONG_NO_TRADE"
                        else:
                            outcome_str = "CORRECT_NO_TRADE" if directional_move >= 0 else "WRONG_NO_TRADE"

                        outcome = PatternOutcome(
                            fingerprint=fp,
                            triggered=False,
                            pnl_pct=round(move_pct, 2),
                            max_favorable=0.0,
                            max_adverse=0.0,
                            outcome=outcome_str,
                            date=today_str,
                        )
                        logger.info(
                            f"[ConvEng] {signal.symbol} {signal.direction} expired: "
                            f"entry={entry_price:.2f} eod={current_price:.2f} "
                            f"move={move_pct:+.2f}% → {outcome_str}"
                        )
                    else:
                        outcome = PatternOutcome(
                            fingerprint=fp,
                            triggered=False,
                            pnl_pct=0.0,
                            max_favorable=0.0,
                            max_adverse=0.0,
                            outcome="NO_DATA_EXPIRED",
                            date=today_str,
                        )
                        logger.warning(
                            f"[ConvEng] {signal.symbol} expired: no price data "
                            f"(entry={entry_price}, eod={current_price}) → NO_DATA_EXPIRED"
                        )

                self._pattern_db.record_outcome(outcome)
                recorded += 1
            except Exception as e:
                logger.warning(f"[ConvEng] Failed to record outcome for {signal.symbol}: {e}")

        logger.info(f"[ConvEng] Recorded {recorded} pattern outcomes to Layer E DB")

    def update_window_statuses(self, price_map: Dict[str, float]) -> None:
        """
        Update window_status for all watchboard signals based on current prices.

        MISSED  = price moved >0.5% in the signal direction but conviction never triggered
        EXPIRED = signal already expired without triggering
        ACTIVE  = signal still valid and watching
        """
        for key, signal in self._watchboard.items():
            if signal.status == "EXPIRED":
                signal.window_status = "EXPIRED"
                continue
            if signal.status == "TRIGGERED":
                signal.window_status = "ACTIVE"
                continue
            # WATCHING — check if price has moved past entry zone
            current_price = price_map.get(signal.symbol)
            if current_price and signal.metadata.get("entry_price"):
                entry = float(signal.metadata["entry_price"])
                if entry > 0:
                    move_pct = (current_price - entry) / entry * 100
                    if signal.direction == "BUY" and move_pct > 0.5:
                        signal.window_status = "MISSED"
                    elif signal.direction == "SHORT" and move_pct < -0.5:
                        signal.window_status = "MISSED"
                    else:
                        signal.window_status = "ACTIVE"

    def persist_watchboard_to_json(self) -> str:
        """
        Persist all watchboard signals to a daily JSON file for post-mortem analysis.
        Called at EOD before reset_daily(). Returns the file path.
        """
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        data = []
        for key, sig in self._watchboard.items():
            data.append({
                "symbol": sig.symbol,
                "direction": sig.direction,
                "strategy": sig.strategy,
                "status": sig.status,
                "window_status": sig.window_status,
                "signal_type": sig.signal_type,
                "is_dry_run": sig.is_dry_run,
                "created_at": sig.created_at.isoformat() if sig.created_at else None,
                "last_conviction": sig.last_conviction,
                "layer_c_score": sig.layer_c_score,
                "layer_e_score": sig.layer_e_score,
                "conviction_history": list(sig.conviction_history),
                "event_summary": sig.event_summary,
                "metadata": {k: v for k, v in sig.metadata.items()
                             if isinstance(v, (str, int, float, bool, type(None)))},
            })
        path = os.path.join("logs", "conviction_signals", f"{today_str}_signals.json")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            logger.info(f"[ConvEng] Persisted {len(data)} signals to {path}")
        except Exception as e:
            logger.error(f"[ConvEng] Failed to persist watchboard: {e}")
        return path

    def reset_daily(self) -> None:
        """Clear all state for new trading day."""
        count = len(self._watchboard)
        self._watchboard.clear()
        self._phase_state = PhaseState()
        self._prev_snapshot = None
        self._morning_regime_bias = 0.0
        logger.info(f"[ConvEng] Daily reset: cleared {count} signals from watchboard")
