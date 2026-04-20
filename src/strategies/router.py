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
from src.utils.event_quality import passes_quality_gate, score_event_quality
from src.utils.filing_freshness import FilingFreshness, classify_filing_freshness

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


def _parse_filing_ts(filed_at: Optional[str]) -> Optional[datetime]:
    """Parse an ISO filed_at string → IST-aware datetime. None if unparseable/missing."""
    if not filed_at:
        return None
    try:
        ts = datetime.fromisoformat(filed_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        return ts
    except (ValueError, TypeError) as e:
        logger.debug(f"[Router] filed_at parse failed: {filed_at!r} ({e})")
        return None


def _get_sector_followthrough(
    pattern_db: Optional[dict], category: str, direction: str
) -> Optional[float]:
    """Return sector/category historical follow-through rate (0.0-1.0) or None.

    Supported shapes:
      - dict (legacy): nested {"routing"|"categories"|"filing_categories":
        {CATEGORY: {follow_through_rate | hit_rate: 0.xx}}} — delegate.
      - list (current on-disk shape): flat list of trade fingerprints. No
        aggregator wired yet; see TODO below.

    TODO (Step 9 follow-up): pattern_db.json is a list of per-trade
    fingerprints where `triggered=True` entries carry pnl_pct outcomes. As of
    2026-04-20 only 67 legacy/expired entries exist and 0 are triggered, so
    hit-rate aggregation would produce no meaningful data. Wire a
    list-shape aggregator once the triggered-entry count crosses a usable
    threshold (~20 per (sector, category) bucket). Returning None here is safe:
    event_quality treats None as neutral (50/100), not a penalty.
    """
    if pattern_db is None:
        return None
    # Legacy dict-based hit-rate map: delegate when available.
    if isinstance(pattern_db, dict):
        return _lookup_follow_through_rate(pattern_db, category, direction)
    # List-shape (on-disk): cold start → neutral.
    return None


def _get_recent_category_count(
    entry: WatchlistEntry, category: str, lookback_days: int
) -> int:
    """Count recent same-symbol+category filings within the lookback window.

    Step 4 scope: use what's already on the entry (metadata dict). If the
    caller has stashed a count under entry.metadata['recent_same_category_count'],
    use it; otherwise return 0. Full premarket-cache scanning is wired in Step 6.
    """
    if not category:
        return 0
    try:
        md = getattr(entry, "metadata", None) or {}
        val = md.get("recent_same_category_count")
        if isinstance(val, (int, float)) and val >= 0:
            return int(val)
    except (AttributeError, TypeError):
        pass
    return 0


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

    All 6 rules must pass for DAWN; any failure → HYDRA. If config says
    `router.router_enabled=false`, all candidates pass through as UNROUTED.
    If route=DAWN but confidence < router.dawn_confidence_min, demote to HYDRA.
    """
    # ── Phase 4: router kill-switch ─────────────────────────────────────
    try:
        from src.config_loader import get_router_enabled
        router_enabled = get_router_enabled()
    except Exception:
        router_enabled = True
    if not router_enabled:
        logger.info(
            "[ROUTER] Router disabled via config — all candidates pass through unrouted"
        )
        try:
            entry.route = "UNROUTED"
            entry.confidence = 0.0
            entry.routed_at = datetime.now(IST).isoformat()
        except AttributeError:
            pass
        return RouteDecision(
            symbol=entry.symbol,
            route="UNROUTED",
            confidence=0.0,
            rules_passed=[],
            rules_failed=[],
            reasoning="router_enabled=false — bypassed",
        )

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

    # R4 — 4-Layer Event Evaluation.
    #   Layer 1: market-time exposure (NSE session minutes, not wall clock)
    #   Layer 2: price reaction gate (stock already moved → downgrade)
    #   Layer 3: event quality score 0-100 (magnitude, deal size, surprise, ...)
    #   Layer 4: market confirmation — applied downstream in conviction_engine
    #
    # A filing PASSES R4 iff freshness ∈ {PRISTINE, FRESH, WARM} AND the
    # event_quality score clears min_passing_score. STALE freshness or a
    # weak filing (<min_passing_score) → R4 fails → HYDRA.
    # If filed_at is unknown we can't run L1/L2 — record R4_SKIPPED and let
    # downstream conviction scoring apply its own penalty.
    filing_ts = _parse_filing_ts(entry.filed_at)
    # Load freshness + quality config once; any exception → safe hardcoded defaults.
    try:
        from src.config_loader import (
            get_event_quality_config,
            get_filing_freshness_config,
        )
        fresh_cfg = get_filing_freshness_config()
        quality_cfg = get_event_quality_config()
    except Exception as e:
        logger.debug(f"[Router] event-evaluation config unavailable ({e}) — using defaults")
        fresh_cfg = {
            "fresh_market_minutes_max": 30, "warm_market_minutes_max": 90,
            "price_reaction_fresh_pct": 1.0, "price_reaction_warm_pct": 2.0,
        }
        quality_cfg = {"min_passing_score": 55.0}

    if filing_ts is None:
        # No timestamp → can't classify freshness. Skip L1/L2, still score L3 quality.
        passed.append("R4_SKIPPED")
        notes.append("filed_at unknown — freshness skipped")
        entry.filing_freshness = None
    else:
        price_at_filing = getattr(entry, "price_at_filing_time", None)
        # price_now: prefer live LTP from premarket signals if available, else None.
        price_now = None
        try:
            price_now = getattr(premarket, "price_now", None) if premarket else None
        except AttributeError:
            price_now = None
        # If we have price_at_filing but not price_now, degrade to L1-only gracefully.
        if price_at_filing is not None and price_now is None:
            price_at_filing = None  # triggers L2-skipped path in classifier
        tier, fresh_reason = classify_filing_freshness(
            filing_ts=filing_ts,
            as_of=datetime.now(IST),
            price_at_filing=price_at_filing,
            price_now=price_now if price_now is not None else 0.0,
            config=fresh_cfg,
        )
        entry.filing_freshness = tier.value
        notes.append(f"L1/L2: {fresh_reason}")
        if tier == FilingFreshness.STALE:
            failed.append("R4")
        else:
            passed.append("R4")

    # Layer 3: event-quality score (runs regardless of whether L1/L2 were skipped).
    sector_ft = _get_sector_followthrough(pattern_db, category, direction)
    recent_count = _get_recent_category_count(
        entry, category, int(quality_cfg.get("repetition_lookback_days", 5))
    )
    quality_score, quality_breakdown = score_event_quality(
        category=category,
        filing_subject=getattr(entry, "filing_subject", None),
        deal_size_inr=getattr(entry, "deal_size_inr", None),
        market_cap_inr=getattr(entry, "market_cap_inr", None),
        earnings_surprise_pct=getattr(entry, "earnings_surprise_pct", None),
        sector_followthrough=sector_ft,
        recent_same_category_count=recent_count,
        config=quality_cfg,
    )
    entry.event_quality_score = quality_score
    quality_ok, quality_reason = passes_quality_gate(quality_score, quality_cfg)
    notes.append(f"L3: {quality_reason}")
    if not quality_ok:
        # Filing is too weak on fundamentals — fail R4 regardless of freshness
        # path. Remove any prior R4 / R4_SKIPPED pass and record R4 as failed.
        for tag in ("R4", "R4_SKIPPED"):
            if tag in passed:
                passed.remove(tag)
        if "R4" not in failed:
            failed.append("R4")

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

    # ── Phase 4: confidence-threshold demotion ──────────────────────────
    try:
        from src.config_loader import (
            get_hydra_confidence_min, get_router_dawn_confidence_min,
        )
        dawn_min = get_router_dawn_confidence_min()
        hydra_min = get_hydra_confidence_min()
    except Exception:
        dawn_min = 0.85
        hydra_min = 0.0
    if route == "DAWN" and confidence < dawn_min:
        notes.append(
            f"confidence={confidence:.2f}<dawn_confidence_min={dawn_min:.2f} — demoted"
        )
        route = "HYDRA"
    if route == "HYDRA" and confidence < hydra_min:
        notes.append(
            f"confidence={confidence:.2f}<hydra_confidence_min={hydra_min:.2f} — dropped"
        )
        route = "UNROUTED"

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
