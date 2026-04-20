"""
test_event_quality.py — score_event_quality() + passes_quality_gate() tests.

Layer 3 of the R4 evaluation model: scores a filing 0–100 on 5 weighted
components, then gates on min_passing_score.
"""
from src.utils.event_quality import (
    EVENT_TYPE_SCORES,
    classify_market_confirmation,
    passes_quality_gate,
    score_event_quality,
)

# Default config matching config.yaml router.event_quality defaults.
_CFG = {
    "weight_event_type": 0.35,
    "weight_deal_size_ratio": 0.25,
    "weight_earnings_surprise": 0.20,
    "weight_sector_followthrough": 0.10,
    "weight_repetition_penalty": 0.10,
    "repetition_lookback_days": 5,
    "min_passing_score": 55.0,
}


def _score(**kwargs) -> tuple[float, dict]:
    """Convenience wrapper with sensible defaults — override per test."""
    defaults = dict(
        category="UNKNOWN",
        filing_subject=None,
        deal_size_inr=None,
        market_cap_inr=None,
        earnings_surprise_pct=None,
        sector_followthrough=None,
        recent_same_category_count=0,
        config=_CFG,
    )
    defaults.update(kwargs)
    return score_event_quality(**defaults)


# ── Component 1: event type magnitude ────────────────────────────────────────

def test_event_type_merger_raw_is_95():
    _, breakdown = _score(category="MERGER")
    assert breakdown["event_type"]["raw"] == 95


def test_event_type_board_meeting_raw_is_12():
    _, breakdown = _score(category="BOARD_MEETING")
    assert breakdown["event_type"]["raw"] == 12


def test_event_type_unknown_category_defaults_to_40():
    _, breakdown = _score(category="WIDGET_ANNOUNCEMENT")
    assert breakdown["event_type"]["raw"] == 40


def test_event_type_is_case_insensitive():
    _, lower = _score(category="merger")
    _, upper = _score(category="MERGER")
    assert lower["event_type"]["raw"] == upper["event_type"]["raw"] == 95


# ── Component 2: deal/order size ratio ───────────────────────────────────────

def test_deal_ratio_very_large_scores_95():
    # ₹500cr deal / ₹300cr mcap = ratio 1.67 → top tier (95)
    _, breakdown = _score(
        category="ORDER_WIN", deal_size_inr=500e7, market_cap_inr=300e7
    )
    assert breakdown["deal_size_ratio"]["raw"] == 95


def test_deal_ratio_very_small_scores_15():
    # ₹2cr deal / ₹5000cr mcap = 0.0004 → below 0.01 → 15
    _, breakdown = _score(
        category="ORDER_WIN", deal_size_inr=2e7, market_cap_inr=5000e7
    )
    assert breakdown["deal_size_ratio"]["raw"] == 15


def test_deal_ratio_none_deal_is_neutral_40():
    _, breakdown = _score(
        category="ORDER_WIN", deal_size_inr=None, market_cap_inr=1000e7
    )
    assert breakdown["deal_size_ratio"]["raw"] == 40


def test_deal_ratio_invalid_mcap_is_neutral_40():
    _, breakdown = _score(
        category="ORDER_WIN", deal_size_inr=50e7, market_cap_inr=0
    )
    assert breakdown["deal_size_ratio"]["raw"] == 40


# ── Component 3: earnings surprise ───────────────────────────────────────────

def test_earnings_surprise_big_beat_scores_90():
    _, breakdown = _score(category="RESULTS_BEAT", earnings_surprise_pct=45.0)
    assert breakdown["earnings_surprise"]["raw"] == 90


def test_earnings_surprise_big_miss_scores_15():
    _, breakdown = _score(category="RESULTS_MISS", earnings_surprise_pct=-15.0)
    assert breakdown["earnings_surprise"]["raw"] == 15


def test_earnings_surprise_none_for_non_results_is_neutral_50():
    _, breakdown = _score(category="ORDER_WIN", earnings_surprise_pct=None)
    assert breakdown["earnings_surprise"]["raw"] == 50


# ── Component 5: repetition penalty ──────────────────────────────────────────

def test_repetition_first_occurrence_scores_100():
    _, breakdown = _score(recent_same_category_count=0)
    assert breakdown["repetition_penalty"]["raw"] == 100


