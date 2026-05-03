"""Universal event-classification contract for the event-intelligence pipeline.

Every event class (FINANCIAL_RESULTS, ORDER_WIN, M&A, MANAGEMENT_CHANGE, etc.)
produces an `EventClassification` and implements `EventClassParser`. The router,
conviction engine, HYDRA bridge, and evaluation framework consume only this
interface — they never inspect class-specific parser internals.

Build order:
  - This module (interface only, no business logic)
  - src/event_intelligence/classifier.py (FINANCIAL_RESULTS — first class)
  - Future: parsers/order_win.py, parsers/m_and_a.py, ... (each implements ABC)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ── Canonical event classes ───────────────────────────────────────────────────
# Extend this list as new classes are implemented. Keep values stable — they
# are written to disk in signal JSON files and compared in dedupe.

FINANCIAL_RESULTS = "FINANCIAL_RESULTS"
ORDER_WIN = "ORDER_WIN"
M_AND_A = "M_AND_A"
MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
DIVIDEND_BUYBACK = "DIVIDEND_BUYBACK"
REGULATORY = "REGULATORY"
CREDIT_RATING = "CREDIT_RATING"
OTHER = "OTHER"


# ── Novelty labels ────────────────────────────────────────────────────────────

class Novelty:
    PRISTINE = "PRISTINE"        # first-ever disclosure of this fact
    EXPECTED = "EXPECTED"        # market had advance warning (board intimation etc.)
    CONTINUATION = "CONTINUATION"  # follow-up to a prior event in the same chain
    STALE = "STALE"              # event is old (> _MAX_SIGNAL_AGE hours)


# ── Parse quality labels ──────────────────────────────────────────────────────

class ParseQuality:
    XBRL = "XBRL"
    PDF_TABLE = "PDF_TABLE"
    PDF_REGEX = "PDF_REGEX"
    HEURISTIC = "HEURISTIC"
    LLM_ONLY = "LLM_ONLY"


@dataclass
class EventClassification:
    """Universal output produced by any EventClassParser implementation.

    Downstream consumers (HYDRA bridge, DAWN router, postmortem, evaluation)
    consume only this dataclass — they have no dependency on parser internals.
    """
    event_id: str
    symbol: str
    exchange: str

    event_class: str        # one of the constants above (FINANCIAL_RESULTS etc.)
    event_subtype: str      # class-specific: QUARTERLY_BLOWOUT_BEAT, LARGE_ORDER_WIN, etc.
    direction_hint: str     # 'BUY' | 'SHORT' | 'NEUTRAL'
    urgency: float          # 0.0 – 10.0
    confidence: float       # 0.0 – 1.0 (parser + extraction quality)
    material: bool          # expected to move stock > 1%?

    # Novelty / priced-in assessment
    novelty: str            # Novelty.*
    priced_in_discount: float = 0.0   # 0.0 = not priced in; 1.0 = fully priced in

    # Effective urgency after priced-in discount
    @property
    def effective_urgency(self) -> float:
        return self.urgency * (1.0 - self.priced_in_discount)

    # Class-specific evidence payload (structured for each event class)
    evidence: Dict[str, Any] = field(default_factory=dict)

    # Parse provenance
    parse_quality: str = ParseQuality.HEURISTIC   # ParseQuality.*
    source_confidence: float = 1.0                # 0.5–1.0

    # Audit
    filed_at: Optional[str] = None
    classified_at: Optional[str] = None
    audit_notes: List[str] = field(default_factory=list)
    raw_filing: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "event_class": self.event_class,
            "event_subtype": self.event_subtype,
            "direction_hint": self.direction_hint,
            "urgency": self.urgency,
            "effective_urgency": self.effective_urgency,
            "confidence": self.confidence,
            "material": self.material,
            "novelty": self.novelty,
            "priced_in_discount": self.priced_in_discount,
            "evidence": self.evidence,
            "parse_quality": self.parse_quality,
            "source_confidence": self.source_confidence,
            "filed_at": self.filed_at,
            "classified_at": self.classified_at,
            "audit_notes": self.audit_notes,
        }


class EventClassParser(ABC):
    """Abstract base class for all event-class deep parsers.

    Implementation pattern (one file per class):
      class OrderWinParser(EventClassParser):
          def can_handle(self, filing) -> bool:
              return "order" in (filing.raw.category or "").lower()

          def parse(self, filing, document) -> EventClassification:
              ...

          def compute_surprise(self, classification, consensus) -> Optional[float]:
              # Order wins: surprise = deal_value / trailing_4Q_revenue
              ...
    """

    @abstractmethod
    def can_handle(self, filing: Any) -> bool:
        """Return True if this parser recognises the filing type.

        Should be fast and cheap — checked against every incoming filing.
        Must not raise.
        """

    @abstractmethod
    def parse(self, filing: Any, document: Any) -> EventClassification:
        """Deep extraction + classification for this event class.

        Args:
            filing: VerifiedEvent (typed Any to avoid circular import)
            document: ParsedDocument from the appropriate parser

        Must not raise — return a low-confidence HEURISTIC classification
        on any error rather than propagating exceptions.
        """

    @abstractmethod
    def compute_surprise(
        self,
        classification: EventClassification,
        consensus: Any,  # SelfConsensus | AnalystConsensus | None
    ) -> Optional[float]:
        """Compute the surprise_pct for this event class.

        Returns:
            Signed float (positive = beat, negative = miss), or None if
            no consensus is available or surprise is not meaningful for
            this event class.
        """


def effective_urgency_after_discount(
    urgency: float,
    priced_in_discount: float,
) -> float:
    """Standalone helper for callers that don't hold an EventClassification."""
    return urgency * max(0.0, 1.0 - priced_in_discount)
