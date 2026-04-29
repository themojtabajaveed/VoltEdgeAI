"""Adapter — `EarningsSignal` → `FilingEvent`.

This is the single boundary between the event-intelligence pipeline and
the existing VoltEdge router/strategy contract. The router stays
unchanged; we adapt our richer record into the contract it already
understands.

`effective_urgency = urgency × source_confidence`. This is the soft
modulation described in design doc §2.2: TrueData-only events at 0.7
confidence are visible to the system but cannot reach DAWN's R1 ≥ 8.5
gate without official confirmation.
"""

from __future__ import annotations

from typing import Optional

from src.data_ingestion.exchange_filings import FilingEvent
from src.event_intelligence.models import EarningsSignal


def to_filing_event(sig: EarningsSignal) -> FilingEvent:
    """Build a FilingEvent the existing router can consume."""
    effective_urgency = float(sig.urgency) * float(sig.source_confidence)

    deal_size_inr: Optional[float] = None
    if sig.dividend is not None and sig.dividend.total_payout_inr is not None:
        deal_size_inr = sig.dividend.total_payout_inr

    earnings_surprise_pct: Optional[float] = None
    if sig.earnings is not None:
        earnings_surprise_pct = sig.earnings.surprise_pct

    return FilingEvent(
        symbol=sig.symbol,
        company_name=sig.symbol,   # we don't always carry it; symbol is fine for routing
        headline=sig.summary,
        category=sig.dawn_category or "",
        filed_at=sig.filed_at,
        exchange=sig.exchange,
        urgency=effective_urgency,
        deal_size_inr=deal_size_inr,
        price_at_filing_time=None,
        market_cap_inr=None,
        earnings_surprise_pct=earnings_surprise_pct,
        raw={
            "event_id": sig.event_id,
            "event_type": sig.event_type,
            "event_subtype": sig.event_subtype,
            "direction_hint": sig.direction_hint,
            "source_confidence": sig.source_confidence,
            "source_chain": sig.source_chain,
            "raw_urgency": sig.urgency,
            "classifier_model": sig.classifier_model,
            "classifier_version": sig.classifier_version,
            "parse_status": sig.parse_status,
            "audit_notes": sig.audit_notes,
        },
    )
