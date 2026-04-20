"""
test_exchange_filings.py — deal-size regex + enrichment tests.

Covers:
  1. _parse_deal_size_inr — six subject-line shapes, including USD conversion
     and no-amount fallback.
  2. enrich_filing — populates price_at_filing_time + market_cap_inr from
     pre-market context without clobbering values that are already set.

These run fully offline — no NSE/BSE network calls.
"""
from src.data_ingestion.exchange_filings import (
    FilingEvent,
    _parse_deal_size_inr,
    _USD_INR_APPROX,
    enrich_filing,
)


# ── Deal-size regex: the 6 canonical shapes ──────────────────────────────────

def test_parse_rupee_symbol_crore():
    # "₹500 crore order" → 500 * 1e7 = 5e9 INR
    assert _parse_deal_size_inr("Received order worth ₹500 crore") == 5.0e9


def test_parse_rs_period_cr():
    # "Rs. 200 Cr" → 200 * 1e7 = 2e9 INR
    assert _parse_deal_size_inr("Order awarded Rs. 200 Cr") == 2.0e9


def test_parse_inr_million():
    # "INR 1500 Mn" → 1500 * 1e6 = 1.5e9 INR
    assert _parse_deal_size_inr("Contract value INR 1500 Mn") == 1.5e9


def test_parse_usd_million_with_conversion():
    # "USD 50 million" → 50 * 1e6 * 83 = 4.15e9 INR (approx)
    result = _parse_deal_size_inr("Deal announced: USD 50 million")
    assert result is not None
    assert result == 50 * 1e6 * _USD_INR_APPROX


def test_parse_bare_number_crores():
    # "order worth 300 crores" (no currency prefix) → 300 * 1e7 = 3e9 INR
    assert _parse_deal_size_inr("Company received order worth 300 crores") == 3.0e9


def test_parse_no_amount_returns_none():
    # Subject with no numeric amount → None
    assert _parse_deal_size_inr("Intimation of board meeting") is None


# ── Edge cases that the parser must not trip on ──────────────────────────────

def test_parse_empty_string_returns_none():
    assert _parse_deal_size_inr("") is None
    assert _parse_deal_size_inr(None) is None  # type: ignore[arg-type]


def test_parse_comma_thousands_separator():
    # "Rs. 1,500 crore" → 1500 * 1e7 = 1.5e10 INR
    assert _parse_deal_size_inr("Rs. 1,500 crore deal") == 1.5e10


def test_parse_decimal_amount():
    # "₹2.5 crore" → 2.5 * 1e7 = 2.5e7 INR
    assert _parse_deal_size_inr("Debt raised: ₹2.5 crore") == 2.5e7


def test_parse_dollar_sign_million():
    # "$100 million" → 100 * 1e6 * 83
    result = _parse_deal_size_inr("Order value: $100 million from US client")
    assert result is not None
    assert result == 100 * 1e6 * _USD_INR_APPROX


# ── enrich_filing: populates context-dependent fields ────────────────────────

def _stub_filing(**overrides) -> FilingEvent:
    defaults = dict(
        symbol="ACME",
        company_name="Acme Ltd",
        headline="Acme receives order worth 500 crore",
        category="ORDER_WIN",
        filed_at="2026-04-18T14:30:00+05:30",
        exchange="NSE",
        urgency=9.0,
    )
    defaults.update(overrides)
    return FilingEvent(**defaults)


def test_enrich_populates_price_and_market_cap():
    """Given prev_close + mcap, enrich fills both fields on FilingEvent."""
    f = _stub_filing()
    assert f.price_at_filing_time is None
    assert f.market_cap_inr is None

    enriched = enrich_filing(f, previous_close=825.50, market_cap_inr=4.2e10)

    assert enriched is f  # mutates in place
    assert enriched.price_at_filing_time == 825.50
    assert enriched.market_cap_inr == 4.2e10


def test_enrich_preserves_already_set_fields():
    """enrich_filing must not overwrite values that are already present."""
    f = _stub_filing()
    f.price_at_filing_time = 900.0
    f.market_cap_inr = 1.0e10

    enriched = enrich_filing(f, previous_close=825.50, market_cap_inr=4.2e10)

    assert enriched.price_at_filing_time == 900.0
    assert enriched.market_cap_inr == 1.0e10


def test_enrich_none_inputs_are_noop():
    """Passing None for both inputs leaves the filing untouched."""
    f = _stub_filing()
    enriched = enrich_filing(f, previous_close=None, market_cap_inr=None)
    assert enriched.price_at_filing_time is None
    assert enriched.market_cap_inr is None


def test_filing_event_carries_deal_size_when_set():
    """FilingEvent.deal_size_inr is a first-class field after Step 7a."""
    f = _stub_filing()
    f.deal_size_inr = _parse_deal_size_inr(f.headline)
    assert f.deal_size_inr == 5.0e9
