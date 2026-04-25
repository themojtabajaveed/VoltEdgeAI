"""Tests for FilingArchiver — Event Study Step 1."""

import os
import sqlite3
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src.event_study.filing_archiver import FilingArchiver
from src.data_ingestion.exchange_filings import FilingEvent

IST = ZoneInfo("Asia/Kolkata")


def _make_filing(
    symbol: str = "INFY",
    headline: str = "Order win worth Rs. 500 crore",
    filed_at: str = "2026-04-20T10:00:00+05:30",
) -> FilingEvent:
    return FilingEvent(
        symbol=symbol,
        company_name="Infosys Ltd",
        headline=headline,
        category="ORDER_WIN",
        filed_at=filed_at,
        exchange="NSE",
        urgency=9.0,
        deal_size_inr=5_000_000_000.0,
    )


# ── Test 1: schema ────────────────────────────────────────────────────────────

def test_schema_created():
    archiver = FilingArchiver(":memory:")
    conn = archiver._conn

    # All 17 columns present
    cur = conn.execute("PRAGMA table_info(filings_archive)")
    cols = {row[1] for row in cur.fetchall()}
    expected = {
        "id", "symbol", "company_name", "exchange", "headline", "headline_hash",
        "category", "filed_at", "urgency_score", "event_type", "event_direction",
        "event_summary", "deal_size_inr", "quality_score", "freshness_tag",
        "processed", "created_at",
    }
    assert expected == cols, f"Missing columns: {expected - cols}"

    # 3 indexes present
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='filings_archive'"
    )
    indexes = {row[0] for row in cur.fetchall()}
    assert "idx_filings_filed_at" in indexes
    assert "idx_filings_symbol" in indexes
    assert "idx_filings_processed" in indexes


# ── Test 2: deduplication ─────────────────────────────────────────────────────

def test_dedup_same_filing():
    archiver = FilingArchiver(":memory:")
    filing = _make_filing()
    clf = {
        "urgency": 9,
        "direction": "BUY",
        "event_type": "ORDER_WIN",
        "summary": "Large order win",
        "material": True,
    }

    mock_freshness = MagicMock()
    mock_freshness.value = "PRISTINE"

    with patch("src.utils.event_quality.score_event_quality", return_value=(75.0, {})), \
         patch("src.utils.filing_freshness.classify_filing_freshness",
               return_value=(mock_freshness, "ok")), \
         patch("src.config_loader.get_event_quality_config", return_value={}), \
         patch("src.config_loader.get_filing_freshness_config", return_value={}):

        first = archiver._store_batch([filing], [clf])
        second = archiver._store_batch([filing], [clf])

    assert first == 1, f"Expected 1 insertion, got {first}"
    assert second == 0, f"Expected 0 on duplicate, got {second}"

    cur = archiver._conn.execute("SELECT COUNT(*) FROM filings_archive")
    assert cur.fetchone()[0] == 1


# ── Test 3: incremental_update uses correct hours_back ───────────────────────

def test_incremental_update_detects_last_filing():
    archiver = FilingArchiver(":memory:")

    # Seed one row so MAX(filed_at) is non-NULL
    archiver._conn.execute("""
        INSERT INTO filings_archive (symbol, headline, headline_hash, filed_at)
        VALUES ('INFY', 'Old filing', 'Old filing', '2026-04-20T10:00:00+05:30')
    """)
    archiver._conn.commit()

    captured: list[int] = []

    def _mock_fetch(hours_back: int):
        captured.append(hours_back)
        return []

    with patch("src.data_ingestion.exchange_filings.fetch_exchange_filings", _mock_fetch):
        archiver.incremental_update()

    assert len(captured) == 1, "fetch_exchange_filings should be called exactly once"
    assert isinstance(captured[0], int)
    assert captured[0] >= 1


# ── Test 4: _classify_batch returns expected keys ────────────────────────────

def test_classify_batch_structure():
    archiver = FilingArchiver(":memory:")
    filing = _make_filing()

    groq_result = [
        {
            "symbol": "INFY",
            "headline": filing.headline,
            "category": "ORDER_WIN",
            "body": "",
            "urgency": 9,
            "direction": "BUY",
            "event_type": "ORDER_WIN",
            "summary": "Major order win announced",
            "material": True,
        }
    ]

    with patch("src.llm.groq_client.classify_events_batch", return_value=groq_result):
        results = archiver._classify_batch([filing])

    assert len(results) == 1
    result = results[0]
    assert "event_type" in result
    assert "direction" in result
    assert "summary" in result


# ── Test 5: integration — real NSE call ──────────────────────────────────────

@pytest.mark.integration
def test_backfill_integration():
    db_path = "/tmp/test_filings.db"
    try:
        archiver = FilingArchiver(db_path=db_path)
        count = archiver.backfill()
        assert count >= 0

        # No duplicate (symbol, filed_at, headline_hash) rows
        cur = archiver._conn.execute("""
            SELECT symbol, filed_at, headline_hash, COUNT(*) AS cnt
            FROM filings_archive
            GROUP BY symbol, filed_at, headline_hash
            HAVING cnt > 1
        """)
        duplicates = cur.fetchall()
        assert duplicates == [], f"Found duplicate rows: {duplicates}"
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


# ── Step 10 tests: priority_categories filter ─────────────────────────────────

def _make_filing_with_category(category: str, symbol: str = "INFY") -> FilingEvent:
    return FilingEvent(
        symbol=symbol,
        company_name=f"{symbol} Ltd",
        headline=f"{category} announcement",
        category=category,
        filed_at="2026-04-20T10:00:00+05:30",
        exchange="NSE",
        urgency=5.0,
        deal_size_inr=None,
    )


