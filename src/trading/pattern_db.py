"""
pattern_db.py — Layer E Pattern Database
-----------------------------------------
Stores and queries historical signal outcomes to compute
Layer E (Pattern Match) scores for the conviction engine.

PatternFingerprint: strategy, direction, phase, sector,
    catalyst_type, time_bucket, vix_regime
PatternOutcome: fingerprint + triggered, pnl_pct, outcome

Layer E = win_rate * 100, clamped [20, 80].
Returns 50 if fewer than 5 historical matches.
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


@dataclass
class PatternFingerprint:
    strategy: str       # HYDRA, VIPER
    direction: str      # BUY, SHORT
    phase_at_trigger: str  # MarketPhase value at trigger/expiry
    sector: str         # From sector_guard
    catalyst_type: str  # "earnings", "acquisition", "upgrade", "momentum", "unknown"
    time_bucket: str    # "first_hour", "mid_session", "last_hour"
    vix_regime: str     # "low" (<14), "normal" (14-22), "elevated" (>22)


@dataclass
class PatternOutcome:
    fingerprint: PatternFingerprint
    triggered: bool     # Did conviction reach 70?
    pnl_pct: float      # If traded: realized PnL as % of entry. 0 if expired.
    max_favorable: float  # Maximum favorable excursion (best unrealized %)
    max_adverse: float    # Maximum adverse excursion (worst unrealized %)
    outcome: str         # "WIN", "LOSS", "EXPIRED"
    date: str            # YYYY-MM-DD


def classify_catalyst_type(event_summary: str) -> str:
    """Simple keyword classifier for catalyst type."""
    summary_lower = (event_summary or "").lower()
    if any(w in summary_lower for w in ["earning", "result", "q1", "q2", "q3", "q4", "profit", "revenue"]):
        return "earnings"
    if any(w in summary_lower for w in ["acqui", "merger", "buyout", "takeover"]):
        return "acquisition"
    if any(w in summary_lower for w in ["upgrade", "target", "rating", "broker"]):
        return "upgrade"
    if any(w in summary_lower for w in ["downgrade", "cut"]):
        return "downgrade"
    if any(w in summary_lower for w in ["momentum", "breakout", "volume", "spike"]):
        return "momentum"
    return "unknown"


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
        sector=get_sector(signal.symbol),
        catalyst_type=classify_catalyst_type(signal.event_summary),
        time_bucket=classify_time_bucket(signal.created_at),
        vix_regime=classify_vix_regime(vix),
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
        """Load existing pattern database."""
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
            f"→ {outcome.outcome} ({outcome.pnl_pct:+.2f}%)"
        )

    def compute_layer_e(self, fingerprint: PatternFingerprint) -> float:
        """
        Compute Layer E score from historical pattern matches.

        Fuzzy matching: at least 3 of 7 fingerprint fields must match.
        Returns win_rate * 100, clamped to [20, 80].
        Returns 50 if fewer than MIN_MATCHES_FOR_SCORE matches.
        """
        fp_dict = asdict(fingerprint)
        fp_fields = list(fp_dict.keys())

        matches = []
        for outcome in self._outcomes:
            stored_fp = outcome.get("fingerprint", {})
            match_count = sum(
                1 for field in fp_fields
                if fp_dict.get(field) == stored_fp.get(field)
            )
            if match_count >= 3:
                matches.append(outcome)

        if len(matches) < MIN_MATCHES_FOR_SCORE:
            return 50.0  # Cold start — insufficient data

        wins = sum(1 for m in matches if m.get("outcome") == "WIN")
        win_rate = wins / len(matches)
        score = win_rate * 100.0

        return max(20.0, min(80.0, score))

    @property
    def total_outcomes(self) -> int:
        return len(self._outcomes)

    def get_summary(self) -> str:
        """One-line summary for logging."""
        if not self._outcomes:
            return "PatternDB: empty (cold start)"
        total = len(self._outcomes)
        wins = 0
        for o in self._outcomes:
            if isinstance(o, dict) and o.get("outcome") == "WIN":
                wins += 1
        return f"PatternDB: {total} outcomes, {wins} wins ({wins/total*100:.0f}%)"
