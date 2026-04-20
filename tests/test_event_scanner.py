"""
test_event_scanner.py — apply_event_evaluation_gate() integration tests.

Exercises the 4-layer event-evaluation gate that sits between classify_events()
and injection into HYDRA's watchlist. Each scenario drives the gate directly
with a synthetic MarketEvent and a fixed `now` so market-time math is
deterministic.

Timebase for all tests below:
    as_of = Mon 2026-05-11 11:00 IST  (105 min into the session)
    09:15..11:00 = 105 NSE session minutes available
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.data_ingestion.event_scanner import MarketEvent, apply_event_evaluation_gate

IST = ZoneInfo("Asia/Kolkata")

# Fixed reference moment, deep into the session so we can synthesise any
# freshness tier by walking filing_ts backwards.
_AS_OF = datetime(2026, 5, 11, 11, 0, tzinfo=IST)  # Mon 2026-05-11 11:00 IST


def _evt(symbol: str, category: str, filing_ts: datetime, **kw) -> MarketEvent:
    """Build a MarketEvent with sensible defaults. Overrides via kwargs."""
    defaults = dict(
        headline=f"{category} announcement for {symbol}",
        body="",
        category=category,
        source="NSE_ANNOUNCEMENTS",
        timestamp=filing_ts,
        urgency=9.0,
        direction="BUY",
        event_type=category,
        summary=f"{category} for {symbol}",
        material=True,
    )
    defaults.update(kw)
    return MarketEvent(symbol=symbol, **defaults)


# ── Scenario 1: STALE filing (market_min > 90) ───────────────────────────────

def test_stale_event_discarded_l1():
    """filing_ts = 09:15, as_of = 11:00 → 105 market-min > 90 warm-max → STALE → discard."""
    evt = _evt("STALE_CO", "ORDER_WIN", filing_ts=_AS_OF - timedelta(minutes=105))
    survivors = apply_event_evaluation_gate([evt], seen_headlines=set(), now=_AS_OF)
    assert survivors == []
    # Gate tagged freshness on the event for audit purposes even when discarded
    assert evt.filing_freshness == "STALE"


# ── Scenario 2: Fresh filing but quality < 55 ────────────────────────────────

def test_fresh_but_weak_event_discarded_l3():
    """filing_ts 10:45 → 15 market-min (FRESH), category BOARD_MEETING → quality ~39 < 55.

    L3 rejects. Event must not appear in survivors; scanner logs the discard
    with the L3 reason including the quality score.
    """
    evt = _evt("WEAK_BM", "BOARD_MEETING", filing_ts=_AS_OF - timedelta(minutes=15))
    survivors = apply_event_evaluation_gate([evt], seen_headlines=set(), now=_AS_OF)
    assert survivors == []
    assert evt.filing_freshness == "FRESH"
    assert evt.event_quality_score is not None
    assert evt.event_quality_score < 55.0


# ── Scenario 3: Fresh, strong quality, L4 TOO_EARLY ──────────────────────────

def test_fresh_strong_too_early_injected():
    """filing_ts 10:55 → 5 market-min (FRESH) with strong ORDER_WIN.

    market_min < 15 → L4 TOO_EARLY, which is a pass-through (pristine/fresh
    filings should inject immediately without waiting for tape confirmation).
    """
    evt = _evt("FRESH_WIN", "ORDER_WIN", filing_ts=_AS_OF - timedelta(minutes=5))
    survivors = apply_event_evaluation_gate([evt], seen_headlines=set(), now=_AS_OF)
    assert len(survivors) == 1
    assert survivors[0] is evt
    assert evt.filing_freshness == "FRESH"
    assert evt.event_quality_score is not None and evt.event_quality_score >= 55.0
    assert evt.market_confirmation == "TOO_EARLY"
    # TOO_EARLY contributes 0; FRESH contributes +4 (default bonus)
    assert evt.conviction_modifier == 4


# ── Scenario 4: Fresh, strong, market INDIFFERENT → discard ──────────────────

def test_fresh_strong_but_indifferent_market_discarded_l4():
    """filing_ts 10:40 → 20 market-min (FRESH), quality OK, but tape shows
    no spike and velocity 0.2% < 0.5% threshold → INDIFFERENT → discarded."""
    evt = _evt(
        "QUIET", "ORDER_WIN", filing_ts=_AS_OF - timedelta(minutes=20),
        volume_current_15min=300, volume_avg_15min=400,
        price_velocity_pct=0.2,
    )
    survivors = apply_event_evaluation_gate([evt], seen_headlines=set(), now=_AS_OF)
    assert survivors == []
    assert evt.market_confirmation == "INDIFFERENT"


# ── Scenario 5: Fresh, strong, market CONFIRMED → inject with bonus ──────────

def test_fresh_strong_confirmed_injected_with_bonus():
    """20 market-min, volume 1000 > 400×2 → CONFIRMED via volume alone.

    Conviction modifier = fresh_conviction_bonus (+4) + confirmed_conviction_bonus (+10) = +14.
    """
    evt = _evt(
        "HOT", "ORDER_WIN", filing_ts=_AS_OF - timedelta(minutes=20),
        volume_current_15min=1000, volume_avg_15min=400,
        price_velocity_pct=0.1,
    )
    survivors = apply_event_evaluation_gate([evt], seen_headlines=set(), now=_AS_OF)
    assert len(survivors) == 1
    assert survivors[0] is evt
    assert evt.filing_freshness == "FRESH"
    assert evt.market_confirmation == "CONFIRMED"
    # FRESH (+4) + CONFIRMED (+10) = 14
    assert evt.conviction_modifier == 14


# ── Scenario 6: PRISTINE (market_min = 0), strong, injected ─────────────────

def test_pristine_strong_event_injected_with_pristine_bonus():
    """filing_ts = as_of → 0 market-min → PRISTINE. L4 always TOO_EARLY here.

    Conviction modifier = pristine_conviction_bonus (+8) + 0 (TOO_EARLY) = +8.
    """
    evt = _evt("PRISTINE_WIN", "ORDER_WIN", filing_ts=_AS_OF)
    survivors = apply_event_evaluation_gate([evt], seen_headlines=set(), now=_AS_OF)
    assert len(survivors) == 1
    assert survivors[0] is evt
    assert evt.filing_freshness == "PRISTINE"
    assert evt.market_confirmation == "TOO_EARLY"
    assert evt.event_quality_score is not None and evt.event_quality_score >= 55.0
    # PRISTINE (+8) + TOO_EARLY (0) = 8
    assert evt.conviction_modifier == 8


# ── Bonus: MarketEvent.filed_at_dt property round-trip ───────────────────────

def test_market_event_filed_at_dt_assumes_ist_for_naive_timestamp():
    """Naive datetime → .filed_at_dt should return IST-aware datetime."""
    naive = datetime(2026, 5, 11, 10, 0)  # no tzinfo
    evt = _evt("NAIVE", "ORDER_WIN", filing_ts=naive)
    dt = evt.filed_at_dt
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == IST.utcoffset(dt)