def test_repetition_spam_count_3_scores_5():
    _, breakdown = _score(recent_same_category_count=3)
    assert breakdown["repetition_penalty"]["raw"] == 5


# ── Full-score integration ───────────────────────────────────────────────────

def test_full_score_order_win_100cr_deal_500cr_mcap_sector_070_fresh():
    """ORDER_WIN + deal 100cr/500cr mcap (ratio 0.20) + no earnings data +
    sector 0.70 + count 0.

    Expected components:
      event_type:          80 * 0.35 = 28.00
      deal_size_ratio:     95 * 0.25 = 23.75  (ratio=0.20 → top tier)
      earnings_surprise:   50 * 0.20 = 10.00  (None → neutral)
      sector_followthrough:70 * 0.10 =  7.00  (0.70 * 100)
      repetition_penalty: 100 * 0.10 = 10.00
      ────────────────────────────────── 78.75
    """
    score, _ = _score(
        category="ORDER_WIN",
        deal_size_inr=100e7,
        market_cap_inr=500e7,
        earnings_surprise_pct=None,
        sector_followthrough=0.70,
        recent_same_category_count=0,
    )
    assert abs(score - 78.75) < 0.5, f"expected ~78.75, got {score}"


def test_full_score_board_meeting_all_neutrals_is_weak():
    """BOARD_MEETING + deal None + surprise None + sector None + count 2.

    Expected components with default-neutral missing data:
      event_type:       12 * 0.35 = 4.20
      deal_size_ratio:  40 * 0.25 = 10.00   (None → neutral 40)
      earnings_surprise:50 * 0.20 = 10.00   (None → neutral 50)
      sector_followth.: 50 * 0.10 =  5.00   (None → neutral 50)
      repetition:       30 * 0.10 =  3.00   (count=2 → 30)
      ──────────────────────────────── 32.20

    The neutral-missing-data defaults (40/50/50) prevent a pure BOARD_MEETING
    from scoring below ~32. It is still comfortably below min_passing_score=55,
    so the quality gate correctly rejects it — which is the actual business outcome.
    """
    score, _ = _score(
        category="BOARD_MEETING",
        deal_size_inr=None,
        market_cap_inr=None,
        earnings_surprise_pct=None,
        sector_followthrough=None,
        recent_same_category_count=2,
    )
    assert score < 35, f"weak filing should score < 35, got {score}"
    passes, _reason = passes_quality_gate(score, _CFG)
    assert passes is False, "BOARD_MEETING at 32.2 must fail the quality gate"


def test_full_score_merger_large_deal_is_strong():
    """MERGER + deal 500cr/300cr mcap + surprise None + sector None + count 0.

    Expected:
      event_type:       95 * 0.35 = 33.25
      deal_size_ratio:  95 * 0.25 = 23.75  (ratio=1.67 → top tier)
      earnings_surprise:50 * 0.20 = 10.00
      sector_followth.: 50 * 0.10 =  5.00
      repetition:      100 * 0.10 = 10.00
      ──────────────────────────────── 82.00
    """
    score, _ = _score(
        category="MERGER",
        deal_size_inr=500e7,
        market_cap_inr=300e7,
        earnings_surprise_pct=None,
        sector_followthrough=None,
        recent_same_category_count=0,
    )
    assert score > 80, f"strong filing should score > 80, got {score}"
    passes, _reason = passes_quality_gate(score, _CFG)
    assert passes is True


# ── Quality gate ─────────────────────────────────────────────────────────────

def test_quality_gate_passes_when_score_at_or_above_minimum():
    passes, reason = passes_quality_gate(60.0, _CFG)
    assert passes is True
    assert reason, "reason must be non-empty"


def test_quality_gate_fails_when_score_below_minimum():
    passes, reason = passes_quality_gate(48.0, _CFG)
    assert passes is False
    assert reason, "reason must be non-empty"
    assert "weak filing" in reason


# ── Breakdown dict structure ─────────────────────────────────────────────────

def test_breakdown_contains_all_five_components():
    _, breakdown = _score(category="ORDER_WIN")
    expected = {
        "event_type",
        "deal_size_ratio",
        "earnings_surprise",
        "sector_followthrough",
        "repetition_penalty",
    }
    assert set(breakdown.keys()) == expected


