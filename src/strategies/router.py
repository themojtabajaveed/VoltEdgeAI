"""
router.py — DawnHydraRouter
---------------------------
Deterministic pre-market routing boundary between DAWN and HYDRA.

Given a pre-market candidate (from HYDRA's watchlist), decide whether it
should be traded by DAWN (blind market order at 09:15, entire move
compresses into the first 15 minutes) or by HYDRA (wait for intraday
TA confirmation).

Routing policy: 6 rules. ALL must pass to route to DAWN. Any failure
defaults to HYDRA. This keeps DAWN reserved for the top-tier, liquid,
fresh, category-aligned catalysts and routes everything else to the
more cautious TA-confirmation path.

Pure function — no I/O beyond logging.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from src.strategies.base import WatchlistEntry
from src.data_ingestion.pre_market_data import PreMarketSignals

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Category policy ──────────────────────────────────────────────────────
# Catalysts whose entire move compresses into the first 15 minutes after open.
DAWN_CATEGORIES = frozenset({
    "ORDER_WIN",
    "ACQUISITION",
    "MERGER",
    "RESULTS_BLOWOUT",
    "REGULATORY_CLEARANCE",
    "INDEX_ADDITION",
    "MAJOR_CONTRACT",
    "FDA_APPROVAL",
    "EARNINGS_SURPRISE",
})

# ── Rule thresholds ──────────────────────────────────────────────────────
_MIN_FILING_URGENCY = 8.5
_MIN_FILING_IMPACT = 8.0
_MIN_GAP_ABS = 2.0
_MAX_HOURS_SINCE_FILING = 16.0
_MIN_FOLLOW_THROUGH = 0.60
_COLD_START_FOLLOW_THROUGH = 0.55
_MIN_AVG_VOLUME_20D = 300_000


@dataclass
class RouteDecision:
    """Outcome of a single router invocation."""
    symbol: str
    route: str                      # "DAWN" or "HYDRA"
    confidence: float               # rules_passed / 6.0
    rules_passed: List[str] = field(default_factory=list)
    rules_failed: List[str] = field(default_factory=list)
    reasoning: str = ""


# ── Internal helpers ─────────────────────────────────────────────────────

def _coalesce_filing_urgency(
    entry: WatchlistEntry, premarket: Optional[PreMarketSignals]
) -> float:
    if premarket is not None and premarket.filing_urgency is not None:
        return float(premarket.filing_urgency)
    return float(entry.filing_urgency or 0.0)


def _coalesce_filing_impact(premarket: Optional[PreMarketSignals]) -> float:
    if premarket is not None and premarket.filing_impact_score is not None:
        return float(premarket.filing_impact_score)
    return 0.0


def _coalesce_filing_category(
    entry: WatchlistEntry, premarket: Optional[PreMarketSignals]
) -> str:
    cat = ""
    if premarket is not None and premarket.filing_category:
        cat = premarket.filing_category
    elif entry.filing_category:
        cat = entry.filing_category
    return str(cat).upper().strip()


def _coalesce_gap_pct(
    entry: WatchlistEntry, premarket: Optional[PreMarketSignals]
) -> float:
    if premarket is not None and premarket.gap_pct:
        return float(premarket.gap_pct)
    return float(entry.gap_pct or 0.0)


def _coalesce_avg_volume(
    entry: WatchlistEntry, premarket: Optional[PreMarketSignals]
) -> int:
    if premarket is not None and premarket.avg_volume_20d:
        return int(premarket.avg_volume_20d)
    return int(entry.avg_volume_20d or 0)


def _hours_since_filing(filed_at: Optional[str]) -> Optional[float]:
    """Return hours elapsed since filed_at (ISO string). None if unparseable."""
    if not filed_at:
        return None
    try:
        ts = datetime.fromisoformat(filed_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        now = datetime.now(IST)
        delta = (now - ts).total_seconds() / 3600.0
        return max(0.0, delta)
    except (ValueError, TypeError) as e:
        logger.debug(f"[Router] filed_at parse failed: {filed_at!r} ({e})")
        return None


def _lookup_follow_through_rate(
    pattern_db: Optional[dict], category: str, direction: str
) -> Optional[float]:
    """Look up historical follow-through rate for (category, direction).
    Returns None when no history exists (cold start)."""
    if not pattern_db or not category:
        return None
    if not isinstance(pattern_db, dict):
        return None
    # Best-effort schema scan: support a few common shapes.
    key_upper = category.upper()
    dir_upper = (direction or "").upper()
    for root_key in ("routing", "categories", "filing_categories"):
        bucket = pattern_db.get(root_key)
        if not isinstance(bucket, dict):
            continue
        entry = bucket.get(key_upper) or bucket.get(category)
        if not isinstance(entry, dict):
            continue
        # Try (category, direction) combined
        dir_entry = entry.get(dir_upper) or entry.get(direction)
        if isinstance(dir_entry, dict):
            rate = dir_entry.get("follow_through_rate") or dir_entry.get("hit_rate")
            if isinstance(rate, (int, float)):
                return float(rate)
        # Fall back to category-level rate
        rate = entry.get("follow_through_rate") or entry.get("hit_rate")
        if isinstance(rate, (int, float)):
            return float(rate)
    return None


# ── Main router ──────────────────────────────────────────────────────────

def route_candidate(
    entry: WatchlistEntry,
    premarket: Optional[PreMarketSignals],
    pattern_db: Optional[dict] = None,
) -> RouteDecision:
    """Route a pre-market candidate to DAWN or HYDRA.

    All 6 rules must pass for DAWN; any failure → HYDRA.
    """
    passed: List[str] = []
    failed: List[str] = []
    notes: List[str] = []

    filing_urgency = _coalesce_filing_urgency(entry, premarket)
    filing_impact = _coalesce_filing_impact(premarket)
    category = _coalesce_filing_category(entry, premarket)
    gap_pct = _coalesce_gap_pct(entry, premarket)
    avg_volume = _coalesce_avg_volume(entry, premarket)
    direction = (entry.direction or "").upper()

    # R1 — Top-tier catalyst (urgency OR impact score clears the bar)
    if filing_urgency >= _MIN_FILING_URGENCY or filing_impact >= _MIN_FILING_IMPACT:
        passed.append("R1")
        notes.append(
            f"urgency={filing_urgency:.1f} impact={filing_impact:.1f}"
        )
    else:
        failed.append("R1")
        notes.append(
            f"urgency={filing_urgency:.1f}<{_MIN_FILING_URGENCY} "
            f"impact={filing_impact:.1f}<{_MIN_FILING_IMPACT}"
        )

    # R2 — Filing category is in the DAWN whitelist
    if category and category in DAWN_CATEGORIES:
        passed.append("R2")
    else:
        failed.append("R2")
        notes.append(f"category={category or 'UNKNOWN'} not in DAWN set")

    # R3 — Gap must actually be meaningful (direction-aware).
    #     gap_pct == 0.0 means pre-market data was unavailable → HYDRA.
    if gap_pct == 0.0:
        failed.append("R3")
        notes.append("gap_pct=0.0 (pre-market data unavailable)")
    elif direction == "SHORT":
        if gap_pct <= -_MIN_GAP_ABS:
            passed.append("R3")
        else:
            failed.append("R3")
            notes.append(f"SHORT gap={gap_pct:+.2f}%>{-_MIN_GAP_ABS}")
    else:  # LONG / BUY / default
        if gap_pct >= _MIN_GAP_ABS:
            passed.append("R3")
        else:
            failed.append("R3")
            notes.append(f"LONG gap={gap_pct:+.2f}%<{_MIN_GAP_ABS}")

    # R4 — Freshness. Stale catalysts don't compress.
    #     If filed_at unknown, give the benefit of the doubt — pass R4 but note it.
    hours_since = _hours_since_filing(entry.filed_at)
    if hours_since is None:
        passed.append("R4")
        notes.append("filed_at unknown — R4 assumed fresh")
    elif hours_since <= _MAX_HOURS_SINCE_FILING:
        passed.append("R4")
        notes.append(f"filed {hours_since:.1f}h ago")
    else:
        failed.append("R4")
        notes.append(f"filed {hours_since:.1f}h ago>{_MAX_HOURS_SINCE_FILING}")

    # R5 — Historical follow-through rate.
    #     Cold start (no pattern_db history) passes as "R5_COLD_START" with a
    #     confidence penalty, rather than forcing HYDRA. This lets DAWN take
    #     first-ever trades in a category when every other rule is green.
    ft_rate = _lookup_follow_through_rate(pattern_db, category, direction)
    if ft_rate is None:
        passed.append("R5_COLD_START")
        notes.append(
            f"R5 cold-start — no historical validation yet "
            f"(assumed {_COLD_START_FOLLOW_THROUGH:.2f})"
        )
    elif ft_rate >= _MIN_FOLLOW_THROUGH:
        passed.append("R5")
        notes.append(f"follow-through={ft_rate:.2f}")
    else:
        failed.append("R5")
        notes.append(f"follow-through={ft_rate:.2f}<{_MIN_FOLLOW_THROUGH}")

    # R6 — Liquidity gate
    if avg_volume >= _MIN_AVG_VOLUME_20D:
        passed.append("R6")
    else:
        failed.append("R6")
        notes.append(f"avg_vol_20d={avg_volume}<{_MIN_AVG_VOLUME_20D}")

    route = "DAWN" if not failed else "HYDRA"
    confidence = round(len(passed) / 6.0, 2)
    reasoning = (
        f"{route}: "
        + (f"passed {passed}" if passed else "no rules passed")
        + (f"; failed {failed}" if failed else "")
        + (f" — {'; '.join(notes)}" if notes else "")
    )

    decision = RouteDecision(
        symbol=entry.symbol,
        route=route,
        confidence=confidence,
        rules_passed=passed,
        rules_failed=failed,
        reasoning=reasoning,
    )
    # Stamp decision onto the entry so downstream consumers can read it.
    try:
        entry.route = route
        entry.confidence = confidence
        entry.routed_at = datetime.now(IST).isoformat()
    except AttributeError:
        # Older persisted WatchlistEntry missing new fields — skip silently.
        pass
    logger.info(
        f"[Router] {entry.symbol} → {route} | "
        f"rules_passed={passed} rules_failed={failed}"
    )
    logger.info(f"[Router] {entry.symbol} reasoning: {reasoning}")
    return decision


# ── Shadow tracker ───────────────────────────────────────────────────────

def create_hydra_shadow(entry: WatchlistEntry) -> dict:
    """
    For every DAWN-routed signal, create a shadow entry for HYDRA to track.
    Used for counterfactual learning: what would HYDRA's TA-confirmation
    path have produced for this same symbol? `open_price_9_15` is filled
    in later (by runner when 09:15 prices are known).
    """
    return {
        "symbol": entry.symbol,
        "direction": entry.direction,
        "filing_category": entry.filing_category,
        "gap_pct": entry.gap_pct,
        "dawn_entry_time": datetime.now(IST).isoformat(),
        "open_price_9_15": None,
        "event_summary": entry.event_summary,
    }
