"""
pattern_db.py — Layer E Pattern Database
-----------------------------------------
Stores and queries historical signal outcomes to compute
Layer E (Pattern Match) scores for the conviction engine.

PatternFingerprint: strategy, direction, phase, sector,
    catalyst_type, time_bucket, vix_regime, primary_type, event_type
PatternOutcome: fingerprint + triggered, pnl_pct, outcome

Valid outcomes:
  WIN               — triggered trade, positive PnL
  LOSS              — triggered trade, negative PnL
  CORRECT_NO_TRADE  — expired, price moved 1%+ in signal direction
  WRONG_NO_TRADE    — expired, price moved 1%+ against signal direction
  NO_DATA_EXPIRED   — expired, no price data available to evaluate
  LEGACY_UNCATEGORIZED — pre-2026-04-10 records with outcome=EXPIRED and pnl=0.0

Layer E = success_rate * 100, clamped [20, 80].
Returns 50 if fewer than 5 definitive historical matches.
"""
import os
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional

from src.trading.sector_guard import get_sector

logger = logging.getLogger(__name__)

PATTERN_DB_PATH = "data/pattern_db.json"
MIN_MATCHES_FOR_SCORE = 5

# Outcomes that count as positive in Layer E scoring
POSITIVE_OUTCOMES = {"WIN", "CORRECT_NO_TRADE"}
# Outcomes that count as negative in Layer E scoring
NEGATIVE_OUTCOMES = {"LOSS", "WRONG_NO_TRADE"}
# Outcomes excluded from Layer E scoring (neither positive nor negative)
EXCLUDED_OUTCOMES = {"LEGACY_UNCATEGORIZED", "NO_DATA_EXPIRED", "EXPIRED"}


@dataclass
class PatternFingerprint:
    strategy: str       # HYDRA, VIPER, VIPER-COIL
    direction: str      # BUY, SHORT
    phase_at_trigger: str  # MarketPhase value at trigger/expiry
    sector: str         # From sector_guard (with strategy fallback)
    catalyst_type: str  # "earnings", "acquisition", "upgrade", "momentum", etc.
    time_bucket: str    # "first_hour", "mid_session", "last_hour"
    vix_regime: str     # "low" (<14), "normal" (14-22), "elevated" (>22)
    primary_type: str = ""   # From signal.signal_type: MOMENTUM/SCALP/SWING
    event_type: str = ""     # From event/strategy: GAP_AND_GO/OVEREXTENDED/COIL/etc.


@dataclass
class PatternOutcome:
    fingerprint: PatternFingerprint
    triggered: bool     # Did conviction reach threshold?
    pnl_pct: float      # Realized PnL if traded; actual move% if expired
    max_favorable: float  # Maximum favorable excursion (best unrealized %)
    max_adverse: float    # Maximum adverse excursion (worst unrealized %)
    outcome: str         # WIN/LOSS/CORRECT_NO_TRADE/WRONG_NO_TRADE/NO_DATA_EXPIRED
    date: str            # YYYY-MM-DD


def classify_catalyst_type(event_summary: str) -> str:
    """Keyword classifier for catalyst type."""
    summary_lower = (event_summary or "").lower()
    if any(w in summary_lower for w in ["earning", "result", "q1", "q2", "q3", "q4", "profit", "revenue"]):
        return "earnings"
    if any(w in summary_lower for w in ["acqui", "merger", "buyout", "takeover"]):
        return "acquisition"
    if any(w in summary_lower for w in ["upgrade", "target", "rating", "broker"]):
        return "upgrade"
    if any(w in summary_lower for w in ["downgrade", "cut"]):
        return "downgrade"
    if any(w in summary_lower for w in ["gap", "open"]):
        return "gap_play"
    if any(w in summary_lower for w in ["overextended", "overbought", "oversold", "extreme"]):
        return "overextended"
    if any(w in summary_lower for w in ["coil", "squeeze", "tight", "compress"]):
        return "coil"
    if any(w in summary_lower for w in ["sector", "rotation"]):
        return "sector_rotation"
    if any(w in summary_lower for w in ["block", "promoter", "bulk", "insider"]):
        return "insider_activity"
    if any(w in summary_lower for w in ["split", "bonus", "dividend"]):
        return "corporate_action"
    if any(w in summary_lower for w in ["momentum", "breakout", "volume", "spike", "runner", "mover"]):
        return "momentum"
    return "unknown"


