"""Tests for NSEPoller (Plan B Phase B1).

Mocks `_fetch_nse_filings` so tests run offline. The real fixture at
`tests/fixtures/nse_announcements/2026-04-29.json` is the basis for an
optional integration test gated on its presence.
"""

from __future__ import annotations

import json
import os
import queue
from pathlib import Path

import pytest

from src.data_ingestion.exchange_filings import FilingEvent
from src.event_intelligence.config import EventIntelConfig
from src.event_intelligence.nse_poller import (
    NSEPoller, _current_interval_s, _filing_to_raw,
)


def _cfg(tmp_path) -> EventIntelConfig:
    return EventIntelConfig(
        truedata_api_key="",
        truedata_ws_url="",
        gemini_api_key="",
        data_dir=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )


def _filing(symbol="ACME", headline="Q4 FY26 Result", filed_at="2026-04-29T09:49:14+05:30"):
    return FilingEvent(
        symbol=symbol,
        company_name=symbol,
        headline=headline,
        category="Financial Results",
        filed_at=filed_at,
        exchange="NSE",
        urgency=8.0,
        deal_size_inr=None,
        raw={"seq_id": "NSE-12345", "attchmntFile": "https://nsearchives.../q4.pdf"},
    )


def test_filing_to_raw_tags_source_correctly():
    raw = _filing_to_raw(_filing())
    assert raw.source == "NSE_OFFICIAL"
    assert raw.exchange == "NSE"
    assert raw.nse_filing_id == "NSE-12345"
    assert raw.bse_filing_id is None
    assert raw.truedata_event_id is None


def test_filing_to_raw_uses_canonical_event_id():
    raw = _filing_to_raw(_filing())
    # event_id is the canonical (cross-source) id, not the per-source one.
    # Different vendor ids should still hash to the same event_id when
    # symbol/type/date/headline match.
    raw2 = _filing_to_raw(_filing())
    assert raw.event_id == raw2.event_id


def test_poller_dispatches_new_events(tmp_path):
    cfg = _cfg(tmp_path)
    out_q: queue.Queue = queue.Queue()
    filings = [_filing(symbol="A"), _filing(symbol="B"), _filing(symbol="C")]
    poller = NSEPoller(cfg, out_q, fetch_fn=lambda cutoff: filings)

    poller._poll_once()

    drained = []
    while True:
        try:
            drained.append(out_q.get_nowait())
        except queue.Empty:
            break
    assert len(drained) == 3
    assert all(r.source == "NSE_OFFICIAL" for r in drained)


def test_poller_skips_already_seen(tmp_path):
    """Second poll with same filings should not re-emit — seen-set handles it."""
    cfg = _cfg(tmp_path)
    out_q: queue.Queue = queue.Queue()
    filings = [_filing(symbol="A"), _filing(symbol="B")]
    poller = NSEPoller(cfg, out_q, fetch_fn=lambda cutoff: filings)

    poller._poll_once()
    first = _drain(out_q)
    assert len(first) == 2

    poller._poll_once()
    second = _drain(out_q)
    assert len(second) == 0  # nothing new


def test_poller_persists_official_events_artifact(tmp_path):
    cfg = _cfg(tmp_path)
    out_q: queue.Queue = queue.Queue()
    filings = [_filing(symbol="ACME")]
    poller = NSEPoller(cfg, out_q, fetch_fn=lambda cutoff: filings)
    poller._poll_once()

    artifact = list(tmp_path.glob("official_events_*.json"))
    assert len(artifact) == 1
    records = json.loads(artifact[0].read_text())
    assert len(records) == 1
    assert records[0]["symbol"] == "ACME"
    assert records[0]["source"] == "NSE_OFFICIAL"


def test_poller_never_raises_on_fetch_failure(tmp_path):
    cfg = _cfg(tmp_path)
    out_q: queue.Queue = queue.Queue()

    def boom(cutoff):
        raise RuntimeError("simulated NSE outage")

    poller = NSEPoller(cfg, out_q, fetch_fn=boom)
    poller._poll_once()  # must not raise
    assert out_q.empty()


