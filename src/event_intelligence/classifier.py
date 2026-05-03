"""Classifier orchestrator.

Composes parser output + Gemini extraction + deterministic bucketing
into a final `EarningsSignal`. Gemini's role is structured extraction
of numeric values and qualitative tone; the *bucketing* into
event_subtype (BLOWOUT_BEAT, INLINE, MISS, etc.) is done in pure Python
so it's auditable and not at the mercy of LLM consistency.

Flow:
    parsed_doc + filing_meta
        → build prompt (with prior-quarters context)
        → Gemini extract → ExtractedFields
        → deterministic bucketing (subtype, dawn_category, surprise_pct)
        → assemble EarningsSignal

If Gemini is unavailable or returns garbage, the classifier falls back
to a heuristic path that produces a low-urgency, material=false-leaning
record so the pipeline never halts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from src.event_intelligence.config import EventIntelConfig

from src.event_intelligence.dedupe import compute_event_id
from src.event_intelligence.models import (
    DividendPayload,
    EarningsPayload,
    EarningsSignal,
    GuidancePayload,
    VerifiedEvent,
)
from src.event_intelligence.parsers.base import ParsedDocument, ParseStatus
from src.llm.gemini_client import call_json as gemini_call_json
from src.llm.gemini_client import get_client as gemini_get_client

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Gemini daily budget tracker (module-level, thread-safe) ──────────────
_budget_lock = threading.Lock()
_budget_date: Optional[str] = None   # IST date "YYYY-MM-DD"
_budget_calls: int = 0

# NSE/BSE category substrings that warrant Gemini spend.
# Everything else (AGM notices, appointments, generic updates) is skipped.
_PRIORITY_CAT_KEYWORDS: tuple = (
    "financial result",
    "result",
    "board meeting",
    "quarterly",
    "half year",
    "annual result",
    "dividend",
    "earnings",
)
_PRIORITY_HEADLINE_KEYWORDS: tuple = (
    "result", "profit", "revenue", "quarterly", "half year",
    "annual", "dividend", "eps", "earnings",
    "outcome of board", "outcome",  # board-meeting result announcements
)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify_earnings_v1.txt"

_CLASSIFIER_VERSION = "v1"


# ── DAWN_CATEGORIES mapping ────────────────────────────────────────────────
# Mirrors the table in the design doc §2.3. The right-hand value is what
# gets stamped onto FilingEvent.category. None means "no DAWN match" —
# router R2 will route to HYDRA naturally.

_SUBTYPE_TO_DAWN: Dict[str, Optional[str]] = {
    "QUARTERLY_BLOWOUT_BEAT": "RESULTS_BLOWOUT",
    "QUARTERLY_STRONG_BEAT": "EARNINGS_SURPRISE",
    "QUARTERLY_BEAT": "EARNINGS_SURPRISE",
    "QUARTERLY_INLINE": None,
    "QUARTERLY_MISS": "EARNINGS_SURPRISE",
    "QUARTERLY_STRONG_MISS": "EARNINGS_SURPRISE",
    "ANNUAL_BLOWOUT": "RESULTS_BLOWOUT",
    "ANNUAL_DISAPPOINTMENT": "EARNINGS_SURPRISE",
    "GUIDANCE_RAISED_LARGE": "EARNINGS_SURPRISE",
    "GUIDANCE_RAISED": "EARNINGS_SURPRISE",
    "GUIDANCE_REAFFIRMED": None,
    "GUIDANCE_CUT": "EARNINGS_SURPRISE",
    "GUIDANCE_WITHDRAWN": "EARNINGS_SURPRISE",
    "DIVIDEND_INTRODUCED": None,
    "DIVIDEND_RAISED_LARGE": "EARNINGS_SURPRISE",
    "DIVIDEND_RAISED": None,
    "DIVIDEND_MAINTAINED": None,
    "DIVIDEND_CUT": "EARNINGS_SURPRISE",
    "DIVIDEND_OMITTED": "EARNINGS_SURPRISE",
    "DIVIDEND_SPECIAL_LARGE": "EARNINGS_SURPRISE",
    "BOARD_MEETING_RESULTS": None,
    "BOARD_MEETING_DIVIDEND": None,
    "BOARD_MEETING_OTHER": None,
    "OTHER": None,
}


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def _load_prompt_template() -> str:
    try:
        return _PROMPT_PATH.read_text()
    except OSError as e:
        logger.error("[Classifier] could not load prompt template: %s", e)
        return ""


def _build_prompt(
    verified: VerifiedEvent,
    parsed: ParsedDocument,
    prior_quarters: List[Dict[str, Any]],
) -> str:
    template = _load_prompt_template()
    if not template:
        return ""
    inputs = {
        "symbol": verified.raw.symbol,
        "exchange": verified.raw.exchange,
        "filing_subject": verified.official_subject or verified.raw.headline,
        "filing_body": parsed.raw_text_excerpt or "",
        "parsed_metrics": parsed.fields,
        "filed_at": verified.official_filed_at or verified.raw.vendor_filed_at,
        "prior_quarters": prior_quarters,
    }
    return template.replace("{INPUTS_JSON}", json.dumps(inputs, indent=2, default=str))


# ── Bucketing helpers ──────────────────────────────────────────────────────

def _compute_surprise_pct(
    extracted: Dict[str, Any],
    prior_quarters: Optional[List[Dict[str, Any]]] = None,
    verified: Optional["VerifiedEvent"] = None,
) -> Optional[float]:
    """Signed surprise: positive = beat.

    Priority:
      1. actual_pat vs analyst consensus_pat (if Gemini extracted consensus)
      2. pat_yoy_pct (if Gemini extracted explicit YoY from filing text)
      3. actual_pat vs self-consensus expected_pat (trend + seasonality model)
      4. actual_pat vs same-quarter-prior-year PAT from DB (simple YoY fallback)
    """
    pat = _safe_float(extracted.get("pat"))
    consensus = _safe_float(extracted.get("consensus_pat"))
    pat_yoy = _safe_float(extracted.get("pat_yoy_pct"))

    if pat is not None and consensus is not None and consensus > 0:
        return ((pat - consensus) / consensus) * 100.0
    if pat_yoy is not None:
        return pat_yoy

    # Path 3: self-consensus model (uses trend + seasonality + rev momentum)
    if pat is not None and prior_quarters and len(prior_quarters) >= 4:
        sc_surprise = _surprise_from_self_consensus(pat, prior_quarters, verified)
        if sc_surprise is not None:
            return sc_surprise

    # Path 4: simple YoY from DB (fallback when self-consensus lacks data)
    if pat is not None and prior_quarters:
        return _surprise_from_db(pat, prior_quarters, verified)
    return None


def _surprise_from_self_consensus(
    current_pat: float,
    prior_quarters: List[Dict[str, Any]],
    verified: Optional["VerifiedEvent"],
) -> Optional[float]:
    """Compute surprise vs self-consensus expected PAT.

    Uses the multi-component model (trend + seasonality + revenue momentum)
    for a more sophisticated baseline than raw YoY.
    """
    try:
        from src.event_intelligence.consensus import compute_self_consensus, compute_surprise_pct as _sc_surprise
    except ImportError:
        return None

    # Determine target fiscal quarter from filing date
    filed_at = None
    if verified:
        filed_at = verified.official_filed_at or verified.raw.vendor_filed_at
    if not filed_at:
        return None

    try:
        filed_dt = datetime.fromisoformat(filed_at)
    except (ValueError, TypeError):
        return None

    month = filed_dt.month
    if month in (4, 5, 6):
        target_fq = 4
    elif month in (7, 8, 9):
        target_fq = 1
    elif month in (10, 11, 12):
        target_fq = 2
    else:
        target_fq = 3

    sc = compute_self_consensus(prior_quarters, target_fiscal_quarter=target_fq)
    if sc.expected_pat_cr is None or sc.confidence == "LOW":
        return None

    # Unit-normalize current_pat to crores (same as self-consensus output)
    # Use DB anchor for normalization
    db_pats = [q.get("pat_cr") for q in prior_quarters if q.get("pat_cr")]
    if not db_pats:
        return None
    anchor = db_pats[-1]

    # Try plausible units and pick the most reasonable
    best_pat_cr = None
    best_dist = float("inf")
    for pat_cr in (current_pat, current_pat / 100.0, current_pat / 1e7):
        ratio = pat_cr / anchor if anchor else 0
        if 0.05 <= abs(ratio) <= 20:
            dist = abs(ratio - 1.0)
            if dist < best_dist:
                best_dist = dist
                best_pat_cr = pat_cr

    if best_pat_cr is None:
        return None

    return _sc_surprise(best_pat_cr, sc)


def _surprise_from_db(
    current_pat: float,
    prior_quarters: List[Dict[str, Any]],
    verified: Optional["VerifiedEvent"],
) -> Optional[float]:
    """Compute YoY surprise against same-quarter-prior-year PAT from DB.

    Handles unit normalization: DB stores crores, extracted PAT may be
    in absolute INR (from Gemini) or lakhs (from PDF parser).
    """
    if not prior_quarters:
        return None

    # Determine which fiscal quarter this filing belongs to
    filed_at = None
    if verified:
        filed_at = verified.official_filed_at or verified.raw.vendor_filed_at
    if not filed_at:
        return None

    try:
        filed_dt = datetime.fromisoformat(filed_at)
    except (ValueError, TypeError):
        return None

    # Indian results season: Q4 results filed Apr-May, Q1 Jul-Aug, Q2 Oct-Nov, Q3 Jan-Feb
    # Map filing month to the quarter whose results are being reported
    month = filed_dt.month
    if month in (4, 5, 6):
        target_fq = 4  # Q4 results
    elif month in (7, 8, 9):
        target_fq = 1  # Q1 results
    elif month in (10, 11, 12):
        target_fq = 2  # Q2 results
    else:  # Jan, Feb, Mar
        target_fq = 3  # Q3 results

    # Find same fiscal_quarter from prior year — exclude entries within 200 days
    # of filing (which would be the current quarter itself if already in DB).
    from datetime import date as _date
    same_q_prior = []
    for q in prior_quarters:
        if q.get("fiscal_quarter") != target_fq:
            continue
        qe = q.get("quarter_end")
        if isinstance(qe, str):
            try:
                qe = _date.fromisoformat(qe)
            except ValueError:
                continue
        if qe and (filed_dt.date() - qe).days >= 200:
            same_q_prior.append(q)
    if not same_q_prior:
        return None

    # Most recent prior-year same-quarter entry
    prior_pat_cr = same_q_prior[-1].get("pat_cr")
    if not prior_pat_cr or prior_pat_cr == 0:
        return None

    # Unit normalization: use DB prior PAT (crores) as anchor.
    # Try all plausible conversions; pick the one giving the most reasonable
    # YoY ratio (closest to 1.0 within the plausible band of 0.05–20x).
    candidates = [
        ("crores", current_pat),
        ("lakhs", current_pat / 100.0),
        ("abs_inr", current_pat / 1e7),
    ]
    best_unit = None
    best_ratio_dist = float("inf")
    best_pat_cr = None
    for unit, pat_cr in candidates:
        ratio = pat_cr / prior_pat_cr
        if 0.05 <= abs(ratio) <= 20:
            # Distance from 1.0 (closer = more plausible as a YoY comparison)
            dist = abs(ratio - 1.0)
            if dist < best_ratio_dist:
                best_ratio_dist = dist
                best_unit = unit
                best_pat_cr = pat_cr
    if best_pat_cr is None:
        return None
    current_pat_cr = best_pat_cr

    return ((current_pat_cr - prior_pat_cr) / abs(prior_pat_cr)) * 100.0


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _detect_annual(verified: VerifiedEvent, extracted: Dict[str, Any]) -> bool:
    """True if the filing is a full-year annual result, not quarterly.

    Indian convention: "Q1/Q2/Q3/Q4" markers always mean quarterly even
    when accompanied by an "FY26" tag. Only the explicit words "Annual"
    or "Year Ended" (or "Audited Financial Results for the year") signal
    a full-year filing.
    """
    text = (
        f"{verified.official_subject or ''} "
        f"{verified.raw.headline or ''} "
        f"{extracted.get('guidance_period') or ''}"
    ).upper()
    if any(q in text for q in (" Q1", " Q2", " Q3", " Q4", "QUARTERLY", "QUARTER")):
        return False
    if "ANNUAL" in text or "YEAR ENDED" in text or "FULL YEAR" in text:
        return True
    return False


def _bucket_results_subtype(
    is_annual: bool, surprise_pct: Optional[float]
) -> str:
    """Map signed surprise to subtype. See design doc §2.3."""
    if surprise_pct is None:
        return "ANNUAL_BLOWOUT" if is_annual else "QUARTERLY_INLINE"
    if surprise_pct >= 25.0:
        return "ANNUAL_BLOWOUT" if is_annual else "QUARTERLY_BLOWOUT_BEAT"
    if surprise_pct >= 10.0:
        return "ANNUAL_BLOWOUT" if is_annual else "QUARTERLY_STRONG_BEAT"
    if surprise_pct >= 1.0:
        return "QUARTERLY_BEAT"
    if surprise_pct > -1.0:
        return "QUARTERLY_INLINE"
    if surprise_pct > -10.0:
        return "QUARTERLY_MISS"
    return "ANNUAL_DISAPPOINTMENT" if is_annual else "QUARTERLY_STRONG_MISS"


def _bucket_guidance_subtype(extracted: Dict[str, Any]) -> str:
    tone = (extracted.get("guidance_tone") or "SILENT").upper()
    delta = _safe_float(extracted.get("guidance_delta_pct")) or 0.0
    if tone == "RAISED":
        return "GUIDANCE_RAISED_LARGE" if abs(delta) >= 15.0 else "GUIDANCE_RAISED"
    if tone == "CUT":
        return "GUIDANCE_CUT"
    if tone == "WITHDRAWN":
        return "GUIDANCE_WITHDRAWN"
    if tone == "REAFFIRMED":
        return "GUIDANCE_REAFFIRMED"
    return "GUIDANCE_REAFFIRMED"


def _bucket_dividend_subtype(extracted: Dict[str, Any]) -> str:
    action = (extracted.get("dividend_action") or "MAINTAINED").upper()
    yld = _safe_float(extracted.get("dividend_yield_pct")) or 0.0
    if action == "INTRODUCED":
        return "DIVIDEND_INTRODUCED"
    if action == "RAISED":
        return "DIVIDEND_RAISED_LARGE" if yld >= 2.0 else "DIVIDEND_RAISED"
    if action == "SPECIAL":
        return "DIVIDEND_SPECIAL_LARGE" if yld >= 2.0 else "DIVIDEND_RAISED"
    if action == "CUT":
        return "DIVIDEND_CUT"
    if action == "OMITTED":
        return "DIVIDEND_OMITTED"
    return "DIVIDEND_MAINTAINED"


def _direction_from_subtype(subtype: str) -> str:
    if subtype in {
        "QUARTERLY_BLOWOUT_BEAT", "QUARTERLY_STRONG_BEAT", "QUARTERLY_BEAT",
        "ANNUAL_BLOWOUT", "GUIDANCE_RAISED_LARGE", "GUIDANCE_RAISED",
        "DIVIDEND_INTRODUCED", "DIVIDEND_RAISED_LARGE", "DIVIDEND_RAISED",
        "DIVIDEND_SPECIAL_LARGE",
    }:
        return "BUY"
    if subtype in {
        "QUARTERLY_MISS", "QUARTERLY_STRONG_MISS", "ANNUAL_DISAPPOINTMENT",
        "GUIDANCE_CUT", "GUIDANCE_WITHDRAWN", "DIVIDEND_CUT", "DIVIDEND_OMITTED",
    }:
        return "SHORT"
    return "NEUTRAL"


def _urgency_from_subtype(subtype: str, parse_status: ParseStatus) -> float:
    base = {
        "QUARTERLY_BLOWOUT_BEAT": 9.5,
        "ANNUAL_BLOWOUT": 9.5,
        "QUARTERLY_STRONG_MISS": 9.0,
        "ANNUAL_DISAPPOINTMENT": 9.0,
        "GUIDANCE_WITHDRAWN": 9.0,
        "DIVIDEND_OMITTED": 9.0,
        "QUARTERLY_STRONG_BEAT": 8.5,
        "GUIDANCE_RAISED_LARGE": 8.5,
        "DIVIDEND_SPECIAL_LARGE": 8.0,
        "DIVIDEND_RAISED_LARGE": 7.5,
        "QUARTERLY_BEAT": 7.0,
        "QUARTERLY_MISS": 7.0,
        "GUIDANCE_RAISED": 7.0,
        "GUIDANCE_CUT": 7.0,
        "DIVIDEND_CUT": 6.5,
        "DIVIDEND_INTRODUCED": 6.0,
        "DIVIDEND_RAISED": 5.5,
        "QUARTERLY_INLINE": 4.0,
        "GUIDANCE_REAFFIRMED": 3.0,
        "DIVIDEND_MAINTAINED": 2.0,
        "BOARD_MEETING_RESULTS": 4.5,
        "BOARD_MEETING_DIVIDEND": 3.5,
        "BOARD_MEETING_OTHER": 1.5,
        "OTHER": 2.0,
    }.get(subtype, 4.0)

    # Heuristic-only filings can never reach DAWN's R1 ≥ 8.5 gate.
    if parse_status == ParseStatus.HEURISTIC:
        return min(base, 5.0)
    return base


# ── Public API ──────────────────────────────────────────────────────────────

def classify(
    verified: VerifiedEvent,
    parsed: ParsedDocument,
    prior_quarters: Optional[List[Dict[str, Any]]] = None,
    cfg: Optional["EventIntelConfig"] = None,
) -> EarningsSignal:
    """Run the full classification pipeline and return an EarningsSignal."""
    prior_quarters = prior_quarters or []
    daily_budget = cfg.gemini_daily_budget if cfg is not None else 1000

    # 1. Call Gemini for structured extraction (best effort).
    gemini_response = _try_gemini(verified, parsed, prior_quarters, daily_budget)

    # 2. Determine event_type. Prefer Gemini, fall back to heuristic.
    # Final fallback: if PDF/PARTIAL parse extracted real financial numbers but
    # no event_type was determined (Gemini skipped, heuristic had no subject
    # match), infer FINANCIAL_RESULTS from the evidence rather than OTHER.
    event_type = (
        (gemini_response or {}).get("event_type")
        or parsed.fields.get("event_type_guess")
        or (
            "FINANCIAL_RESULTS"
            if parsed.status in (
                ParseStatus.PDF_TABLE, ParseStatus.PDF_REGEX,
                ParseStatus.SUCCESS, ParseStatus.PARTIAL,  # legacy compat
            )
            and (parsed.fields.get("pat") is not None or parsed.fields.get("revenue") is not None)
            else "OTHER"
        )
    )

    # 3. Extract metrics. Cross-validate PAT before filling from parser.
    extracted = (gemini_response or {}).get("extracted") or {}
    xval_note = _cross_validate_pat(
        parser_pat=parsed.fields.get("pat"),
        gemini_pat=extracted.get("pat"),
        symbol=verified.raw.symbol,
    )
    for parser_field in ("revenue", "pat", "ebitda", "eps"):
        if extracted.get(parser_field) is None and parsed.fields.get(parser_field) is not None:
            extracted[parser_field] = parsed.fields.get(parser_field)

    # 4. Bucket into subtype.
    is_annual = _detect_annual(verified, extracted)
    surprise_pct = _compute_surprise_pct(extracted, prior_quarters, verified)

    if event_type == "FINANCIAL_RESULTS":
        subtype = _bucket_results_subtype(is_annual, surprise_pct)
    elif event_type == "GUIDANCE_CHANGE":
        subtype = _bucket_guidance_subtype(extracted)
    elif event_type == "DIVIDEND_ACTION":
        subtype = _bucket_dividend_subtype(extracted)
    elif event_type == "BOARD_MEETING":
        subtype = _board_meeting_subtype(verified.raw.headline)
    else:
        subtype = "OTHER"

    direction = _direction_from_subtype(subtype)
    urgency = _urgency_from_subtype(subtype, parsed.status)
    dawn_category = _SUBTYPE_TO_DAWN.get(subtype)

    # 5. Materiality + summary.
    material = bool((gemini_response or {}).get("material", subtype not in ("DIVIDEND_MAINTAINED", "BOARD_MEETING_OTHER", "OTHER")))
    summary = (gemini_response or {}).get("summary") or _default_summary(verified, subtype)
    _base_audit = (gemini_response or {}).get("audit_notes") or f"parse={parsed.status.value}; subtype={subtype}"
    audit_notes = _base_audit + xval_note
    market_moving = float((gemini_response or {}).get("market_moving_confidence") or 5.0)

    # 6. Build payloads.
    earnings_payload = _build_earnings_payload(extracted, surprise_pct) if event_type == "FINANCIAL_RESULTS" else None
    guidance_payload = _build_guidance_payload(extracted) if event_type == "GUIDANCE_CHANGE" else None
    dividend_payload = _build_dividend_payload(extracted) if event_type == "DIVIDEND_ACTION" else None

    # 7. event_id (deterministic dedupe across sources).
    filed_at = verified.official_filed_at or verified.raw.vendor_filed_at or verified.raw.received_at
    source_id = verified.nse_filing_id or verified.bse_filing_id or verified.raw.truedata_event_id
    event_id = compute_event_id(verified.raw.symbol, event_type, filed_at, source_id)

    # 8. Assemble.
    classifier_model = gemini_get_client()._model if gemini_response else "heuristic"

    return EarningsSignal(
        event_id=event_id,
        symbol=verified.raw.symbol,
        exchange=verified.raw.exchange,
        event_type=event_type,
        event_subtype=subtype,
        filed_at=filed_at,
        detected_at=verified.raw.received_at,
        classified_at=_now_ist_iso(),
        direction_hint=direction,
        urgency=urgency,
        market_moving_confidence=market_moving,
        dawn_category=dawn_category,
        material=material,
        summary=summary[:80],
        source_confidence=verified.source_confidence,
        source_chain=_build_source_chain(verified),
        truedata_event_id=verified.raw.truedata_event_id,
        nse_filing_id=verified.nse_filing_id,
        bse_filing_id=verified.bse_filing_id,
        xbrl_url=verified.xbrl_url,
        pdf_url=verified.pdf_url,
        earnings=earnings_payload,
        guidance=guidance_payload,
        dividend=dividend_payload,
        classifier_model=classifier_model,
        classifier_version=_CLASSIFIER_VERSION,
        parse_status=parsed.status.value,
        audit_notes=audit_notes[:200],
        raw_truedata=verified.raw.raw,
        raw_filing={
            "official_subject": verified.official_subject,
            "official_category": verified.official_category,
            "state": verified.state,
            "conflict_notes": verified.conflict_notes,
        },
    )


def _is_priority_for_gemini(verified: VerifiedEvent) -> bool:
    """Return True if this filing warrants spending Gemini budget."""
    cat = (verified.official_category or verified.raw.category or "").lower()
    if any(kw in cat for kw in _PRIORITY_CAT_KEYWORDS):
        return True
    headline = (verified.raw.headline or "").lower()
    return any(kw in headline for kw in _PRIORITY_HEADLINE_KEYWORDS)


def _check_and_increment_budget(daily_budget: int) -> bool:
    """Thread-safe daily budget gate. Returns True if a call is allowed."""
    global _budget_date, _budget_calls
    today = datetime.now(IST).strftime("%Y-%m-%d")
    with _budget_lock:
        if _budget_date != today:
            _budget_date = today
            _budget_calls = 0
        if _budget_calls >= daily_budget:
            return False
        _budget_calls += 1
        return True


def _cross_validate_pat(
    parser_pat: Optional[float],
    gemini_pat: Any,
    symbol: str,
) -> str:
    """Flag a >30% divergence between parser and Gemini PAT. Returns audit suffix."""
    if parser_pat is None or gemini_pat is None:
        return ""
    try:
        g_val = float(gemini_pat)
    except (TypeError, ValueError):
        return ""
    if parser_pat == 0.0:
        return ""
    divergence = abs(g_val - parser_pat) / abs(parser_pat)
    if divergence > 0.30:
        logger.warning(
            "[Classifier] %s PAT divergence %.0f%%: parser=%.1f gemini=%.1f",
            symbol, divergence * 100, parser_pat, g_val,
        )
        return f"; PAT_DIVERGENCE={divergence * 100:.0f}%"
    return ""


def _try_gemini(
    verified: VerifiedEvent,
    parsed: ParsedDocument,
    prior_quarters: List[Dict[str, Any]],
    daily_budget: int = 1000,
) -> Optional[Dict[str, Any]]:
    # Gate 1: nothing to extract from
    if parsed.status == ParseStatus.HEURISTIC and not parsed.raw_text_excerpt:
        return None

    # Gate 2: reserve budget for priority categories only
    if not _is_priority_for_gemini(verified):
        logger.debug("[Classifier] skip Gemini non-priority: %s", verified.raw.symbol)
        return None

    # Gate 3: daily budget cap
    if not _check_and_increment_budget(daily_budget):
        logger.warning(
            "[Classifier] Gemini daily budget (%d) exhausted — heuristic fallback for %s",
            daily_budget, verified.raw.symbol,
        )
        return None

    prompt = _build_prompt(verified, parsed, prior_quarters)
    if not prompt:
        return None
    return gemini_call_json(prompt)


def _board_meeting_subtype(headline: str) -> str:
    h = (headline or "").upper()
    if "RESULT" in h:
        return "BOARD_MEETING_RESULTS"
    if "DIVIDEND" in h:
        return "BOARD_MEETING_DIVIDEND"
    return "BOARD_MEETING_OTHER"


def _default_summary(verified: VerifiedEvent, subtype: str) -> str:
    return f"{verified.raw.symbol} {subtype.replace('_', ' ').lower()}"


def _build_source_chain(verified: VerifiedEvent) -> List[str]:
    chain = []
    if verified.raw.truedata_event_id:
        chain.append("truedata")
    if verified.nse_filing_id:
        chain.append("nse_official")
    if verified.bse_filing_id:
        chain.append("bse_official")
    return chain or ["unknown"]


def _build_earnings_payload(extracted: Dict[str, Any], surprise_pct: Optional[float]) -> EarningsPayload:
    return EarningsPayload(
        revenue=_safe_float(extracted.get("revenue")),
        revenue_yoy_pct=_safe_float(extracted.get("revenue_yoy_pct")),
        revenue_qoq_pct=_safe_float(extracted.get("revenue_qoq_pct")),
        pat=_safe_float(extracted.get("pat")),
        pat_yoy_pct=_safe_float(extracted.get("pat_yoy_pct")),
        pat_qoq_pct=_safe_float(extracted.get("pat_qoq_pct")),
        ebitda=_safe_float(extracted.get("ebitda")),
        ebitda_margin_pct=_safe_float(extracted.get("ebitda_margin_pct")),
        eps=_safe_float(extracted.get("eps")),
        eps_yoy_pct=_safe_float(extracted.get("eps_yoy_pct")),
        consensus_revenue=_safe_float(extracted.get("consensus_revenue")),
        consensus_pat=_safe_float(extracted.get("consensus_pat")),
        consensus_eps=_safe_float(extracted.get("consensus_eps")),
        surprise_pct=surprise_pct,
    )


def _build_guidance_payload(extracted: Dict[str, Any]) -> GuidancePayload:
    return GuidancePayload(
        tone=(extracted.get("guidance_tone") or "SILENT").upper(),
        metric=extracted.get("guidance_metric"),
        period=extracted.get("guidance_period"),
        delta_pct=_safe_float(extracted.get("guidance_delta_pct")),
    )


def _build_dividend_payload(extracted: Dict[str, Any]) -> DividendPayload:
    return DividendPayload(
        action=(extracted.get("dividend_action") or "MAINTAINED").upper(),
        amount_per_share_inr=_safe_float(extracted.get("dividend_amount_per_share_inr")),
        yield_pct=_safe_float(extracted.get("dividend_yield_pct")),
        record_date=extracted.get("record_date"),
    )
