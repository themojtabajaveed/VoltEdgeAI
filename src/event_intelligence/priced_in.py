"""'Already Priced In' assessment engine for event-intelligence signals.

Computes a priced_in_discount (0.0 → 1.0) that reduces effective urgency
when the market has already had time to absorb the information:

  effective_urgency = raw_urgency × (1.0 - priced_in_discount)

Discount sources (applied cumulatively, capped at 0.85):
  1. Board-meeting chain  — intimation > N days ago → market anticipated results
  2. Sector rotation      — sector moved > X% of expected gap → market already repriced
  3. Guidance overlap     — actual falls within previously-guided range → no surprise
  4. Pre-open gap check   — pre-open session already shows gap in direction → execution edge gone

All thresholds are v1 defaults exposed as module-level constants.
Tune from postmortem evidence.

Usage (in hydra_bridge or pre-open assembly):
    from src.event_intelligence.priced_in import assess_priced_in
    result = assess_priced_in(signal_record, data_dir="data")
    effective_urgency = result.effective_urgency
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Configurable thresholds (v1 defaults) ────────────────────────────────────
# Board-meeting chain
CHAIN_LOOKBACK_DAYS = 14       # how far back to search for prior intimation
CHAIN_MIN_DAYS = 3             # intimation > 3 days ago = partially priced in
CHAIN_DISCOUNT_FACTOR = 0.20   # 20% discount when chain detected

# Sector rotation
SECTOR_ATTRIBUTION_PCT = 0.50  # sector move explains > 50% of gap → discount
SECTOR_DISCOUNT_FACTOR = 0.25  # 25% discount when sector rotation detected

# Guidance overlap
GUIDANCE_OVERLAP_DISCOUNT = 0.40  # 40% discount when actual is within guided range

# Pre-open gap (applied by pre-open check job at 09:07 IST)
GAP_ALIGNED_THRESHOLD = 3.0   # gap > 3% in same direction → partially priced in
GAP_ALIGNED_DISCOUNT = 0.35   # 35% discount
GAP_OPPOSED_SIGNAL = True     # flag as CONFLICT_SIGNAL if gap opposes direction

# Maximum total discount (never fully zero-out a signal)
MAX_TOTAL_DISCOUNT = 0.85


@dataclass
class PricedInAssessment:
    """Result of the priced-in evaluation for one signal."""
    symbol: str
    raw_urgency: float
    priced_in_discount: float         # 0.0–0.85
    effective_urgency: float          # raw × (1 - discount)
    components: Dict[str, float] = field(default_factory=dict)
    # Individual discount contributions
    board_chain_discount: float = 0.0
    sector_discount: float = 0.0
    guidance_discount: float = 0.0
    gap_discount: float = 0.0
    # Flags
    conflict_signal: bool = False     # gap opposes filing direction
    novelty: str = "PRISTINE"        # PRISTINE|EXPECTED|CONTINUATION|STALE
    notes: List[str] = field(default_factory=list)


# ── Board-meeting chain detection ────────────────────────────────────────────

def _check_board_chain(
    symbol: str,
    filed_at: str,
    event_type: str,
    data_dir: str,
) -> tuple[float, str]:
    """Check if a board-meeting intimation preceded this results filing.

    Returns (discount, note).
    Board meeting intimations are stored in earnings_schedule_{date}.json.
    If we find one for the same symbol > CHAIN_MIN_DAYS before the current
    filing, the market had advance notice.
    """
    if event_type != "FINANCIAL_RESULTS":
        return 0.0, ""

    try:
        current_dt = datetime.fromisoformat(filed_at)
    except (ValueError, TypeError):
        return 0.0, ""

    # Search schedule files for the past CHAIN_LOOKBACK_DAYS
    for days_back in range(CHAIN_MIN_DAYS, CHAIN_LOOKBACK_DAYS + 1):
        check_date = (current_dt - timedelta(days=days_back)).strftime("%Y-%m-%d")
        schedule_path = os.path.join(data_dir, f"earnings_schedule_{check_date}.json")

        if not os.path.exists(schedule_path):
            continue

        try:
            with open(schedule_path) as f:
                records = json.load(f)
            if not isinstance(records, list):
                continue
        except (json.JSONDecodeError, OSError):
            continue

        for rec in records:
            rec_symbol = rec.get("symbol", "")
            if rec_symbol.upper() == symbol.upper():
                note = (
                    f"board_chain: intimation found {days_back}d ago "
                    f"({check_date}) → discount {CHAIN_DISCOUNT_FACTOR:.0%}"
                )
                return CHAIN_DISCOUNT_FACTOR, note

    return 0.0, ""


# ── Sector rotation discount ────────────────────────────────────────────────

def _check_sector_rotation(
    symbol: str,
    direction_hint: str,
    data_dir: str,
) -> tuple[float, str]:
    """Check if the symbol's sector has already moved significantly.

    Uses the tradability_universe.json to find sector membership,
    then checks sector_performance.json (produced by overnight job)
    for sector-level returns.

    If the sector moved > SECTOR_ATTRIBUTION_PCT of the expected gap
    in the same direction, apply discount.
    """
    # Load sector membership
    universe_path = Path(data_dir) / "tradability_universe.json"
    if not universe_path.exists():
        return 0.0, ""

    try:
        with open(universe_path) as f:
            universe = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0.0, ""

    # Find sector for this symbol
    if isinstance(universe, list):
        record = next((r for r in universe if r.get("symbol", "").upper() == symbol.upper()), None)
    elif isinstance(universe, dict):
        record = universe.get(symbol) or universe.get(symbol.upper())
    else:
        return 0.0, ""

    if not record:
        return 0.0, ""

    sector = record.get("sector", "")
    if not sector:
        return 0.0, ""

    # Load sector performance
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    sector_path = Path(data_dir) / f"sector_performance_{today_str}.json"
    if not sector_path.exists():
        # Try generic (non-dated) path
        sector_path = Path(data_dir) / "sector_performance.json"
        if not sector_path.exists():
            return 0.0, ""

    try:
        with open(sector_path) as f:
            sector_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0.0, ""

    sector_return = None
    if isinstance(sector_data, dict):
        sector_entry = sector_data.get(sector) or sector_data.get(sector.upper())
        if isinstance(sector_entry, dict):
            sector_return = sector_entry.get("return_pct") or sector_entry.get("pct_change")
        elif isinstance(sector_entry, (int, float)):
            sector_return = float(sector_entry)

    if sector_return is None:
        return 0.0, ""

    # Check if sector move aligns with filing direction
    sector_direction = "BUY" if sector_return > 0 else "SHORT"
    if sector_direction != direction_hint:
        return 0.0, ""

    # If sector already moved significantly, it's partially priced in
    if abs(sector_return) > SECTOR_ATTRIBUTION_PCT * 3.0:  # 3% as "expected gap" proxy
        note = (
            f"sector_rotation: {sector} moved {sector_return:+.1f}% "
            f"(same direction) → discount {SECTOR_DISCOUNT_FACTOR:.0%}"
        )
        return SECTOR_DISCOUNT_FACTOR, note

    return 0.0, ""


# ── Guidance overlap discount ────────────────────────────────────────────────

def _check_guidance_overlap(
    signal_record: Dict[str, Any],
) -> tuple[float, str]:
    """Check if the actual result falls within previously-guided range.

    If company gave forward guidance and the actual result is within that
    range, the surprise is minimal → large discount.
    """
    guidance = signal_record.get("guidance")
    if not guidance or not isinstance(guidance, dict):
        return 0.0, ""

    tone = (guidance.get("tone") or "").upper()
    if tone == "SILENT":
        return 0.0, ""

    # If guidance was REAFFIRMED and actual aligns, big discount
    earnings = signal_record.get("earnings")
    if not earnings or not isinstance(earnings, dict):
        return 0.0, ""

    surprise_pct = earnings.get("surprise_pct")
    if surprise_pct is None:
        return 0.0, ""

    # If surprise is tiny (within ±5%), guidance was spot-on
    if abs(surprise_pct) <= 5.0 and tone == "REAFFIRMED":
        note = (
            f"guidance_overlap: actual within reaffirmed range "
            f"(surprise={surprise_pct:+.1f}%) → discount {GUIDANCE_OVERLAP_DISCOUNT:.0%}"
        )
        return GUIDANCE_OVERLAP_DISCOUNT, note

    # If guidance was RAISED and actual matches the raise, partially priced in
    delta_pct = guidance.get("delta_pct")
    if tone == "RAISED" and delta_pct is not None:
        try:
            if abs(surprise_pct) <= abs(float(delta_pct)) * 0.5:
                note = (
                    f"guidance_overlap: actual within raised guidance "
                    f"(surprise={surprise_pct:+.1f}%, guided={delta_pct:+.1f}%) "
                    f"→ discount {GUIDANCE_OVERLAP_DISCOUNT * 0.7:.0%}"
                )
                return GUIDANCE_OVERLAP_DISCOUNT * 0.7, note
        except (TypeError, ValueError):
            pass

    return 0.0, ""


# ── Pre-open gap check ───────────────────────────────────────────────────────

def _check_preopen_gap(
    symbol: str,
    direction_hint: str,
    data_dir: str,
) -> tuple[float, bool, str]:
    """Check if the pre-open session already shows gap in expected direction.

    Returns (discount, is_conflict_signal, note).
    Reads from premarket_cache_{today}.json which is populated at ~09:07 IST.
    """
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    cache_path = os.path.join(data_dir, f"premarket_cache_{today_str}.json")

    if not os.path.exists(cache_path):
        return 0.0, False, ""

    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0.0, False, ""

    # Extract pre-open gap for this symbol
    signals = cache.get("signals") or cache
    entry = None
    if isinstance(signals, dict):
        entry = signals.get(symbol) or signals.get(symbol.upper())
    elif isinstance(signals, list):
        entry = next(
            (s for s in signals if (s.get("symbol") or "").upper() == symbol.upper()),
            None,
        )

    if not entry or not isinstance(entry, dict):
        return 0.0, False, ""

    gap_pct = entry.get("gap_pct") or entry.get("preopen_gap_pct")
    if gap_pct is None:
        return 0.0, False, ""

    try:
        gap_pct = float(gap_pct)
    except (TypeError, ValueError):
        return 0.0, False, ""

    # Determine if gap aligns or opposes the filing direction
    gap_direction = "BUY" if gap_pct > 0 else "SHORT" if gap_pct < 0 else "NEUTRAL"

    if gap_direction == direction_hint and abs(gap_pct) >= GAP_ALIGNED_THRESHOLD:
        # Gap aligns with our signal → market already moved, less edge
        note = (
            f"preopen_gap: {gap_pct:+.1f}% aligns with {direction_hint} "
            f"→ discount {GAP_ALIGNED_DISCOUNT:.0%}"
        )
        return GAP_ALIGNED_DISCOUNT, False, note

    if (
        GAP_OPPOSED_SIGNAL
        and gap_direction != "NEUTRAL"
        and gap_direction != direction_hint
        and abs(gap_pct) >= GAP_ALIGNED_THRESHOLD
    ):
        # Gap opposes filing direction — potential conflict / SHORT opportunity
        note = (
            f"preopen_gap: {gap_pct:+.1f}% OPPOSES {direction_hint} "
            f"→ CONFLICT_SIGNAL flagged"
        )
        return 0.0, True, note

    return 0.0, False, ""


# ── Main assessment function ─────────────────────────────────────────────────

def assess_priced_in(
    signal_record: Dict[str, Any],
    data_dir: str = "data",
    *,
    check_board_chain: bool = True,
    check_sector: bool = True,
    check_guidance: bool = True,
    check_preopen: bool = True,
) -> PricedInAssessment:
    """Assess how much of a signal's information is already priced in.

    Args:
        signal_record: dict from earnings_signals JSON (or EarningsSignal.to_dict())
        data_dir: path to data directory containing artifacts
        check_*: toggle individual discount sources (for testing/phased rollout)

    Returns:
        PricedInAssessment with discount breakdown and effective urgency.
    """
    symbol = signal_record.get("symbol", "")
    raw_urgency = float(signal_record.get("urgency", 0.0))
    filed_at = signal_record.get("filed_at", "")
    direction_hint = signal_record.get("direction_hint", "BUY")
    event_type = signal_record.get("event_type", "")

    notes: List[str] = []
    board_discount = 0.0
    sector_disc = 0.0
    guidance_disc = 0.0
    gap_disc = 0.0
    conflict_signal = False

    # 1. Board-meeting chain
    if check_board_chain:
        board_discount, note = _check_board_chain(symbol, filed_at, event_type, data_dir)
        if note:
            notes.append(note)

    # 2. Sector rotation
    if check_sector:
        sector_disc, note = _check_sector_rotation(symbol, direction_hint, data_dir)
        if note:
            notes.append(note)

    # 3. Guidance overlap
    if check_guidance:
        guidance_disc, note = _check_guidance_overlap(signal_record)
        if note:
            notes.append(note)

    # 4. Pre-open gap
    if check_preopen:
        gap_disc, conflict_signal, note = _check_preopen_gap(
            symbol, direction_hint, data_dir
        )
        if note:
            notes.append(note)

    # ── Combine discounts (cumulative, capped) ────────────────────────────
    total_discount = board_discount + sector_disc + guidance_disc + gap_disc
    total_discount = min(total_discount, MAX_TOTAL_DISCOUNT)

    # ── Novelty classification ────────────────────────────────────────────
    if total_discount >= 0.50:
        novelty = "EXPECTED"
    elif total_discount >= 0.20:
        novelty = "CONTINUATION"
    elif board_discount > 0:
        novelty = "EXPECTED"
    else:
        novelty = "PRISTINE"

    effective_urgency = raw_urgency * (1.0 - total_discount)

    assessment = PricedInAssessment(
        symbol=symbol,
        raw_urgency=raw_urgency,
        priced_in_discount=round(total_discount, 3),
        effective_urgency=round(effective_urgency, 2),
        components={
            "board_chain": board_discount,
            "sector_rotation": sector_disc,
            "guidance_overlap": guidance_disc,
            "preopen_gap": gap_disc,
        },
        board_chain_discount=board_discount,
        sector_discount=sector_disc,
        guidance_discount=guidance_disc,
        gap_discount=gap_disc,
        conflict_signal=conflict_signal,
        novelty=novelty,
        notes=notes,
    )

    if total_discount > 0 or conflict_signal:
        logger.info(
            "[PricedIn] %s: discount=%.0f%% (chain=%.0f%% sector=%.0f%% "
            "guidance=%.0f%% gap=%.0f%%) eff_urgency=%.1f%s",
            symbol,
            total_discount * 100,
            board_discount * 100,
            sector_disc * 100,
            guidance_disc * 100,
            gap_disc * 100,
            effective_urgency,
            " CONFLICT" if conflict_signal else "",
        )

    return assessment


# ── Batch assessment (for pre-open assembly) ─────────────────────────────────

def assess_batch(
    signals: List[Dict[str, Any]],
    data_dir: str = "data",
    **kwargs,
) -> List[PricedInAssessment]:
    """Assess priced-in discount for a batch of signals.

    Sorts results by effective_urgency descending.
    """
    results = []
    for sig in signals:
        assessment = assess_priced_in(sig, data_dir=data_dir, **kwargs)
        results.append(assessment)
    results.sort(key=lambda a: a.effective_urgency, reverse=True)
    return results
