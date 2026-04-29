"""Tier-2 dataclasses for the event-intelligence pipeline.

These hold the rich payload (parsed metrics + LLM classification + audit
trail). The router still consumes the existing `FilingEvent` from
`src.data_ingestion.exchange_filings`; `adapter.to_filing_event()` builds
that view from `EarningsSignal`. Tier 1 (FilingEvent) is unchanged.

All times stored ISO 8601 with timezone — IST when the value is sourced
from an exchange convention (filed_at), otherwise the offset is included
explicitly. Internal computations should normalize to UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Sub-payloads ────────────────────────────────────────────────────────────

@dataclass
class EarningsPayload:
    """Quarterly or annual financial-result metrics.

    All amounts in INR (absolute, not crores). Use `None` for any field
    not present in the source — never guess. `surprise_pct` is signed:
    positive = beat, negative = miss; computed against consensus_pat if
    available, else against prior-year same-period PAT.
    """

    revenue: Optional[float] = None
    revenue_yoy_pct: Optional[float] = None
    revenue_qoq_pct: Optional[float] = None
    pat: Optional[float] = None
    pat_yoy_pct: Optional[float] = None
    pat_qoq_pct: Optional[float] = None
    ebitda: Optional[float] = None
    ebitda_margin_pct: Optional[float] = None
    eps: Optional[float] = None
    eps_yoy_pct: Optional[float] = None
    consensus_revenue: Optional[float] = None
    consensus_pat: Optional[float] = None
    consensus_eps: Optional[float] = None
    surprise_pct: Optional[float] = None


@dataclass
class GuidancePayload:
    """Forward-looking management guidance change.

    `tone` is one of: RAISED, REAFFIRMED, CUT, WITHDRAWN, SILENT.
    `delta_pct` is signed magnitude when the filing quotes both old and
    new figures.
    """

    tone: str
    metric: Optional[str] = None
    period: Optional[str] = None
    delta_pct: Optional[float] = None


@dataclass
class DividendPayload:
    """Dividend declaration.

    `action` is one of: INTRODUCED, RAISED, MAINTAINED, CUT, OMITTED, SPECIAL.
    """

    action: str
    amount_per_share_inr: Optional[float] = None
    yield_pct: Optional[float] = None
    total_payout_inr: Optional[float] = None
    record_date: Optional[str] = None
    payment_date: Optional[str] = None


# ── Top-level signal ────────────────────────────────────────────────────────

@dataclass
class EarningsSignal:
    """Final, classified, emitted record from the pipeline.

    Persisted to `data/earnings_signals_YYYY-MM-DD.json`. Read by the
    shadow router and (post-soak) by the live router-integration path.
    """

    # Identity
    event_id: str
    symbol: str
    exchange: str
    event_type: str            # FINANCIAL_RESULTS|GUIDANCE_CHANGE|DIVIDEND_ACTION|BOARD_MEETING
    event_subtype: str         # see classifier.SUBTYPE_THRESHOLDS for full list
    filed_at: str              # ISO 8601 with offset (IST)
    detected_at: str           # ISO 8601 with offset
    classified_at: str         # ISO 8601 with offset

    # Routing
    direction_hint: str        # BUY|SHORT|NEUTRAL
    urgency: float             # 0-10
    market_moving_confidence: float
    dawn_category: Optional[str]
    material: bool
    summary: str

    # Source confidence
    source_confidence: float   # 0.5 conflict, 0.7 vendor only, 1.0 official confirmed
    source_chain: List[str] = field(default_factory=list)
    truedata_event_id: Optional[str] = None
    nse_filing_id: Optional[str] = None
    bse_filing_id: Optional[str] = None
    xbrl_url: Optional[str] = None
    pdf_url: Optional[str] = None

    # Payloads
    earnings: Optional[EarningsPayload] = None
    guidance: Optional[GuidancePayload] = None
    dividend: Optional[DividendPayload] = None

    # Audit
    classifier_model: str = ""
    classifier_version: str = "v1"
    parse_status: str = "SUCCESS"   # SUCCESS|PARTIAL|XBRL_FALLBACK_PDF|HEURISTIC|FAILED
    audit_notes: str = ""
    raw_truedata: Dict[str, Any] = field(default_factory=dict, repr=False)
    raw_filing: Dict[str, Any] = field(default_factory=dict, repr=False)


# ── Intermediate records (between pipeline stages) ──────────────────────────

@dataclass
class RawTruedataEvent:
    """Normalized ingest record before classification.

    Despite the historical class name, this is now source-agnostic. The
    `source` field tags origin:
      - "TRUEDATA"     — vendor WebSocket; needs NSE/BSE verification.
      - "NSE_OFFICIAL" — direct NSE poll; born at confidence 1.0.
      - "BSE_OFFICIAL" — direct BSE poll; born at confidence 1.0.

    The verifier's short-circuit branch reads `source` to decide whether
    to run the polling state machine or emit CONFIRMED@1.0 immediately.
    Class name retained for backward-compat with v1; rename to
    RawIngestEvent deferred to v2.
    """

    event_id: str
    symbol: str
    exchange: str              # exchange tag from the source
    headline: str
    category: str              # source-tagged category
    received_at: str           # ISO 8601 IST — when our pipeline saw it
    vendor_filed_at: Optional[str]   # ISO 8601 IST — source's view of exchange publication
    truedata_event_id: Optional[str]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    # ── Plan B additions (additive; defaults preserve v1 behaviour) ──
    source: str = "TRUEDATA"
    nse_filing_id: Optional[str] = None
    bse_filing_id: Optional[str] = None


@dataclass
class VerifiedEvent:
    """RawTruedataEvent + NSE/BSE confirmation status (Phase-B output)."""

    raw: RawTruedataEvent
    state: str                 # INITIAL|AWAITING_CONFIRM|LIVE_UNCONFIRMED|CONFIRMED|CONFLICT|EXPIRED_NO_CONFIRM
    source_confidence: float   # 0.5|0.7|1.0
    nse_filing_id: Optional[str] = None
    bse_filing_id: Optional[str] = None
    official_subject: Optional[str] = None
    official_filed_at: Optional[str] = None
    official_category: Optional[str] = None
    xbrl_url: Optional[str] = None
    pdf_url: Optional[str] = None
    conflict_notes: Optional[str] = None
