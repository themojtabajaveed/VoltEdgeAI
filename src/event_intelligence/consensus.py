"""Self-consensus model for Bucket B (uncovered but tradable) stocks.

Produces an expected PAT estimate for an upcoming quarter using only the
company's own historical quarterly results — no analyst estimates required.

Five components (v1 defaults, all weights configurable):
  1. Trailing 4Q PAT CAGR         — weight 0.40
  2. Same-quarter YoY seasonality  — weight 0.25
  3. Revenue momentum/acceleration — weight 0.20
  4. Management guidance overlay   — weight 0.10 (if available)
  5. Event-chain adjustment        — weight 0.05 (recent material events)

Usage:
    from src.db.models_quarterly import get_prior_quarters
    from src.event_intelligence.consensus import compute_self_consensus

    history = get_prior_quarters("INFY", n=8)
    sc = compute_self_consensus(history, target_fiscal_quarter=1)
    print(sc.expected_pat_cr, sc.confidence)

All weights and growth caps are v1 defaults. They will be tuned from
postmortem evidence as accuracy data accumulates. Do not treat them as
calibrated — they are starting estimates only.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Default weights (v1) ────────────────────────────────────────────────────
# Sum to 1.0. Remaining weight when guidance absent is redistributed to trend.
_W_TREND = 0.40
_W_SEASONAL = 0.25
_W_REV_MOM = 0.20
_W_GUIDANCE = 0.10
_W_EVENT = 0.05

# Growth rate caps to prevent extrapolation absurdity
_MAX_GROWTH = 0.50     # +50% cap
_MIN_GROWTH = -0.50    # -50% floor

# Minimum quarters for each component to be reliable
_MIN_QUARTERS_TREND = 4
_MIN_QUARTERS_SEASONAL = 2    # need 2 same-quarter observations for YoY


@dataclass
class SelfConsensus:
    """Output of the self-consensus model."""
    expected_pat_cr: Optional[float]        # None if insufficient history
    confidence: str                          # 'HIGH' | 'MEDIUM' | 'LOW'
    method: str                              # e.g. 'SELF_CONSENSUS_v1'
    components: Dict[str, Any] = field(default_factory=dict)
    # Breakdown of each growth rate component before blending
    # Keys: trend_growth, seasonal_growth, rev_momentum, guidance_applied,
    #       event_chain_applied, blended_growth, base_pat_cr


def compute_self_consensus(
    prior_quarters: List[dict],
    target_fiscal_quarter: int,
    guidance: Optional[dict] = None,
    recent_events: Optional[List[dict]] = None,
    *,
    w_trend: float = _W_TREND,
    w_seasonal: float = _W_SEASONAL,
    w_rev_mom: float = _W_REV_MOM,
    w_guidance: float = _W_GUIDANCE,
    w_event: float = _W_EVENT,
) -> SelfConsensus:
    """Produce expected PAT for target_fiscal_quarter from historical rows.

    Args:
        prior_quarters: list of dicts from get_prior_quarters(), sorted
            ascending (oldest first). Each dict must have at minimum
            'pat_cr', 'revenue_cr', 'fiscal_quarter', 'fiscal_year'.
        target_fiscal_quarter: 1-4, the quarter we're forecasting.
        guidance: optional dict with keys 'tone' (str), 'metric' (str),
            'delta_pct' (float | None). From GuidancePayload.
        recent_events: optional list of EarningsSignal-like dicts with
            'event_type', 'event_subtype', 'urgency', 'direction_hint'.
        w_*: weight overrides for testing / config tuning.
    """
    if len(prior_quarters) < _MIN_QUARTERS_TREND:
        return SelfConsensus(
            expected_pat_cr=None,
            confidence="LOW",
            method="SELF_CONSENSUS_v1",
            components={"reason": "insufficient_history", "n": len(prior_quarters)},
        )

    pats = [q.get("pat_cr") for q in prior_quarters]
    revenues = [q.get("revenue_cr") for q in prior_quarters]

    base_pat = _last_valid(pats)
    if base_pat is None:
        return SelfConsensus(
            expected_pat_cr=None,
            confidence="LOW",
            method="SELF_CONSENSUS_v1",
            components={"reason": "no_valid_pat"},
        )

    # ── Component 1: Trailing 4Q PAT CAGR ───────────────────────────────
    trend_growth = _compute_trailing_cagr(pats)

    # ── Component 2: Same-quarter seasonality ────────────────────────────
    seasonal_growth = _compute_seasonality(prior_quarters, target_fiscal_quarter)

    # ── Component 3: Revenue momentum/acceleration ───────────────────────
    rev_momentum = _compute_revenue_momentum(revenues)

    # ── Blend without guidance (normalize weights) ───────────────────────
    if guidance and _guidance_is_usable(guidance):
        blended_no_guidance = (
            w_trend * trend_growth
            + w_seasonal * seasonal_growth
            + w_rev_mom * rev_momentum
        )
        # Remaining weight goes to event chain (and unused guidance slot)
        base_expected = base_pat * (1.0 + blended_no_guidance)
        base_expected = _apply_guidance(base_expected, base_pat, guidance, w_guidance)
        guidance_applied = True
    else:
        # Redistribute guidance weight to trend
        effective_trend_w = w_trend + w_guidance
        blended_no_guidance = (
            effective_trend_w * trend_growth
            + w_seasonal * seasonal_growth
            + w_rev_mom * rev_momentum
        )
        base_expected = base_pat * (1.0 + blended_no_guidance)
        guidance_applied = False

    # ── Component 5: Event-chain adjustment ─────────────────────────────
    event_chain_applied = False
    if recent_events:
        adjusted = _apply_event_chain(base_expected, recent_events, w_event)
        if adjusted != base_expected:
            base_expected = adjusted
            event_chain_applied = True

    # ── Confidence ───────────────────────────────────────────────────────
    valid_pats = [p for p in pats if p is not None]
    variance = _growth_variance(valid_pats)
    n_quarters = len(valid_pats)
    if n_quarters >= 8 and variance < 0.15:
        confidence = "HIGH"
    elif n_quarters >= 4:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    blended_growth = (base_expected / base_pat - 1.0) if base_pat != 0 else 0.0

    return SelfConsensus(
        expected_pat_cr=round(base_expected, 2),
        confidence=confidence,
        method="SELF_CONSENSUS_v1",
        components={
            "base_pat_cr": base_pat,
            "trend_growth": trend_growth,
            "seasonal_growth": seasonal_growth,
            "rev_momentum": rev_momentum,
            "blended_growth": blended_growth,
            "guidance_applied": guidance_applied,
            "event_chain_applied": event_chain_applied,
            "n_quarters": n_quarters,
            "variance": variance,
        },
    )


def compute_surprise_pct(
    actual_pat_cr: float,
    consensus: SelfConsensus,
) -> Optional[float]:
    """Signed surprise vs self-consensus expected PAT. None if no estimate."""
    if consensus.expected_pat_cr is None or consensus.expected_pat_cr == 0.0:
        return None
    return ((actual_pat_cr - consensus.expected_pat_cr) / abs(consensus.expected_pat_cr)) * 100.0


# ── Component implementations ────────────────────────────────────────────────

def _compute_trailing_cagr(pats: List[Optional[float]]) -> float:
    """4-quarter trailing PAT CAGR, capped at ±50%."""
    valid = [(i, p) for i, p in enumerate(pats) if p is not None]
    if len(valid) < _MIN_QUARTERS_TREND:
        return 0.0
    oldest_val = valid[-4][1] if len(valid) >= 4 else valid[0][1]
    newest_val = valid[-1][1]
    if oldest_val <= 0 or newest_val is None:
        return 0.0
    try:
        cagr = (newest_val / oldest_val) ** 0.25 - 1.0
    except (ValueError, ZeroDivisionError):
        return 0.0
    return max(_MIN_GROWTH, min(_MAX_GROWTH, cagr))


def _compute_seasonality(
    quarters: List[dict],
    target_fiscal_quarter: int,
) -> float:
    """YoY growth for the specific target fiscal quarter, capped at ±50%."""
    same_q = [
        q for q in quarters
        if q.get("fiscal_quarter") == target_fiscal_quarter
        and q.get("pat_cr") is not None
    ]
    if len(same_q) < _MIN_QUARTERS_SEASONAL:
        return 0.0
    latest = same_q[-1]["pat_cr"]
    prior = same_q[-2]["pat_cr"]
    if not prior or prior <= 0:
        return 0.0
    growth = (latest / prior) - 1.0
    return max(_MIN_GROWTH, min(_MAX_GROWTH, growth))


def _compute_revenue_momentum(revenues: List[Optional[float]]) -> float:
    """Revenue growth acceleration (delta of growth rates), capped at ±30%."""
    valid = [r for r in revenues if r is not None and r > 0]
    if len(valid) < 3:
        return 0.0
    growth_rates = []
    for i in range(1, len(valid)):
        growth_rates.append(valid[i] / valid[i - 1] - 1.0)
    if len(growth_rates) < 2:
        return 0.0
    acceleration = growth_rates[-1] - growth_rates[-2]
    return max(-0.30, min(0.30, acceleration))


def _guidance_is_usable(guidance: dict) -> bool:
    tone = (guidance.get("tone") or "").upper()
    return tone in ("RAISED", "CUT", "REAFFIRMED") and tone != "SILENT"


def _apply_guidance(
    base_expected: float,
    base_pat: float,
    guidance: dict,
    w_guidance: float,
) -> float:
    """Blend management guidance into the expected PAT.

    If the company guided PAT growth explicitly, trust it at w_guidance weight.
    Otherwise adjust qualitatively for tone.
    """
    tone = (guidance.get("tone") or "").upper()
    delta_pct = guidance.get("delta_pct")

    if delta_pct is not None:
        try:
            guided_expected = base_pat * (1.0 + float(delta_pct) / 100.0)
            # 60% guidance, 40% model
            return base_expected * (1.0 - w_guidance) + guided_expected * w_guidance
        except (TypeError, ValueError):
            pass

    # Qualitative tone adjustment
    if tone == "RAISED":
        return base_expected * (1.0 + w_guidance * 0.1)
    if tone == "CUT":
        return base_expected * (1.0 - w_guidance * 0.15)
    return base_expected


def _apply_event_chain(
    base_expected: float,
    events: List[dict],
    w_event: float,
) -> float:
    """Adjust expected PAT for recent material corporate events."""
    adjustment = 0.0
    for evt in events:
        etype = (evt.get("event_type") or "").upper()
        esubtype = (evt.get("event_subtype") or "").upper()
        urgency = float(evt.get("urgency") or 0.0)
        direction = (evt.get("direction_hint") or "").upper()

        if etype == "ORDER_WIN" and urgency >= 7:
            adjustment += 0.05
        elif etype == "MANAGEMENT_CHANGE" and direction == "SHORT":
            adjustment -= 0.10
        elif etype == "CREDIT_RATING" and "DOWNGRADE" in esubtype:
            adjustment -= 0.08
        elif etype == "FINANCIAL_RESULTS" and "BLOWOUT" in esubtype:
            adjustment += 0.03

    if adjustment == 0.0:
        return base_expected
    # Weight the event chain adjustment by w_event
    return base_expected * (1.0 + w_event * adjustment / 0.05)


def _last_valid(values: List[Optional[float]]) -> Optional[float]:
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _growth_variance(pats: List[float]) -> float:
    """Standard deviation of sequential QoQ growth rates."""
    if len(pats) < 3:
        return 1.0
    rates = []
    for i in range(1, len(pats)):
        if pats[i - 1] > 0:
            rates.append(pats[i] / pats[i - 1] - 1.0)
    if not rates:
        return 1.0
    mean = sum(rates) / len(rates)
    variance = sum((r - mean) ** 2 for r in rates) / len(rates)
    return math.sqrt(variance)