class TestPriorityCategoriesFilter:
    """Group 9 — Step 10: _classify_batch sends only priority filings to Groq."""

    def _make_archiver_with_priorities(self, priorities: list) -> FilingArchiver:
        archiver = FilingArchiver(":memory:")
        archiver._priority_categories = [p.lower() for p in priorities]
        return archiver

    def test_priority_filings_sent_to_groq(self):
        """Filings matching priority_categories are sent to Groq."""
        archiver = self._make_archiver_with_priorities(["orders", "financial results"])
        priority_filing = _make_filing_with_category("Orders")
        non_priority = _make_filing_with_category("Trading Window")

        groq_return = [{
            "symbol": "INFY", "headline": "Orders announcement",
            "category": "Orders", "body": "",
            "urgency": 8, "direction": "BUY", "event_type": "ORDER_WIN",
            "summary": "Order win", "material": True,
        }]

        with patch("src.llm.groq_client.classify_events_batch",
                   return_value=groq_return) as mock_groq:
            results = archiver._classify_batch([priority_filing, non_priority])

        # Groq called exactly once (only 1 priority filing)
        assert mock_groq.call_count == 1
        groq_payload = mock_groq.call_args[0][0]
        assert len(groq_payload) == 1
        assert groq_payload[0]["category"] == "Orders"

    def test_non_priority_filings_get_default_classification(self):
        """Non-priority filings receive low-confidence default (no Groq call)."""
        archiver = self._make_archiver_with_priorities(["orders"])
        filings = [
            _make_filing_with_category("Trading Window"),
            _make_filing_with_category("Shareholding"),
            _make_filing_with_category("Regulation 30"),
        ]

        with patch("src.llm.groq_client.classify_events_batch",
                   return_value=[]) as mock_groq:
            results = archiver._classify_batch(filings)

        # Groq not called — all filings are non-priority
        assert mock_groq.call_count == 0
        assert len(results) == 3
        for r in results:
            # Default classification marker
            assert r.get("event_type") == "Other"
            assert r.get("direction") == "NEUTRAL"
            assert r.get("llm_label_confidence") == 0.0

    def test_exact_groq_call_count_on_mixed_batch(self):
        """Batch of 2 priority + 3 non-priority → Groq called with exactly 2 items."""
        archiver = self._make_archiver_with_priorities(["orders", "financial results"])
        filings = [
            _make_filing_with_category("Orders", "RELIANCE"),
            _make_filing_with_category("Trading Window", "TCS"),
            _make_filing_with_category("Financial Results", "INFY"),
            _make_filing_with_category("Shareholding", "WIPRO"),
            _make_filing_with_category("Allotment", "HDFCBANK"),
        ]

        groq_return = [
            {"symbol": "RELIANCE", "headline": "Orders announcement", "category": "Orders",
             "body": "", "urgency": 8, "direction": "BUY", "event_type": "ORDER_WIN",
             "summary": "Order win", "material": True},
            {"symbol": "INFY", "headline": "Financial Results announcement",
             "category": "Financial Results", "body": "",
             "urgency": 7, "direction": "BUY", "event_type": "EARNINGS",
             "summary": "Strong earnings", "material": True},
        ]

        with patch("src.llm.groq_client.classify_events_batch",
                   return_value=groq_return) as mock_groq:
            results = archiver._classify_batch(filings)

        assert mock_groq.call_count == 1
        assert len(mock_groq.call_args[0][0]) == 2, (
            "Groq payload must contain exactly 2 priority filings"
        )
        assert len(results) == 5

        # Priority results land at the right original indices (0=Orders, 2=FinResults)
        assert results[0].get("event_type") == "ORDER_WIN"
        assert results[2].get("event_type") == "EARNINGS"
        # Non-priority results use default
        assert results[1].get("event_type") == "Other"
        assert results[3].get("event_type") == "Other"
        assert results[4].get("event_type") == "Other"

    def test_empty_priority_categories_sends_all_to_groq(self):
        """When priority_categories is empty, all filings are treated as priority."""
        archiver = self._make_archiver_with_priorities([])
        filings = [
            _make_filing_with_category("Trading Window"),
            _make_filing_with_category("Shareholding"),
        ]
        groq_return = [
            {"symbol": "INFY", "headline": "t", "category": "Trading Window", "body": "",
             "urgency": 1, "direction": "NEUTRAL", "event_type": "GENERAL",
             "summary": "", "material": False},
            {"symbol": "INFY", "headline": "t", "category": "Shareholding", "body": "",
             "urgency": 1, "direction": "NEUTRAL", "event_type": "SHAREHOLDING",
             "summary": "", "material": False},
        ]
        with patch("src.llm.groq_client.classify_events_batch",
                   return_value=groq_return) as mock_groq:
            results = archiver._classify_batch(filings)

        assert mock_groq.call_count == 1
        assert len(mock_groq.call_args[0][0]) == 2

    def test_is_priority_case_insensitive(self):
        """Category matching is case-insensitive substring check."""
        archiver = self._make_archiver_with_priorities(["board meeting"])
        f_lower  = _make_filing_with_category("board meeting outcome")
        f_upper  = _make_filing_with_category("BOARD MEETING - Results")
        f_no_match = _make_filing_with_category("Trading Window")

        assert archiver._is_priority(f_lower) is True
        assert archiver._is_priority(f_upper) is True
        assert archiver._is_priority(f_no_match) is False