def classify_event_type(event_summary: str, strategy: str) -> str:
    """Extract pattern/event type from strategy name and event summary."""
    strategy_upper = (strategy or "").upper()
    summary_lower = (event_summary or "").lower()

    # Strategy name carries strong signal
    if "COIL" in strategy_upper:
        return "COIL"

    # Check event summary for move type keywords
    for keyword, event_type in [
        ("overextended", "OVEREXTENDED"),
        ("gap_and_go", "GAP_AND_GO"),
        ("gap and go", "GAP_AND_GO"),
        ("gradual_runner", "GRADUAL_RUNNER"),
        ("gradual runner", "GRADUAL_RUNNER"),
        ("sector_wave", "SECTOR_WAVE"),
        ("sector wave", "SECTOR_WAVE"),
        ("gap_and_trap", "GAP_AND_TRAP"),
        ("dead_cat", "DEAD_CAT_BOUNCE"),
        ("dead cat", "DEAD_CAT_BOUNCE"),
        ("breakout", "BREAKOUT"),
        ("reversal", "REVERSAL"),
        ("squeeze", "COIL"),
    ]:
        if keyword in summary_lower:
            return event_type

    # Fallback from strategy name
    if "HYDRA" in strategy_upper:
        return "EVENT_DRIVEN"
    if "VIPER" in strategy_upper:
        return "MOMENTUM"
    return "UNKNOWN"


def classify_time_bucket(created_at: Optional[datetime]) -> str:
    """Classify when the signal was created into a time bucket."""
    if created_at is None:
        return "mid_session"
    hour = created_at.hour
    minute = created_at.minute
    total_minutes = hour * 60 + minute
    if total_minutes < 10 * 60 + 15:  # Before 10:15
        return "first_hour"
    elif total_minutes > 14 * 60:  # After 14:00
        return "last_hour"
    return "mid_session"


def classify_vix_regime(vix: float) -> str:
    """Classify VIX into regime buckets."""
    if vix < 14:
        return "low"
    elif vix > 22:
        return "elevated"
    return "normal"


def build_fingerprint(
    signal,
    phase_value: str,
    vix: float = 15.0,
) -> PatternFingerprint:
    """
    Build a PatternFingerprint from an ActiveSignal and market context.

    Args:
        signal: ActiveSignal instance
        phase_value: MarketPhase.value string at the time of trigger/expiry
        vix: Current VIX value
    """
    return PatternFingerprint(
        strategy=signal.strategy,
        direction=signal.direction,
        phase_at_trigger=phase_value,
        sector=get_sector(signal.symbol, strategy=signal.strategy),
        catalyst_type=classify_catalyst_type(signal.event_summary),
        time_bucket=classify_time_bucket(signal.created_at),
        vix_regime=classify_vix_regime(vix),
        primary_type=getattr(signal, 'signal_type', 'MOMENTUM'),
        event_type=classify_event_type(signal.event_summary, signal.strategy),
    )


