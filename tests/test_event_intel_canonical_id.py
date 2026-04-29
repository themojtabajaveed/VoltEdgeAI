"""Tests for canonical event_id (Plan B Phase B0).

The canonical id makes the same announcement on NSE+BSE+TrueData hash
to one value so the seen-set collapses cross-source duplicates.
"""

from __future__ import annotations

import pytest

from src.event_intelligence.dedupe import (
    compute_canonical_event_id, compute_event_id,
)


SUBJECT = "Outcome of Board Meeting - Q4 FY26 Result"
SYMBOL = "COHANCE"
EVT_TYPE = "FINANCIAL_RESULTS"
FILED = "2026-04-29T09:49:14+05:30"


def test_canonical_collapses_nse_and_bse():
    nse_id = compute_canonical_event_id(SYMBOL, EVT_TYPE, FILED, SUBJECT)
    bse_id = compute_canonical_event_id(SYMBOL, EVT_TYPE, FILED, SUBJECT)
    assert nse_id == bse_id


def test_canonical_collapses_truedata_and_official():
    """TrueData and Official sources of the same announcement collapse."""
    truedata_id = compute_canonical_event_id(SYMBOL, EVT_TYPE, FILED, SUBJECT)
    official_id = compute_canonical_event_id(SYMBOL, EVT_TYPE, FILED, SUBJECT)
    assert truedata_id == official_id


def test_canonical_differs_from_per_source_id():
    """compute_event_id (per-source) and compute_canonical_event_id (cross-source)
    must produce *different* hashes — they encode different uniqueness."""
    canon = compute_canonical_event_id(SYMBOL, EVT_TYPE, FILED, SUBJECT)
    per_src = compute_event_id(SYMBOL, EVT_TYPE, FILED, source_id="NSE-12345")
    assert canon != per_src


def test_canonical_collapses_corrigendum_to_original():
    """Corrigenda strip noise words and collapse onto the same canonical id."""
    original = compute_canonical_event_id(SYMBOL, EVT_TYPE, FILED, SUBJECT)
    corrigendum = compute_canonical_event_id(
        SYMBOL, EVT_TYPE, FILED,
        f"CORRIGENDUM - {SUBJECT}",
    )
    revised = compute_canonical_event_id(
        SYMBOL, EVT_TYPE, FILED,
        f"REVISED - {SUBJECT}",
    )
    assert original == corrigendum == revised


def test_canonical_differs_across_symbols():
    a = compute_canonical_event_id("RELIANCE", EVT_TYPE, FILED, SUBJECT)
    b = compute_canonical_event_id("TCS", EVT_TYPE, FILED, SUBJECT)
    assert a != b


def test_canonical_differs_across_event_types():
    a = compute_canonical_event_id(SYMBOL, "FINANCIAL_RESULTS", FILED, SUBJECT)
    b = compute_canonical_event_id(SYMBOL, "DIVIDEND_ACTION", FILED, SUBJECT)
    assert a != b


def test_canonical_differs_across_dates():
    a = compute_canonical_event_id(SYMBOL, EVT_TYPE, "2026-04-29T09:49:14+05:30", SUBJECT)
    b = compute_canonical_event_id(SYMBOL, EVT_TYPE, "2026-04-30T09:49:14+05:30", SUBJECT)
    assert a != b


def test_canonical_invariant_within_same_day():
    """Same announcement at different times within the same trading day collapse."""
    a = compute_canonical_event_id(SYMBOL, EVT_TYPE, "2026-04-29T09:49:14+05:30", SUBJECT)
    b = compute_canonical_event_id(SYMBOL, EVT_TYPE, "2026-04-29T15:45:18+05:30", SUBJECT)
    assert a == b


def test_canonical_uppercases_symbol():
    a = compute_canonical_event_id("cohance", EVT_TYPE, FILED, SUBJECT)
    b = compute_canonical_event_id("COHANCE", EVT_TYPE, FILED, SUBJECT)
    assert a == b


def test_canonical_returns_16_char_hex():
    out = compute_canonical_event_id(SYMBOL, EVT_TYPE, FILED, SUBJECT)
    assert len(out) == 16
    int(out, 16)  # must be valid hex


def test_canonical_validates_required_fields():
    with pytest.raises(ValueError):
        compute_canonical_event_id("", EVT_TYPE, FILED, SUBJECT)
    with pytest.raises(ValueError):
        compute_canonical_event_id(SYMBOL, "", FILED, SUBJECT)
    with pytest.raises(ValueError):
        compute_canonical_event_id(SYMBOL, EVT_TYPE, "", SUBJECT)