def test_breakdown_contributions_sum_to_total_score():
    """Sum of per-component contributions must equal the rounded total (±0.5 for
    rounding noise between raw-float total and 1-decimal rounded score)."""
    score, breakdown = _score(
        category="ORDER_WIN",
        deal_size_inr=100e7,
        market_cap_inr=500e7,
        sector_followthrough=0.70,
        recent_same_category_count=0,
    )
    contribution_sum = sum(c["contribution"] for c in breakdown.values())
    assert abs(contribution_sum - score) < 0.5, (
        f"contributions sum {contribution_sum} != score {score}"
    )


def test_breakdown_each_component_has_raw_weight_contribution_keys():
    _, breakdown = _score(category="MERGER")
    for name, comp in breakdown.items():
        assert {"raw", "weight", "contribution"} == set(comp.keys()), (
            f"{name} breakdown missing expected keys: {comp}"
        )


# ── Event-type table sanity ──────────────────────────────────────────────────

def test_event_type_table_has_unknown_fallback():
    assert "UNKNOWN" in EVENT_TYPE_SCORES
    assert EVENT_TYPE_SCORES["UNKNOWN"] == 40


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 4 — classify_market_confirmation() tests
# ═══════════════════════════════════════════════════════════════════════════════

_L4_CFG = {
    "min_market_minutes_before_check": 15,
    "volume_spike_multiplier": 2.0,
    "min_price_velocity_pct": 0.5,
}


def test_l4_too_early_under_min_market_minutes():
    """market_min=10 < 15 → TOO_EARLY regardless of volume/velocity."""
    status, reason = classify_market_confirmation(
        market_minutes_elapsed=10,
        volume_current_15min=9999,
        volume_avg_15min=10,
        price_velocity_pct=5.0,
        config=_L4_CFG,
    )
    assert status == "TOO_EARLY"
    assert "10" in reason and "15" in reason


def test_l4_no_data_when_both_feeds_missing():
    """market_min ≥ 15 but both volume and velocity are None → NO_DATA."""
    status, reason = classify_market_confirmation(
        market_minutes_elapsed=20,
        volume_current_15min=None,
        volume_avg_15min=None,
        price_velocity_pct=None,
        config=_L4_CFG,
    )
    assert status == "NO_DATA"
    assert "unavailable" in reason.lower() or "skip" in reason.lower()


def test_l4_confirmed_by_volume_spike_alone():
    """volume_current=1000 > volume_avg=400 × 2.0 = 800 → CONFIRMED."""
    status, reason = classify_market_confirmation(
        market_minutes_elapsed=20,
        volume_current_15min=1000,
        volume_avg_15min=400,
        price_velocity_pct=None,
        config=_L4_CFG,
    )
    assert status == "CONFIRMED"
    assert "spike" in reason.lower()


def test_l4_indifferent_no_spike_no_velocity():
    """volume 300 (no spike) AND velocity 0.3% < 0.5% threshold → INDIFFERENT."""
    status, reason = classify_market_confirmation(
        market_minutes_elapsed=20,
        volume_current_15min=300,
        volume_avg_15min=400,
        price_velocity_pct=0.3,
        config=_L4_CFG,
    )
    assert status == "INDIFFERENT"
    assert "no volume spike" in reason.lower()
    assert "20" in reason


def test_l4_confirmed_by_velocity_alone():
    """volume 300 (no spike) but velocity 0.8% ≥ 0.5% → CONFIRMED via velocity."""
    status, reason = classify_market_confirmation(
        market_minutes_elapsed=20,
        volume_current_15min=300,
        volume_avg_15min=400,
        price_velocity_pct=0.8,
        config=_L4_CFG,
    )
    assert status == "CONFIRMED"
    assert "velocity" in reason.lower()


def test_l4_confirmed_by_volume_alone_weak_velocity():
    """volume 900 > 400×2 = 800 → spike, velocity 0.2% < 0.5% → CONFIRMED via volume."""
    status, reason = classify_market_confirmation(
        market_minutes_elapsed=20,
        volume_current_15min=900,
        volume_avg_15min=400,
        price_velocity_pct=0.2,
        config=_L4_CFG,
    )
    assert status == "CONFIRMED"
    assert "spike" in reason.lower()
