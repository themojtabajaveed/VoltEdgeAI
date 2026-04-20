"""
event_quality.py — Event quality scoring (Layer 3 of the R4 evaluation model)
------------------------------------------------------------------------------
Before a filing passes to routing/conviction, ask: is this event fundamentally
worth trading? A routine board-meeting notice and a ₹500cr order win may both
be PRISTINE in terms of market-time exposure, but only one is worth a position.

score_event_quality() returns a 0-100 score from five weighted components:
  1. Event type magnitude       (35%)
  2. Deal/order size / mcap     (25%)
  3. Earnings surprise magnitude (20%)
  4. Sector historical follow-through (10%)
  5. Repetition penalty          (10%)

passes_quality_gate() applies config["min_passing_score"] against the score.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Component 1: event-type magnitude lookup table ──────────────────────────
# Keyed by upper-cased category string. Unknown categories → UNKNOWN (40).

EVENT_TYPE_SCORES: dict[str, int] = {
    # High magnitude 80-100
    "MERGER": 95, "ACQUISITION": 95, "TAKEOVER": 98,
    "LARGE_ORDER_WIN": 88, "ORDER_WIN": 80,
    "DRUG_APPROVAL": 90, "LICENSE_APPROVAL": 85,
    "QIP": 78, "RIGHTS_ISSUE": 72, "PREFERENTIAL_ALLOTMENT": 70,
    "RESULTS_BLOWOUT": 88, "RESULTS_BEAT": 75,
    # Medium magnitude 40-75
    "DIVIDEND_HIGH": 68, "DIVIDEND_NORMAL": 50,
    "MANAGEMENT_CHANGE_CEO": 60, "MANAGEMENT_CHANGE_CFO": 52,
    "JOINT_VENTURE": 65, "MOU": 55,
    "CAPEX_ANNOUNCEMENT": 60, "EXPANSION": 58,
    # Low magnitude 0-35
    "RESULTS_MISS": 30, "RESULTS_INLINE": 35,
    "DEBT_RESTRUCTURE": 32, "RATING_DOWNGRADE": 25,
    "BOARD_MEETING": 12, "AGM": 8, "EGM": 15,
    "DIVIDEND_RECORD_DATE": 20, "BOOK_CLOSURE": 18,
    "INVESTOR_MEET": 22,
    # Default for unmapped categories
    "UNKNOWN": 40,
}

_DEFAULT_CONFIG: dict[str, float] = {
    "weight_event_type": 0.35,
    "weight_deal_size_ratio": 0.25,
    "weight_earnings_surprise": 0.20,
    "weight_sector_followthrough": 0.10,
    "weight_repetition_penalty": 0.10,
    "repetition_lookback_days": 5,
    "min_passing_score": 55.0,
}


# ── Component scorers ────────────────────────────────────────────────────────

def _score_event_type(category: Optional[str]) -> int:
    cat = (category or "").upper().strip()
    return EVENT_TYPE_SCORES.get(cat, EVENT_TYPE_SCORES["UNKNOWN"])


def _score_deal_size_ratio(
    deal_size_inr: Optional[float], market_cap_inr: Optional[float]
) -> int:
    if deal_size_inr is None or market_cap_inr is None or market_cap_inr <= 0:
        return 40  # neutral — data unavailable
    ratio = deal_size_inr / market_cap_inr
    if ratio >= 0.20:
        return 95
    if ratio >= 0.10:
        return 82
    if ratio >= 0.05:
        return 68
    if ratio >= 0.02:
        return 50
    if ratio >= 0.01:
        return 35
    return 15


def _score_earnings_surprise(surprise_pct: Optional[float]) -> int:
    if surprise_pct is None:
        return 50  # neutral — component does not apply to non-results filings
    if surprise_pct >= 30:
        return 90
    if surprise_pct >= 10:
        return 75
    if surprise_pct >= 0:
        return 55
    if surprise_pct >= -10:
        return 35
    return 15  # miss < -10% — still >0 because it's a short signal


def _score_sector_followthrough(followthrough: Optional[float]) -> float:
    if followthrough is None:
        return 50.0  # neutral
    raw = float(followthrough) * 100.0
    return max(0.0, min(100.0, raw))


def _score_repetition(count: int) -> int:
    if count <= 0:
        return 100  # first occurrence
    if count == 1:
        return 60
    if count == 2:
        return 30
    return 5  # count >= 3 — spam


def _validate_weights(config: dict) -> None:
    """Log a WARNING if component weights do not sum to ~1.0. Never crash."""
    weights = [
        float(config.get("weight_event_type", _DEFAULT_CONFIG["weight_event_type"])),
        float(config.get("weight_deal_size_ratio", _DEFAULT_CONFIG["weight_deal_size_ratio"])),
        float(config.get("weight_earnings_surprise", _DEFAULT_CONFIG["weight_earnings_surprise"])),
        float(config.get("weight_sector_followthrough", _DEFAULT_CONFIG["weight_sector_followthrough"])),
        float(config.get("weight_repetition_penalty", _DEFAULT_CONFIG["weight_repetition_penalty"])),
    ]
    total = sum(weights)
    if abs(total - 1.0) > 0.01:
        logger.warning(
            f"[event_quality] component weights sum to {total:.3f}, expected 1.00 "
            f"(tolerance ±0.01). Scores will be distorted — check config.yaml "
            f"router.event_quality.weight_*."
        )


# ── Public API ───────────────────────────────────────────────────────────────

def score_event_quality(
    category: str,
    filing_subject: Optional[str],
    deal_size_inr: Optional[float],
    market_cap_inr: Optional[float],
    earnings_surprise_pct: Optional[float],
    sector_followthrough: Optional[float],
    recent_same_category_count: int,
    config: dict,
) -> tuple[float, dict]:
    """Compute the 0-100 event quality score for a filing.

    Args:
        category: Filing category string (case-insensitive). Unknown → UNKNOWN.
        filing_subject: Raw subject line — currently unused, reserved for future
            keyword enrichment (e.g. parsing deal size from the subject text).
        deal_size_inr: Deal / order value in INR. None → neutral component score.
        market_cap_inr: Company market capitalisation in INR.
        earnings_surprise_pct: PAT/revenue YoY surprise %. None for non-results.
        sector_followthrough: 0.0–1.0 follow-through rate from pattern_db. None → neutral.
        recent_same_category_count: Prior filings in last N days (same symbol+category).
        config: Config dict with weight_* and min_passing_score keys.

    Returns:
        (score, breakdown) — score rounded to 1 decimal, clamped [0, 100].
        breakdown maps each component name to {"raw": ..., "weight": ..., "contribution": ...}.
    """
    _validate_weights(config)

    w_event    = float(config.get("weight_event_type",          _DEFAULT_CONFIG["weight_event_type"]))
    w_deal     = float(config.get("weight_deal_size_ratio",     _DEFAULT_CONFIG["weight_deal_size_ratio"]))
    w_surprise = float(config.get("weight_earnings_surprise",   _DEFAULT_CONFIG["weight_earnings_surprise"]))
    w_sector   = float(config.get("weight_sector_followthrough",_DEFAULT_CONFIG["weight_sector_followthrough"]))
    w_rep      = float(config.get("weight_repetition_penalty",  _DEFAULT_CONFIG["weight_repetition_penalty"]))

    raw_event    = _score_event_type(category)
    raw_deal     = _score_deal_size_ratio(deal_size_inr, market_cap_inr)
    raw_surprise = _score_earnings_surprise(earnings_surprise_pct)
    raw_sector   = _score_sector_followthrough(sector_followthrough)
    raw_rep      = _score_repetition(recent_same_category_count)

    contrib_event    = raw_event    * w_event
    contrib_deal     = raw_deal     * w_deal
    contrib_surprise = raw_surprise * w_surprise
    contrib_sector   = raw_sector   * w_sector
    contrib_rep      = raw_rep      * w_rep

    total = contrib_event + contrib_deal + contrib_surprise + contrib_sector + contrib_rep
    total = max(0.0, min(100.0, total))
    score = round(total, 1)

    breakdown = {
        "event_type":          {"raw": raw_event,    "weight": w_event,    "contribution": round(contrib_event, 2)},
        "deal_size_ratio":     {"raw": raw_deal,     "weight": w_deal,     "contribution": round(contrib_deal, 2)},
        "earnings_surprise":   {"raw": raw_surprise, "weight": w_surprise, "contribution": round(contrib_surprise, 2)},
        "sector_followthrough":{"raw": raw_sector,   "weight": w_sector,   "contribution": round(contrib_sector, 2)},
        "repetition_penalty":  {"raw": raw_rep,      "weight": w_rep,      "contribution": round(contrib_rep, 2)},
    }
    return score, breakdown


def passes_quality_gate(score: float, config: dict) -> tuple[bool, str]:
    """Return (passes, reason_str) for the Layer-3 event-quality gate.

    A filing with quality < min_passing_score is discarded before routing.
    Default min_passing_score = 55 (see config.yaml router.event_quality).
    """
    min_score = float(config.get("min_passing_score", _DEFAULT_CONFIG["min_passing_score"]))
    if score >= min_score:
        return True, f"quality {score}/100 ≥ {min_score}"
    return False, f"quality {score}/100 < {min_score} — weak filing"


# ── Layer 4: Market Confirmation ─────────────────────────────────────────────
# Intraday HYDRA injection path: after we've cleared L1/L2 (freshness) and L3
# (event quality), the final check asks the tape itself — is the market
# actually reacting to this filing? Applied only when enough market minutes
# have elapsed for a reaction to be visible.

_L4_DEFAULTS: dict[str, float] = {
    "min_market_minutes_before_check": 15,
    "volume_spike_multiplier": 2.0,
    "min_price_velocity_pct": 0.5,
    "indifferent_conviction_penalty": 20,
    "confirmed_conviction_bonus": 10,
}


def classify_market_confirmation(
    market_minutes_elapsed: int,
    volume_current_15min: Optional[float],
    volume_avg_15min: Optional[float],
    price_velocity_pct: Optional[float],
    config: dict,
) -> tuple[str, str]:
    """Classify the tape's reaction to an intraday filing — Layer 4.

    Returns (status, reason) where status ∈ {"CONFIRMED", "INDIFFERENT",
    "TOO_EARLY", "NO_DATA"}.

    - TOO_EARLY: fewer than min_market_minutes_before_check have elapsed.
      Pass through: a PRISTINE filing should inject regardless of tape yet.
    - NO_DATA: both volume and velocity feeds are unavailable; skip gracefully.
    - CONFIRMED: volume spike OR price velocity clears threshold.
    - INDIFFERENT: market has had time to react and shows nothing — discard.

    Volume spike rule: volume_current_15min > volume_avg_15min × multiplier.
    Velocity rule: |price_velocity_pct| ≥ min_price_velocity_pct.
    Either signal alone is sufficient for CONFIRMED — this is the market
    speaking, not a fundamentals gate.
    """
    min_min = int(config.get(
        "min_market_minutes_before_check", _L4_DEFAULTS["min_market_minutes_before_check"]
    ))
    vol_mult = float(config.get(
        "volume_spike_multiplier", _L4_DEFAULTS["volume_spike_multiplier"]
    ))
    min_velocity = float(config.get(
        "min_price_velocity_pct", _L4_DEFAULTS["min_price_velocity_pct"]
    ))

    if market_minutes_elapsed < min_min:
        return (
            "TOO_EARLY",
            f"only {market_minutes_elapsed} market-min elapsed, need {min_min}",
        )

    if volume_current_15min is None and price_velocity_pct is None:
        return (
            "NO_DATA",
            "volume and velocity unavailable — skipping L4",
        )

    # Compute signals; None → treat as 0 (i.e. feed missing → doesn't fire).
    vol_cur = float(volume_current_15min or 0)
    vol_avg = float(volume_avg_15min or 0)
    velocity_abs = abs(float(price_velocity_pct or 0))

    volume_spike = vol_avg > 0 and vol_cur > (vol_avg * vol_mult)
    velocity_confirmed = velocity_abs >= min_velocity

    if volume_spike and velocity_confirmed:
        return (
            "CONFIRMED",
            f"volume spike {vol_cur:.0f} > {vol_avg:.0f}×{vol_mult} "
            f"AND velocity {velocity_abs:.2f}% ≥ {min_velocity}%",
        )
    if volume_spike:
        return (
            "CONFIRMED",
            f"volume spike {vol_cur:.0f} > {vol_avg:.0f}×{vol_mult} "
            f"(velocity {velocity_abs:.2f}% < {min_velocity}%)",
        )
    if velocity_confirmed:
        return (
            "CONFIRMED",
            f"velocity {velocity_abs:.2f}% ≥ {min_velocity}% "
            f"(no volume spike)",
        )
    return (
        "INDIFFERENT",
        f"no volume spike, no velocity after {market_minutes_elapsed} market-min",
    )