class PatternDB:
    """
    Manages the pattern outcome database for Layer E scoring.
    Append-only storage in data/pattern_db.json.
    """

    def __init__(self, path: str = PATTERN_DB_PATH):
        self._path = path
        self._outcomes: List[dict] = []
        self._load()

    def _load(self) -> None:
        """Load existing pattern database and migrate legacy records."""
        if os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    data = json.load(f)
                # Handle both list format and legacy dict format
                if isinstance(data, list):
                    self._outcomes = data
                elif isinstance(data, dict):
                    self._outcomes = data.get("entries", data.get("outcomes", []))
                else:
                    self._outcomes = []
                logger.info(f"[PatternDB] Loaded {len(self._outcomes)} historical outcomes")
            except Exception as e:
                logger.warning(f"[PatternDB] Load failed: {e} — starting fresh")
                self._outcomes = []
        else:
            self._outcomes = []
            logger.info("[PatternDB] No existing pattern DB — starting fresh")

        # Migrate legacy records (idempotent)
        migrated = self._migrate_legacy_records()
        if migrated > 0:
            logger.info(f"[PatternDB] Migrated {migrated} legacy records to LEGACY_UNCATEGORIZED")

    def _migrate_legacy_records(self) -> int:
        """Mark pre-fix records as LEGACY_UNCATEGORIZED so they are excluded from Layer E scoring."""
        migrated = 0
        for record in self._outcomes:
            if record.get("outcome") == "LEGACY_UNCATEGORIZED":
                continue  # Already migrated
            if (record.get("outcome") == "EXPIRED"
                    and record.get("pnl_pct", 0) == 0.0
                    and record.get("date", "9999") < "2026-04-10"):
                record["outcome"] = "LEGACY_UNCATEGORIZED"
                # Backfill new fingerprint fields if missing
                fp = record.get("fingerprint", {})
                if "primary_type" not in fp:
                    fp["primary_type"] = ""
                if "event_type" not in fp:
                    fp["event_type"] = ""
                migrated += 1
        if migrated > 0:
            self._save()
        return migrated

    def _save(self) -> None:
        """Persist outcomes to disk."""
        os.makedirs(os.path.dirname(self._path) or "data", exist_ok=True)
        try:
            with open(self._path, "w") as f:
                json.dump(self._outcomes, f, indent=2)
        except Exception as e:
            logger.error(f"[PatternDB] Save failed: {e}")

    def record_outcome(self, outcome: PatternOutcome) -> None:
        """Append a new outcome to the database."""
        self._outcomes.append({
            "fingerprint": asdict(outcome.fingerprint),
            "triggered": outcome.triggered,
            "pnl_pct": outcome.pnl_pct,
            "max_favorable": outcome.max_favorable,
            "max_adverse": outcome.max_adverse,
            "outcome": outcome.outcome,
            "date": outcome.date,
        })
        self._save()
        logger.info(
            f"[PatternDB] Recorded: {outcome.fingerprint.strategy} "
            f"{outcome.fingerprint.direction} {outcome.fingerprint.sector} "
            f"{outcome.fingerprint.event_type} "
            f"→ {outcome.outcome} ({outcome.pnl_pct:+.2f}%)"
        )

    def compute_layer_e(self, fingerprint: PatternFingerprint) -> float:
        """
        Compute Layer E score from historical pattern matches.

        Fuzzy matching: at least 3 of 9 fingerprint fields must match.
        Only counts definitive outcomes (WIN/LOSS/CORRECT_NO_TRADE/WRONG_NO_TRADE).
        Excludes LEGACY_UNCATEGORIZED and NO_DATA_EXPIRED from scoring.

        Returns success_rate * 100, clamped to [20, 80].
        Returns 60 if fewer than MIN_MATCHES_FOR_SCORE definitive matches (warm cold-start).
        """
        fp_dict = asdict(fingerprint)
        fp_fields = list(fp_dict.keys())

        matches = []
        for outcome in self._outcomes:
            stored_fp = outcome.get("fingerprint", {})
            match_count = sum(
                1 for f in fp_fields
                if fp_dict.get(f) and fp_dict.get(f) == stored_fp.get(f)
            )
            if match_count >= 3:
                matches.append(outcome)

        # Filter to definitive outcomes only
        definitive = [
            m for m in matches
            if m.get("outcome") in POSITIVE_OUTCOMES | NEGATIVE_OUTCOMES
        ]

        if len(definitive) < MIN_MATCHES_FOR_SCORE:
            return 60.0  # Warm cold-start — matches ActiveSignal.layer_e_score default

        positive = sum(1 for m in definitive if m.get("outcome") in POSITIVE_OUTCOMES)
        negative = sum(1 for m in definitive if m.get("outcome") in NEGATIVE_OUTCOMES)

        if positive + negative == 0:
            return 50.0

        success_rate = positive / (positive + negative)
        score = success_rate * 100.0

        logger.info(
            f"[LayerE] Pattern {fingerprint.strategy} {fingerprint.event_type}: "
            f"{positive} correct / {negative} wrong → score {score:.0f}"
        )

        return max(20.0, min(80.0, score))

    @property
    def total_outcomes(self) -> int:
        return len(self._outcomes)

    def get_summary(self) -> str:
        """One-line summary for logging."""
        if not self._outcomes:
            return "PatternDB: empty (cold start)"
        total = len(self._outcomes)
        active = sum(1 for o in self._outcomes if isinstance(o, dict)
                     and o.get("outcome") not in EXCLUDED_OUTCOMES)
        wins = sum(1 for o in self._outcomes if isinstance(o, dict)
                   and o.get("outcome") in POSITIVE_OUTCOMES)
        return f"PatternDB: {total} total, {active} active, {wins} positive outcomes"
