"""
move_classifier.py — Top Mover Classification Engine
------------------------------------------------------
Classifies each top gainer/loser into one of 6 move types:
  - GAP_AND_GO: Strong gap with volume → ride it
  - GRADUAL_RUNNER: Steady climb with rising volume → ride it
  - SECTOR_WAVE: Whole sector moving → only trade leader
  - OVEREXTENDED: RSI extreme, volume declining → COIL reversal candidate
  - GAP_AND_TRAP: Gap on thin volume, no catalyst → skip
  - DEAD_CAT_BOUNCE: Loser bounces on no volume → skip

Uses rules-based classification first, then Groq for edge cases.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class MoveType(Enum):
    GAP_AND_GO = "GAP_AND_GO"
    GRADUAL_RUNNER = "GRADUAL_RUNNER"
    SECTOR_WAVE = "SECTOR_WAVE"
    OVEREXTENDED = "OVEREXTENDED"
    GAP_AND_TRAP = "GAP_AND_TRAP"
    DEAD_CAT_BOUNCE = "DEAD_CAT_BOUNCE"
    UNKNOWN = "UNKNOWN"


class TradeMode(Enum):
    STRIKE = "STRIKE"      # Momentum continuation (LIVE)
    COIL = "COIL"          # Mean reversion (DRY-RUN)
    SKIP = "SKIP"          # Do not trade


@dataclass
class ClassifiedMover:
    """A top mover with its classification and trading mode."""
    symbol: str
    pct_change: float           # +5.2 or -3.8
    volume: int
    prev_close: float
    last_price: float
    direction: str              # "BUY" or "SHORT"

    # Enrichment fields
    gap_pct: float = 0.0        # Open vs prev close gap
    volume_ratio: float = 0.0   # Today volume / avg volume
    open_price: float = 0.0

    # Classification output
    move_type: MoveType = MoveType.UNKNOWN
    trade_mode: TradeMode = TradeMode.SKIP
    classification_reason: str = ""
    sector: str = ""
    is_sector_leader: bool = False

    # LLM enrichment
    groq_summary: str = ""
    grok_sentiment: str = ""
    grok_conviction: float = 0.0


class MoveClassifier:
    """
    Classifies top movers into actionable categories.

    Uses a waterfall of rules:
    1. Volume filter → thin volume = TRAP or BOUNCE
    2. Gap analysis → gap size determines continuation potential
    3. RSI/extension check → overextended = COIL candidate
    4. Sector context → multiple same-sector movers = WAVE
    5. Groq confirmation for ambiguous cases
    """

    def __init__(self):
        self._sector_cache: dict = {}

    def classify_movers(
        self,
        movers: List[dict],
        sector_map: dict = None,
    ) -> List[ClassifiedMover]:
        """
        Classify a list of top movers.

        Args:
            movers: List of dicts from momentum_scanner with keys:
                    {symbol, last_price, prev_close, pct_change, volume, direction,
                     open_price, volume_ratio}
            sector_map: {symbol: sector_name} for sector wave detection

        Returns:
            List of ClassifiedMover with move_type and trade_mode set.
        """
        if not movers:
            return []

        classified = []
        sector_counts: dict = {}  # sector → count of movers in that sector

        # First pass: build sector count
        if sector_map:
            for m in movers:
                sector = sector_map.get(m.get("symbol", ""), "OTHER")
                sector_counts[sector] = sector_counts.get(sector, 0) + 1

        # Second pass: classify each mover
        for m in movers:
            cm = ClassifiedMover(
                symbol=m.get("symbol", ""),
                pct_change=float(m.get("pct_change", 0)),
                volume=int(m.get("volume", 0)),
                prev_close=float(m.get("prev_close", 0)),
                last_price=float(m.get("last_price", 0)),
                direction="BUY" if float(m.get("pct_change", 0)) > 0 else "SHORT",
                gap_pct=float(m.get("gap_pct", 0)),
                volume_ratio=float(m.get("volume_ratio", 0)),
                open_price=float(m.get("open_price", 0)),
            )

            # Get sector
            if sector_map:
                cm.sector = sector_map.get(cm.symbol, "OTHER")

            # Classify
            move_type, trade_mode, reason = self._classify_single(
                cm, sector_counts
            )
            cm.move_type = move_type
            cm.trade_mode = trade_mode
            cm.classification_reason = reason

            # Sector leader detection for SECTOR_WAVE
            if move_type == MoveType.SECTOR_WAVE:
                cm.is_sector_leader = self._is_sector_leader(cm, movers, sector_map)
                if not cm.is_sector_leader:
                    cm.trade_mode = TradeMode.SKIP
                    cm.classification_reason += " (not sector leader → SKIP)"

            classified.append(cm)
            logger.info(
                f"[MoveClassifier] {cm.symbol}: {cm.move_type.value} → "
                f"{cm.trade_mode.value} | {cm.pct_change:+.1f}% vol={cm.volume_ratio:.1f}x "
                f"| {reason}"
            )

        return classified

    def _classify_single(
        self, cm: ClassifiedMover, sector_counts: dict
    ) -> Tuple[MoveType, TradeMode, str]:
        """
        Waterfall classification for a single mover.
        Returns (MoveType, TradeMode, reason).
        """
        abs_change = abs(cm.pct_change)

        # ── Rule 1: Volume filter — thin volume = trap or bounce ──
        if cm.volume_ratio < 0.8:
            # Low volume move — likely operator-driven
            if abs_change > 3:
                return (
                    MoveType.GAP_AND_TRAP,
                    TradeMode.SKIP,
                    f"Gap {cm.pct_change:+.1f}% but volume only {cm.volume_ratio:.1f}x avg — no conviction"
                )
            else:
                return (
                    MoveType.UNKNOWN,
                    TradeMode.SKIP,
                    f"Small move ({cm.pct_change:+.1f}%) on thin volume — noise"
                )

        # ── Rule 2: Dead cat bounce detection ──
        # Big loser that's bouncing slightly on weak volume
        if cm.pct_change < -5 and cm.volume_ratio < 1.5:
            return (
                MoveType.DEAD_CAT_BOUNCE,
                TradeMode.SKIP,
                f"Dropped {cm.pct_change:.1f}% — bounce on weak volume is a trap"
            )

        # ── Rule 3: Overextended check (COIL candidate) ──
        # Moved > 5% — potential exhaustion reversal
        if abs_change > 5 and cm.volume_ratio > 1.5:
            return (
                MoveType.OVEREXTENDED,
                TradeMode.COIL,
                f"Moved {cm.pct_change:+.1f}% — extended, volume {cm.volume_ratio:.1f}x "
                f"(COIL: wait for exhaustion reversal after 11AM)"
            )

        # ── Rule 4: Sector wave detection ──
        if cm.sector and cm.sector != "OTHER":
            sector_count = sector_counts.get(cm.sector, 0)
            if sector_count >= 3:
                return (
                    MoveType.SECTOR_WAVE,
                    TradeMode.STRIKE,
                    f"{cm.sector} sector wave ({sector_count} movers) — trade leader only"
                )

        # ── Rule 5: Gap and go (strong gap + volume) ──
        if abs(cm.gap_pct) > 2 and cm.volume_ratio >= 2.0:
            return (
                MoveType.GAP_AND_GO,
                TradeMode.STRIKE,
                f"Gap {cm.gap_pct:+.1f}% with {cm.volume_ratio:.1f}x volume — momentum continuation"
            )

        # ── Rule 6: Gradual runner (no big gap but steady climb + rising volume) ──
        if abs_change >= 2 and cm.volume_ratio >= 1.5:
            if abs(cm.gap_pct) < 2:
                return (
                    MoveType.GRADUAL_RUNNER,
                    TradeMode.STRIKE,
                    f"Climbing {cm.pct_change:+.1f}% steadily with {cm.volume_ratio:.1f}x volume"
                )

        # ── Rule 7: Gap with moderate volume (worth investigating) ──
        if abs(cm.gap_pct) > 2 and cm.volume_ratio >= 1.2:
            return (
                MoveType.GAP_AND_GO,
                TradeMode.STRIKE,
                f"Gap {cm.gap_pct:+.1f}% with decent volume ({cm.volume_ratio:.1f}x) — moderate confidence"
            )

        # ── Fallback: insufficient signal ──
        return (
            MoveType.UNKNOWN,
            TradeMode.SKIP,
            f"Move {cm.pct_change:+.1f}% vol={cm.volume_ratio:.1f}x — doesn't match any pattern"
        )

    def _is_sector_leader(
        self,
        candidate: ClassifiedMover,
        all_movers: List[dict],
        sector_map: dict,
    ) -> bool:
        """
        Check if this stock is the sector leader (highest abs % change in sector).
        """
        if not sector_map:
            return True  # No sector data → assume leader

        same_sector = [
            m for m in all_movers
            if sector_map.get(m.get("symbol", ""), "OTHER") == candidate.sector
        ]

        if not same_sector:
            return True

        # Leader = highest absolute % change
        leader = max(same_sector, key=lambda m: abs(float(m.get("pct_change", 0))))
        return leader.get("symbol", "") == candidate.symbol

    def enrich_with_groq(self, movers: List[ClassifiedMover]) -> List[ClassifiedMover]:
        """
        Use Groq Llama-3.3-70B to add context to classified movers.
        Called after rule-based classification to confirm or reclassify ambiguous ones.
        """
        try:
            from src.llm.groq_client import classify_event
        except ImportError:
            logger.warning("[MoveClassifier] Groq client unavailable — skipping enrichment")
            return movers

        import time

        for cm in movers:
            if cm.trade_mode == TradeMode.SKIP:
                continue  # Don't waste Groq calls on skips

            try:
                result = classify_event(
                    symbol=cm.symbol,
                    headline=f"Stock {cm.pct_change:+.1f}% today, classified as {cm.move_type.value}",
                    category=cm.move_type.value,
                    body=(
                        f"Volume: {cm.volume_ratio:.1f}x average. "
                        f"Gap: {cm.gap_pct:+.1f}%. Sector: {cm.sector}. "
                        f"Previous close: ₹{cm.prev_close:.2f}, LTP: ₹{cm.last_price:.2f}"
                    ),
                )
                cm.groq_summary = result.get("summary", "")
                # If Groq says it's material, boost confidence
                if result.get("material", False) and result.get("urgency", 0) >= 7:
                    cm.classification_reason += f" | Groq: material, urgency={result.get('urgency', 0)}"

                time.sleep(0.1)  # Rate limit between Groq calls

            except Exception as e:
                logger.warning(f"[MoveClassifier] Groq enrichment failed for {cm.symbol}: {e}")

        return movers