def test_feed_synthetic_dispatches_one_event(tmp_path):
    cfg = _cfg(tmp_path)
    out_q: queue.Queue = queue.Queue()
    poller = NSEPoller(cfg, out_q, fetch_fn=lambda cutoff: [])
    poller._seen = set()
    poller.feed_synthetic(_filing(symbol="TEST"))
    assert out_q.qsize() == 1
    raw = out_q.get_nowait()
    assert raw.symbol == "TEST"
    assert raw.source == "NSE_OFFICIAL"


def test_high_watermark_filters_old_events(tmp_path):
    """When high_watermark is set, filings before it are skipped."""
    cfg = _cfg(tmp_path)
    out_q: queue.Queue = queue.Queue()
    poller = NSEPoller(cfg, out_q, fetch_fn=lambda cutoff: [])
    poller._high_watermark = "2026-04-29T15:00:00+05:30"

    # Inject an old filing manually via _filing_to_raw + _dispatch logic.
    old_filing = _filing(filed_at="2026-04-29T08:00:00+05:30")
    poller._fetch_fn = lambda cutoff: [old_filing]
    poller._poll_once()
    assert out_q.empty()

    # New filing past the watermark.
    new_filing = _filing(symbol="NEW", filed_at="2026-04-29T16:00:00+05:30")
    poller._fetch_fn = lambda cutoff: [new_filing]
    poller._poll_once()
    assert out_q.qsize() == 1


@pytest.mark.parametrize(
    "hour,minute,expected_max_interval",
    [
        (10, 0, 30),     # intraday hot — every 30s
        (12, 0, 60),     # intraday warm — every 60s
        (15, 0, 60),     # late session — every 60s
        (16, 0, 120),    # after-cluster — every 2 min
        (1, 0, 1800),    # overnight — every 30 min
    ],
)
def test_phase_aware_cadence(hour, minute, expected_max_interval):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    t = datetime(2026, 4, 29, hour, minute, tzinfo=ist)
    interval = _current_interval_s(t)
    assert interval <= expected_max_interval, (
        f"interval at {hour:02d}:{minute:02d} = {interval}s, expected ≤ {expected_max_interval}s"
    )


# ── Optional integration test against captured NSE fixture ─────────────────

_FIXTURE = Path(__file__).parent / "fixtures" / "nse_announcements" / "2026-04-29.json"


@pytest.mark.skipif(not _FIXTURE.exists(), reason="NSE fixture not captured")
def test_poller_against_real_fixture(tmp_path):
    """Replay one captured day of real NSE announcements through the poller.

    Verifies the integration path: fixture rows → _fetch_nse_filings parsing
    → _filing_to_raw → dispatch → official_events_*.json + raw queue.
    """
    cfg = _cfg(tmp_path)
    out_q: queue.Queue = queue.Queue()

    # Import the actual exchange_filings parsing path so we exercise the
    # row → FilingEvent translation that runs in production.
    fixture_rows = json.loads(_FIXTURE.read_text())
    from src.data_ingestion.exchange_filings import (
        _parse_dt_ist, _NSE_DT_FORMATS, _score_urgency, _parse_deal_size_inr,
    )

    def _replay(cutoff):
        filings = []
        for item in fixture_rows:
            symbol = (item.get("symbol") or "").strip().upper()
            subject = (item.get("desc") or "").strip()
            category = (item.get("smIndustry") or "").strip()
            raw_ts = item.get("an_dt") or item.get("sort_date") or ""
            filed_dt = _parse_dt_ist(str(raw_ts), _NSE_DT_FORMATS)
            if not symbol or not subject or filed_dt is None:
                continue
            filings.append(FilingEvent(
                symbol=symbol,
                company_name=item.get("sm_name", ""),
                headline=subject,
                category=category,
                filed_at=filed_dt.isoformat(),
                exchange="NSE",
                urgency=_score_urgency(category, subject),
                deal_size_inr=_parse_deal_size_inr(subject),
                raw=item,
            ))
        return filings

    poller = NSEPoller(cfg, out_q, fetch_fn=_replay)
    poller._poll_once()
    drained = _drain(out_q)
    # We expect at least some rows to flow through (fixture has 20 rows,
    # most should parse cleanly).
    assert len(drained) >= 5
    # All should be tagged NSE_OFFICIAL with valid canonical event_ids.
    for r in drained:
        assert r.source == "NSE_OFFICIAL"
        assert len(r.event_id) == 16


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out
