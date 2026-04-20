"""
test_router.py — DawnHydraRouter rule validation (Phase 2 retrofit).
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.strategies.base import WatchlistEntry
from src.strategies.router import route_candidate
from src.data_ingestion.pre_market_data import PreMarketSignals

IST = ZoneInfo("Asia/Kolkata")


def _make_premarket(**overrides) -> PreMarketSignals:
    base = dict(
        symbol="TEST",
        as_of=datetime.now(IST),
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        filing_impact_score=8.5,
        gap_pct=3.0,
        avg_volume_20d=1_000_000,
    )
    base.update(overrides)
    try:
        return PreMarketSignals(**base)
    except TypeError:
        pm = PreMarketSignals(symbol=base["symbol"])
        for k, v in base.items():
            setattr(pm, k, v)
        return pm


def test_railtel_routes_to_dawn_cold_start():
    """Top-tier catalyst with no history → DAWN via R5_COLD_START."""
    entry = WatchlistEntry(
        symbol="RAILTEL",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=datetime.now(IST).isoformat(),
        gap_pct=3.5,
    )
    pm = _make_premarket(symbol="RAILTEL")
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "DAWN", decision.reasoning
    assert "R5_COLD_START" in decision.rules_passed
    assert decision.confidence == 1.0


def test_somestock_missing_category_routes_to_hydra():
    """No DAWN category → R2 fails → HYDRA."""
    entry = WatchlistEntry(
        symbol="SOMESTOCK",
        direction="BUY",
        filing_category="PRESS_RELEASE",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=datetime.now(IST).isoformat(),
        gap_pct=3.0,
    )
    pm = _make_premarket(symbol="SOMESTOCK", filing_category="PRESS_RELEASE")
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "HYDRA"
    assert "R2" in decision.rules_failed


def test_stale_filing_routes_to_hydra():
    """Filing filed 30 days ago → far beyond warm_market_minutes_max=90 →
    freshness STALE → R4 fails → HYDRA.

    Using days instead of hours makes this independent of which weekday the
    test runs on. ~22 trading days × 375 session min/day >> 90 min threshold,
    so the filing is deterministically STALE regardless of NSE holidays."""
    old = (datetime.now(IST) - timedelta(days=30)).isoformat()
    entry = WatchlistEntry(
        symbol="OLD",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=old,
        gap_pct=3.0,
    )
    pm = _make_premarket(symbol="OLD")
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "HYDRA"
    assert "R4" in decision.rules_failed


def test_small_gap_routes_to_hydra():
    entry = WatchlistEntry(
        symbol="NOGAP",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=datetime.now(IST).isoformat(),
        gap_pct=0.5,
    )
    pm = _make_premarket(symbol="NOGAP", gap_pct=0.5)
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "HYDRA"
    assert "R3" in decision.rules_failed


def test_illiquid_routes_to_hydra():
    entry = WatchlistEntry(
        symbol="THIN",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=50_000,
        filed_at=datetime.now(IST).isoformat(),
        gap_pct=3.0,
    )
    pm = _make_premarket(symbol="THIN", avg_volume_20d=50_000)
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "HYDRA"
    assert "R6" in decision.rules_failed


def test_router_stamps_metadata_on_entry():
    entry = WatchlistEntry(
        symbol="META",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=datetime.now(IST).isoformat(),
        gap_pct=3.0,
    )
    pm = _make_premarket(symbol="META")
    route_candidate(entry, pm, pattern_db=None)
    assert entry.route in ("DAWN", "HYDRA")
    assert 0.0 <= entry.confidence <= 1.0
    assert entry.routed_at is not None
