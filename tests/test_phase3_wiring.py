"""
test_phase3_wiring.py — Phase 3 integration sanity tests.
"""
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

from src.strategies.base import WatchlistEntry
from src.strategies.router import create_hydra_shadow

IST = ZoneInfo("Asia/Kolkata")


def test_watchlist_entry_has_router_fields():
    """WatchlistEntry defaults route/confidence/routed_at."""
    e = WatchlistEntry(symbol="X", direction="BUY")
    assert e.route == ""
    assert e.confidence == 0.0
    assert e.routed_at is None


def test_select_dawn_candidates_empty_router_filter_returns_empty():
    """Router returns 0 DAWN candidates → select returns []."""
    from src.strategies.dawn import select_dawn_candidates
    with patch("src.strategies.dawn.load_hydra_watchlist", return_value=[
        {"symbol": "ABC", "conviction_score": 80, "direction": "BUY"},
        {"symbol": "XYZ", "conviction_score": 75, "direction": "BUY"},
    ]):
        out = select_dawn_candidates(min_conviction=50, router_filter=[])
    assert out == []


def test_select_dawn_candidates_filters_to_routed_symbols():
    """router_filter with one symbol keeps only that symbol in scored output."""
    from src.strategies import dawn as dawn_mod
    allowed = WatchlistEntry(symbol="ABC", direction="BUY")
    with patch.object(dawn_mod, "load_hydra_watchlist", return_value=[
        {"symbol": "ABC", "conviction_score": 80, "direction": "BUY"},
        {"symbol": "XYZ", "conviction_score": 75, "direction": "BUY"},
    ]), patch.object(
        dawn_mod, "score_dawn_conviction",
        return_value=(90, {"total": 90}),
    ), patch(
        "src.data_ingestion.pre_market_data.fetch_all_premarket_data",
        return_value={},
    ), patch(
        "src.data_ingestion.pre_market_data.get_scan_universe",
        return_value=[],
    ):
        out = dawn_mod.select_dawn_candidates(min_conviction=50, router_filter=[allowed])
    syms = {c.get("symbol") for c in out}
    assert syms == {"ABC"}, f"Expected only ABC, got {syms}"


def test_conviction_threshold_is_62():
    """Fix 1: threshold lowered to 62 for dry-run fills (live gate stays at 70 via live_conviction_threshold)."""
    from src.trading import conviction_engine
    assert conviction_engine.CONVICTION_THRESHOLD == 62.0


def test_hydra_shadow_roundtrip_persistence():
    import tempfile
    tmp_path = Path(tempfile.mkdtemp())
    """Shadow payload serializes to JSON and loads back."""
    entry = WatchlistEntry(
        symbol="ZZZ",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        gap_pct=3.0,
        event_summary="test",
    )
    entry.confidence = 0.83
    entry.route = "DAWN"
    entry.routed_at = datetime.now(IST).isoformat()
    shadow = create_hydra_shadow(entry)

    payload = [{
        "symbol": shadow["symbol"],
        "confidence": entry.confidence,
        "route": entry.route,
        "routed_at": entry.routed_at,
        "filing_category": entry.filing_category,
        "filing_urgency": entry.filing_urgency,
        "direction": shadow["direction"],
        "gap_pct": shadow["gap_pct"],
        "event_summary": shadow["event_summary"],
    }]

    path = tmp_path / "hydra_shadows_TEST.json"
    with open(path, "w") as f:
        json.dump(payload, f, default=str)

    with open(path) as f:
        loaded = json.load(f)
    assert len(loaded) == 1
    assert loaded[0]["symbol"] == "ZZZ"
    assert loaded[0]["route"] == "DAWN"
    assert abs(loaded[0]["confidence"] - 0.83) < 1e-6
