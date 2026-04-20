"""
test_conviction_engine.py — apply_filing_metadata_adjustment() tests.

Covers the additive conviction adjustment that runs after the weighted
5-layer sum in ConvictionEngine.tick(). The adjustment reads filing
metadata (pre-packed conviction_modifier + event_quality_score) and
folds them into the final score before threshold check.

Duck-typed — works for both ActiveSignal (metadata dict) and
WatchlistEntry (first-class attrs).
"""
from dataclasses import dataclass, field
from typing import Optional

from src.strategies.base import WatchlistEntry
from src.trading.conviction_engine import (
    ActiveSignal,
    apply_filing_metadata_adjustment,
)

# Default config matching config.yaml conviction.filing_metadata defaults.
_CFG = {
    "high_quality_conviction_bonus": 6,
    "low_quality_conviction_penalty": 15,
    "apply_enabled": True,
}


def _signal(**meta) -> ActiveSignal:
    """Build an ActiveSignal with stub required fields + filing metadata."""
    return ActiveSignal(
        symbol="ACME",
        direction="BUY",
        strategy="HYDRA",
        layer_c_score=60.0,
        event_summary="stub",
        metadata=dict(meta),
    )


# ── Scenario 1: PRISTINE filing + quality 88 → pristine bonus + high-quality bonus

def test_pristine_high_quality_adjustment():
    """conv_mod=+8 (PRISTINE freshness) + quality 88 (+6) → base 70 → 84."""
    sig = _signal(filing_freshness="PRISTINE", conviction_modifier=8, event_quality_score=88.0)
    score, reasons = apply_filing_metadata_adjustment(70.0, sig, _CFG)
    assert score == 84.0
    assert any("PRISTINE" in r for r in reasons)
    assert any("quality=88" in r and "+6" in r for r in reasons)


# ── Scenario 2: FRESH + CONFIRMED + neutral quality 62 → only +14 modifier, no quality bonus

def test_fresh_confirmed_neutral_quality_adjustment():
    """conv_mod=+14 (FRESH+CONFIRMED) + quality 62 (neutral) → base 70 → 84."""
    sig = _signal(filing_freshness="FRESH", conviction_modifier=14, event_quality_score=62.0)
    score, reasons = apply_filing_metadata_adjustment(70.0, sig, _CFG)
    assert score == 84.0
    assert any("FRESH" in r and "+14" in r for r in reasons)
    # Neutral quality band must not appear in reasons
    assert not any("quality=62" in r for r in reasons)


# ── Scenario 3: No filing metadata → no adjustment, no reasons

def test_no_filing_metadata_noop():
    """Signal without filing metadata → score unchanged, reasons empty."""
    sig = _signal()  # empty metadata
    score, reasons = apply_filing_metadata_adjustment(70.0, sig, _CFG)
    assert score == 70.0
    assert reasons == []


# ── Scenario 4: Defensive low-quality penalty (< 55 should never survive scanner,
#                but conviction engine still applies -15 as a belt-and-braces guard)

def test_low_quality_penalty_defensive():
    """quality 40 (< 55) → -15 penalty even if conv_mod is 0."""
    sig = _signal(event_quality_score=40.0, conviction_modifier=0)
    score, reasons = apply_filing_metadata_adjustment(70.0, sig, _CFG)
    assert score == 55.0
    assert any("quality=40" in r and "-15" in r for r in reasons)


# ── Scenario 5: Clamping at 100 — very high base + bonuses must not overshoot

def test_adjustment_clamps_at_100():
    """base 95 + mod +14 + high-quality +6 = 115 → clamp to 100."""
    sig = _signal(filing_freshness="FRESH", conviction_modifier=14, event_quality_score=90.0)
    score, _ = apply_filing_metadata_adjustment(95.0, sig, _CFG)
    assert score == 100.0


# ── Scenario 6: Clamping at 0 — very low base + penalty must not go negative

def test_adjustment_clamps_at_0():
    """base 5 + mod -20 + low-quality -15 = -30 → clamp to 0."""
    sig = _signal(conviction_modifier=-20, event_quality_score=30.0)
    score, _ = apply_filing_metadata_adjustment(5.0, sig, _CFG)
    assert score == 0.0


# ── Scenario 7: apply_enabled=False short-circuits to no-op

def test_apply_enabled_false_short_circuits():
    """When apply_enabled=False, score passes through untouched."""
    sig = _signal(filing_freshness="PRISTINE", conviction_modifier=8, event_quality_score=88.0)
    cfg = dict(_CFG, apply_enabled=False)
    score, reasons = apply_filing_metadata_adjustment(70.0, sig, cfg)
    assert score == 70.0
    assert reasons == []


# ── Scenario 8: Duck-typing — WatchlistEntry (first-class fields) behaves same

def test_watchlist_entry_duck_typed():
    """WatchlistEntry with filing fields set should produce same adjustment."""
    entry = WatchlistEntry(
        symbol="ACME",
        direction="BUY",
        filing_freshness="PRISTINE",
        conviction_modifier=8,
        event_quality_score=88.0,
    )
    score, reasons = apply_filing_metadata_adjustment(70.0, entry, _CFG)
    assert score == 84.0
    assert any("PRISTINE" in r for r in reasons)
    assert any("quality=88" in r and "+6" in r for r in reasons)
