"""Tests for the verifier's official-source short-circuit (Plan B Phase B4).

When an event arrives with `source != "TRUEDATA"`, the verifier must
emit `VerifiedEvent(state=CONFIRMED, source_confidence=1.0)` immediately
and skip the polling state machine. This means:
  - No NSE/BSE network call from the verifier for official records.
  - No `LIVE_UNCONFIRMED@0.7` provisional emission (that's vendor-only).
  - The TrueData path stays unchanged.
"""

from __future__ import annotations

import queue
from unittest.mock import patch

import pytest

from src.event_intelligence.config import EventIntelConfig
from src.event_intelligence.models import RawTruedataEvent
from src.event_intelligence.verifier import Verifier


def _cfg(tmp_path) -> EventIntelConfig:
    return EventIntelConfig(
        truedata_api_key="",
        truedata_ws_url="",
        gemini_api_key="",
        data_dir=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )


def _official_raw(source: str = "NSE_OFFICIAL") -> RawTruedataEvent:
    return RawTruedataEvent(
        event_id="abc123abc123abcd",
        symbol="COHANCE",
        exchange="NSE",
        headline="Outcome of Board Meeting - Q4 FY26 Result",
        category="Financial Results",
        received_at="2026-04-29T09:49:18+05:30",
        vendor_filed_at="2026-04-29T09:49:14+05:30",
        truedata_event_id=None,
        raw={"attchmntFile": "https://nsearchives.../q4.pdf"},
        source=source,
        nse_filing_id="NSE-12345" if source == "NSE_OFFICIAL" else None,
        bse_filing_id="BSE-67890" if source == "BSE_OFFICIAL" else None,
    )


def _truedata_raw() -> RawTruedataEvent:
    return RawTruedataEvent(
        event_id="def456def456defe",
        symbol="COHANCE",
        exchange="NSE",
        headline="Outcome of Board Meeting - Q4 FY26 Result",
        category="Financial Results",
        received_at="2026-04-29T09:49:18+05:30",
        vendor_filed_at="2026-04-29T09:49:14+05:30",
        truedata_event_id="TD-001",
        raw={},
        source="TRUEDATA",
    )


def test_official_record_short_circuits_to_confirmed(tmp_path):
    cfg = _cfg(tmp_path)
    in_q: queue.Queue = queue.Queue()
    out_q: queue.Queue = queue.Queue()
    v = Verifier(cfg, in_queue=in_q, out_queue=out_q)

    in_q.put(_official_raw())
    # No fetch_filings_for_symbol should be called for official records.
    with patch("src.event_intelligence.verifier.fetch_filings_for_symbol") as mock_fetch:
        v._drain_in_queue(next_poll_at={})
        # Short-circuit path: zero NSE network calls.
        mock_fetch.assert_not_called()

    drained = []
    while True:
        try:
            drained.append(out_q.get_nowait())
        except queue.Empty:
            break

    assert len(drained) == 1
    ve = drained[0]
    assert ve.state == "CONFIRMED"
    assert ve.source_confidence == 1.0
    assert ve.nse_filing_id == "NSE-12345"
    assert ve.official_subject == "Outcome of Board Meeting - Q4 FY26 Result"
    assert ve.pdf_url == "https://nsearchives.../q4.pdf"


def test_bse_official_short_circuits(tmp_path):
    cfg = _cfg(tmp_path)
    in_q: queue.Queue = queue.Queue()
    out_q: queue.Queue = queue.Queue()
    v = Verifier(cfg, in_queue=in_q, out_queue=out_q)

    in_q.put(_official_raw(source="BSE_OFFICIAL"))
    v._drain_in_queue(next_poll_at={})
    ve = out_q.get_nowait()
    assert ve.state == "CONFIRMED"
    assert ve.source_confidence == 1.0
    assert ve.bse_filing_id == "BSE-67890"


def test_truedata_record_does_not_short_circuit(tmp_path):
    """The original TrueData path must be unchanged."""
    cfg = _cfg(tmp_path)
    in_q: queue.Queue = queue.Queue()
    out_q: queue.Queue = queue.Queue()
    v = Verifier(cfg, in_queue=in_q, out_queue=out_q)

    # Force phase to INTRADAY so the TrueData path emits LIVE_UNCONFIRMED@0.7.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    intraday = datetime(2026, 4, 29, 10, 30, tzinfo=ist)

    in_q.put(_truedata_raw())
    with patch("src.event_intelligence.verifier.datetime") as mock_dt:
        mock_dt.now.return_value = intraday
        # Allow fromisoformat to still work.
        mock_dt.fromisoformat = datetime.fromisoformat
        v._drain_in_queue(next_poll_at={})

    ve = out_q.get_nowait()
    # TrueData path during INTRADAY emits LIVE_UNCONFIRMED first.
    assert ve.state == "LIVE_UNCONFIRMED"
    assert ve.source_confidence == 0.7
    # And the event remains in the verifier's pending set for confirmation.
    assert _truedata_raw().event_id in v._pending


def test_official_short_circuit_handles_missing_attachments(tmp_path):
    """xbrl/pdf URL extraction must degrade to None when raw lacks them."""
    cfg = _cfg(tmp_path)
    in_q: queue.Queue = queue.Queue()
    out_q: queue.Queue = queue.Queue()
    v = Verifier(cfg, in_queue=in_q, out_queue=out_q)

    raw = _official_raw()
    raw.raw = {}  # no attachment URL
    in_q.put(raw)
    v._drain_in_queue(next_poll_at={})
    ve = out_q.get_nowait()
    assert ve.state == "CONFIRMED"
    assert ve.xbrl_url is None
    assert ve.pdf_url is None
