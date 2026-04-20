"""
test_r4_freshness.py — market_minutes_of_exposure() and classify_filing_freshness() tests.

NSE session: 09:15–15:30 IST (375 min/day), Mon–Fri, excluding holidays.

Date windows used:
  May 2026  (2026-05-04..08, 2026-05-11) — no NSE holidays in this range.
  Dec 2026  (2026-12-24..28)             — 2026-12-25 Christmas = NSE holiday.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.utils.market_calendar import market_minutes_of_exposure
from src.utils.filing_freshness import FilingFreshness, classify_filing_freshness

# Default freshness config matching config.yaml router.filing_freshness defaults
_FRESHNESS_CFG = {
    "fresh_market_minutes_max": 30,
    "warm_market_minutes_max": 90,
    "price_reaction_fresh_pct": 1.0,
    "price_reaction_warm_pct": 2.0,
}

IST = ZoneInfo("Asia/Kolkata")


def _ist(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=IST)


# ── 5 specified cases ─────────────────────────────────────────────────────────

def test_friday_after_close_to_monday_open_zero():
    """Friday 15:35 filing → Monday 09:15 = 0 market minutes (weekend gap)."""
    filing = _ist(2026, 5, 8, 15, 35)   # Fri 2026-05-08 15:35 IST
    as_of  = _ist(2026, 5, 11, 9, 15)   # Mon 2026-05-11 09:15 IST
    assert market_minutes_of_exposure(filing, as_of) == 0


def test_thursday_after_close_friday_holiday_to_monday_zero():
    """Thursday 15:35 (Friday = Christmas holiday) → Monday 09:15 = 0.

    2026-12-24 Thu (working day), 2026-12-25 Fri (NSE holiday: Christmas),
    2026-12-28 Mon. Stock had zero market minutes to react despite 65 wall-clock
    hours — the classic failure mode of the wall-clock R4 check.
    """
    filing = _ist(2026, 12, 24, 15, 35)  # Thu 2026-12-24 15:35 IST
    as_of  = _ist(2026, 12, 28, 9, 15)   # Mon 2026-12-28 09:15 IST
    assert market_minutes_of_exposure(filing, as_of) == 0


def test_pre_open_filing_clamped_to_session_start():
    """Monday 09:00 filing → Monday 10:30 = 75 min.

    Pre-open window (09:00–09:15) is excluded: effective_start = max(09:00, 09:15) = 09:15.
    """
    filing = _ist(2026, 5, 11, 9, 0)    # Mon 2026-05-11 09:00 IST
    as_of  = _ist(2026, 5, 11, 10, 30)  # Mon 2026-05-11 10:30 IST
    assert market_minutes_of_exposure(filing, as_of) == 75


def test_within_session_same_day():
    """Friday 14:00 → Friday 15:30 = 90 min (pure intraday, no clamping)."""
    filing = _ist(2026, 5, 8, 14, 0)    # Fri 2026-05-08 14:00 IST
    as_of  = _ist(2026, 5, 8, 15, 30)   # Fri 2026-05-08 15:30 IST
    assert market_minutes_of_exposure(filing, as_of) == 90


def test_friday_post_close_to_monday_open_zero():
    """Friday 16:00 (after close) → Monday 09:15 = 0 market minutes."""
    filing = _ist(2026, 5, 8, 16, 0)    # Fri 2026-05-08 16:00 IST
    as_of  = _ist(2026, 5, 11, 9, 15)   # Mon 2026-05-11 09:15 IST
    assert market_minutes_of_exposure(filing, as_of) == 0


# ── 4 robustness edge cases ───────────────────────────────────────────────────

def test_same_timestamp_returns_zero():
    """Identical filing_ts and as_of → 0."""
    ts = _ist(2026, 5, 11, 10, 0)
    assert market_minutes_of_exposure(ts, ts) == 0


def test_inverted_timestamps_returns_zero():
    """as_of < filing_ts → 0, no exception."""
    filing = _ist(2026, 5, 11, 11, 0)
    as_of  = _ist(2026, 5, 11, 10, 0)
    assert market_minutes_of_exposure(filing, as_of) == 0


def test_sub_minute_gap_returns_zero():
    """30-second gap within session → 0 (floor division of 30s = 0 min)."""
    filing = _ist(2026, 5, 11, 9, 15, 0)
    as_of  = filing + timedelta(seconds=30)
    assert market_minutes_of_exposure(filing, as_of) == 0


def test_full_trading_week_no_holidays():
    """Mon 09:15 → Fri 15:30 across a clean week = 1875 min (5 × 375 min/day).

    May 4–8 2026 has no NSE holidays — all 5 sessions run 09:15–15:30 = 375 min each.
    """
    filing = _ist(2026, 5, 4, 9, 15)   # Mon 2026-05-04 09:15 IST
    as_of  = _ist(2026, 5, 8, 15, 30)  # Fri 2026-05-08 15:30 IST
    assert market_minutes_of_exposure(filing, as_of) == 1875


# ── Test 10: naive datetime auto-IST assumption ───────────────────────────────

def test_naive_datetime_assumed_ist_with_debug_log(caplog):
    """Naive datetimes (no tzinfo) are assumed IST: correct result returned
    and a debug log '[market_calendar] naive datetime assumed IST: ...' is emitted
    for each naive input.
    """
    filing_naive = datetime(2026, 5, 11, 9, 0, 0)   # Mon 09:00, no tzinfo
    as_of_naive  = datetime(2026, 5, 11, 10, 30, 0)  # Mon 10:30, no tzinfo
    with caplog.at_level(logging.DEBUG, logger="src.utils.market_calendar"):
        result = market_minutes_of_exposure(filing_naive, as_of_naive)
    assert result == 75
    naive_warnings = [r for r in caplog.records if "naive datetime assumed IST" in r.message]
    assert len(naive_warnings) == 2, (
        f"Expected 2 naive-IST debug logs (one per input), got {len(naive_warnings)}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# classify_filing_freshness() — Layer 1 + Layer 2 tests
# ═══════════════════════════════════════════════════════════════════════════════
#
# Timestamps for clean market-minute counts (Mon 2026-05-11, no holidays nearby):
#   0 min:   Fri 2026-05-08 15:35 → Mon 2026-05-11 09:15
#   15 min:  Mon 2026-05-11 09:15 → Mon 2026-05-11 09:30
#   60 min:  Mon 2026-05-11 09:15 → Mon 2026-05-11 10:15
#   120 min: Mon 2026-05-11 09:15 → Mon 2026-05-11 11:15


# ── Layer 1 only (price_at_filing=None, L2 skipped) ──────────────────────────

def test_classify_pristine_zero_market_min():
    """0 market-min (weekend gap) → PRISTINE. Reason must mention 0 market-min."""
    filing = _ist(2026, 5, 8, 15, 35)
    as_of  = _ist(2026, 5, 11, 9, 15)
    tier, reason = classify_filing_freshness(filing, as_of, None, 100.0, _FRESHNESS_CFG)
    assert tier == FilingFreshness.PRISTINE
    assert reason, "reason must be non-empty"
    assert "0 market-min" in reason


def test_classify_fresh_15min_no_price():
    """15 market-min, no price data → FRESH. Reason mentions L2 skipped."""
    filing = _ist(2026, 5, 11, 9, 15)
    as_of  = _ist(2026, 5, 11, 9, 30)
    tier, reason = classify_filing_freshness(filing, as_of, None, 100.0, _FRESHNESS_CFG)
    assert tier == FilingFreshness.FRESH
    assert reason, "reason must be non-empty"
    assert "L2 skipped" in reason


def test_classify_warm_60min_no_price():
    """60 market-min, no price data → WARM. Reason mentions L2 skipped."""
    filing = _ist(2026, 5, 11, 9, 15)
    as_of  = _ist(2026, 5, 11, 10, 15)
    tier, reason = classify_filing_freshness(filing, as_of, None, 100.0, _FRESHNESS_CFG)
    assert tier == FilingFreshness.WARM
    assert reason, "reason must be non-empty"
    assert "L2 skipped" in reason


def test_classify_stale_120min_l1():
    """120 market-min > 90 threshold → STALE from Layer 1. Reason mentions L1."""
    filing = _ist(2026, 5, 11, 9, 15)
    as_of  = _ist(2026, 5, 11, 11, 15)
    tier, reason = classify_filing_freshness(filing, as_of, None, 100.0, _FRESHNESS_CFG)
    assert tier == FilingFreshness.STALE
    assert reason, "reason must be non-empty"
    assert "(L1)" in reason


# ── Layer 2 override ─────────────────────────────────────────────────────────

def test_classify_fresh_reaction_below_threshold_stays_fresh():
    """15 market-min, reaction 0.5% < 1.0% threshold → stays FRESH."""
    filing = _ist(2026, 5, 11, 9, 15)
    as_of  = _ist(2026, 5, 11, 9, 30)
    tier, reason = classify_filing_freshness(filing, as_of, 100.0, 100.5, _FRESHNESS_CFG)
    assert tier == FilingFreshness.FRESH
    assert reason
    assert "0.5%" in reason


def test_classify_fresh_reaction_above_threshold_downgrades_to_stale():
    """15 market-min, reaction 1.5% > 1.0% threshold → STALE from Layer 2."""
    filing = _ist(2026, 5, 11, 9, 15)
    as_of  = _ist(2026, 5, 11, 9, 30)
    tier, reason = classify_filing_freshness(filing, as_of, 100.0, 101.5, _FRESHNESS_CFG)
    assert tier == FilingFreshness.STALE
    assert reason
    assert "(L2)" in reason
    assert "1.5%" in reason


def test_classify_warm_reaction_below_threshold_stays_warm():
    """60 market-min, reaction 1.8% < 2.0% threshold → stays WARM."""
    filing = _ist(2026, 5, 11, 9, 15)
    as_of  = _ist(2026, 5, 11, 10, 15)
    tier, reason = classify_filing_freshness(filing, as_of, 100.0, 101.8, _FRESHNESS_CFG)
    assert tier == FilingFreshness.WARM
    assert reason
    assert "1.8%" in reason


def test_classify_warm_reaction_above_threshold_downgrades_to_stale():
    """60 market-min, reaction 2.5% > 2.0% threshold → STALE from Layer 2."""
    filing = _ist(2026, 5, 11, 9, 15)
    as_of  = _ist(2026, 5, 11, 10, 15)
    tier, reason = classify_filing_freshness(filing, as_of, 100.0, 102.5, _FRESHNESS_CFG)
    assert tier == FilingFreshness.STALE
    assert reason
    assert "(L2)" in reason
    assert "2.5%" in reason


# ── PRISTINE immunity ─────────────────────────────────────────────────────────

def test_classify_pristine_immune_to_large_price_reaction():
    """0 market-min + 6% gap → still PRISTINE (Friday earnings → Monday gap-up scenario).

    Layer 2 must NOT override PRISTINE: the gap IS the expected behaviour for a
    zero-exposure filing. Downgrading here would punish exactly the filings we want.
    Reason must mention 'L2 immunity'.
    """
    filing = _ist(2026, 5, 8, 15, 35)   # Fri post-close
    as_of  = _ist(2026, 5, 11, 9, 15)   # Mon open — 0 market-min elapsed
    tier, reason = classify_filing_freshness(filing, as_of, 100.0, 106.0, _FRESHNESS_CFG)
    assert tier == FilingFreshness.PRISTINE
    assert reason
    assert "L2 immunity" in reason
    assert "6.0%" in reason


# ═══════════════════════════════════════════════════════════════════════════════
# Router integration — new R4 block wired end-to-end in route_candidate()
# ═══════════════════════════════════════════════════════════════════════════════
# These tests confirm the 4-layer evaluation replaces the old wall-clock R4
# inside src/strategies/router.py — not just the classifier in isolation.

from datetime import timedelta

from src.data_ingestion.pre_market_data import PreMarketSignals
from src.strategies.base import WatchlistEntry
from src.strategies.router import route_candidate


def _router_premarket(**overrides) -> PreMarketSignals:
    """Build a PreMarketSignals with strong defaults (mirrors tests/test_router.py)."""
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


def test_router_r4_pristine_strong_event_routes_to_dawn():
    """filed_at=now + ORDER_WIN → PRISTINE + quality≈63 ≥ 55 → R4 passes → DAWN."""
    entry = WatchlistEntry(
        symbol="FRESH_WIN",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=datetime.now(IST).isoformat(),
        gap_pct=3.5,
    )
    pm = _router_premarket(symbol="FRESH_WIN")
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "DAWN", decision.reasoning
    assert "R4" in decision.rules_passed
    assert "R4" not in decision.rules_failed
    # Stamped on entry
    assert entry.filing_freshness == "PRISTINE"
    assert entry.event_quality_score is not None
    assert entry.event_quality_score >= 55.0


def test_router_r4_weak_event_fails_even_when_pristine():
    """BOARD_MEETING filed just now → PRISTINE (fresh) BUT quality≈32 < 55 →
    R4 fails on L3 → HYDRA. Proves fundamentals gate fires even for fresh filings."""
    entry = WatchlistEntry(
        symbol="WEAK_BM",
        direction="BUY",
        filing_category="BOARD_MEETING",
        filing_urgency=9.0,   # high urgency to isolate R4 as the failure
        avg_volume_20d=1_000_000,
        filed_at=datetime.now(IST).isoformat(),
        gap_pct=3.0,
    )
    pm = _router_premarket(symbol="WEAK_BM", filing_category="BOARD_MEETING")
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "HYDRA", decision.reasoning
    assert "R4" in decision.rules_failed
    # Freshness tier was computed — filing_freshness is populated,
    # event_quality_score is below the gate.
    assert entry.filing_freshness == "PRISTINE"
    assert entry.event_quality_score is not None
    assert entry.event_quality_score < 55.0


def test_router_r4_filed_at_unknown_skipped_with_ok_quality():
    """filed_at=None but strong ORDER_WIN quality → R4_SKIPPED passes, entry
    still routes to DAWN when all other rules clear. filing_freshness remains None."""
    entry = WatchlistEntry(
        symbol="NOTS",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=None,           # unknown timestamp → L1/L2 skipped
        gap_pct=3.0,
    )
    pm = _router_premarket(symbol="NOTS")
    decision = route_candidate(entry, pm, pattern_db=None)
    # Either DAWN (quality passed + other rules green) or HYDRA (never stale by L1)
    # — the key assertion is R4_SKIPPED appears, not R4 in failed.
    assert "R4_SKIPPED" in decision.rules_passed
    assert "R4" not in decision.rules_failed
    assert entry.filing_freshness is None  # freshness never evaluated
    assert entry.event_quality_score is not None  # L3 quality still scored


def test_router_r4_stale_filing_fails_and_stamps_stale_on_entry():
    """Filing 30 days old → freshness STALE → R4 fails → HYDRA.
    Confirms entry.filing_freshness is populated with 'STALE' for observability."""
    old = (datetime.now(IST) - timedelta(days=30)).isoformat()
    entry = WatchlistEntry(
        symbol="DEAD",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=old,
        gap_pct=3.0,
    )
    pm = _router_premarket(symbol="DEAD")
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "HYDRA"
    assert "R4" in decision.rules_failed
    assert entry.filing_freshness == "STALE"
