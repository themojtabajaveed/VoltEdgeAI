"""NSE corporate-announcements poller (Plan B Phase B1).

Daemon-thread producer that hits the NSE corporate-announcements API on
a phase-aware cadence and emits `RawTruedataEvent(source="NSE_OFFICIAL")`
records onto the shared raw queue.

Reuses production-grade primitives from `src.data_ingestion.exchange_filings`:
  - `_get_nse_session()` for cookie-warm session reuse.
  - `_fetch_nse_filings(cutoff)` for the actual NSE API call (proven for
    > 12 months of production use; never raises, returns [] on failure).

What this module adds on top:
  - Phase-aware polling cadence (overnight 30m, pre-peak 5m, intraday-hot
    30s, intraday-warm 60s, late 60s, after-cluster 2m, post-cluster 10m).
  - High-watermark filtering so a 24h API window doesn't re-emit prior
    polls' rows.
  - Cross-source canonical event_id for dedupe (so NSE+BSE+TrueData of
    the same announcement collapse to one downstream record).
  - Per-source replay artifact at `data/official_events_YYYY-MM-DD.json`.
  - Mirrors the public surface of `TrueDataClient` so `main.py` can swap
    producers via the source_mode config flag.
"""

from __future__ import annotations

import logging
import queue as _queue
import threading
import time
from datetime import datetime, time as dt_time
from typing import Optional, Set
from zoneinfo import ZoneInfo

from src.data_ingestion.exchange_filings import (
    FilingEvent, _fetch_nse_filings,
)
from src.event_intelligence.config import EventIntelConfig
from src.event_intelligence.dedupe import (
    compute_canonical_event_id, compute_event_id, load_seen_set, mark_seen,
)
from src.event_intelligence.models import RawTruedataEvent
from src.event_intelligence.writers import append_record

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


# ── Cadence schedule (IST). Tuples are (phase_start, poll_interval_s). ──
# Resolved at every tick so the cadence transitions cleanly across phase
# boundaries; no separate scheduler needed.

_SCHEDULE = [
    (dt_time(0, 0),   1800),  # quiet overnight: every 30 min
    (dt_time(3, 30),  900),   # early pre-market: every 15 min
    (dt_time(6, 0),   300),   # pre-market peak: every 5 min
    (dt_time(9, 15),  30),    # intraday hot: every 30 s
    (dt_time(11, 30), 60),    # intraday warm: every 60 s
    (dt_time(14, 30), 60),    # late session: every 60 s
    (dt_time(15, 30), 120),   # after-cluster: every 2 min
    (dt_time(22, 0),  600),   # post-cluster: every 10 min
    (dt_time(23, 30), 1800),  # back to overnight
]


def _now_ist() -> datetime:
    return datetime.now(IST)


def _now_iso() -> str:
    return _now_ist().isoformat()


def _current_interval_s(now_ist: datetime) -> float:
    """Return the polling interval (seconds) for the current IST time."""
    t = now_ist.time().replace(tzinfo=None)
    interval = _SCHEDULE[0][1]
    for start, secs in _SCHEDULE:
        if t >= start:
            interval = secs
        else:
            break
    return float(interval)


def _filing_to_raw(filing: FilingEvent) -> RawTruedataEvent:
    """Convert an NSE FilingEvent (from exchange_filings.py) into a RawTruedataEvent.

    The seen-set key uses the **canonical** event_id so cross-source
    duplicates collapse. The replay artifact stores the per-source ID
    so audit can distinguish "we saw this on NSE seq_id=X" from "we
    saw it on TrueData id=Y".
    """
    seq_id = str(filing.raw.get("seq_id") or filing.raw.get("filing_id") or "") or None
    canonical_id = compute_canonical_event_id(
        symbol=filing.symbol,
        event_type="UNCLASSIFIED",
        filed_at_iso=filing.filed_at,
        headline=filing.headline,
    )
    attchmnt = filing.raw.get("attchmntFile")
    return RawTruedataEvent(
        event_id=canonical_id,
        symbol=filing.symbol,
        exchange="NSE",
        headline=filing.headline,
        category=filing.category,
        received_at=_now_iso(),
        vendor_filed_at=filing.filed_at,
        truedata_event_id=None,
        raw=dict(filing.raw),
        source="NSE_OFFICIAL",
        nse_filing_id=seq_id,
        bse_filing_id=None,
    )


class NSEPoller:
    """Phase-aware NSE corporate-announcements poller.

    Public surface mirrors TrueDataClient: start/stop/is_alive.
    """

    def __init__(
        self,
        cfg: EventIntelConfig,
        out_queue: _queue.Queue,
        fetch_fn=None,   # injection point for tests
    ) -> None:
        self._cfg = cfg
        self._out_queue = out_queue
        self._fetch_fn = fetch_fn or _fetch_nse_filings
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: Set[str] = set()
        self._lock = threading.Lock()
        # Highest filed_at observed (ISO string) — used to skip prior-poll rows.
        self._high_watermark: Optional[str] = None

    # ── Public surface ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[NSEPoller] start() called while already running")
            return
        self._seen = load_seen_set(self._cfg.data_dir)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="nse-poller", daemon=True
        )
        self._thread.start()
        logger.info("[NSEPoller] started")

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                logger.warning("[NSEPoller] thread did not stop cleanly")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def feed_synthetic(self, filing: FilingEvent) -> None:
        """Inject a FilingEvent for tests / manual triggers."""
        evt = _filing_to_raw(filing)
        self._dispatch(evt)

    # ── internals ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        # On startup, do one immediate poll so freshly started services catch up.
        self._poll_once()
        while not self._stop_event.is_set():
            interval = _current_interval_s(_now_ist())
            if self._stop_event.wait(timeout=interval):
                break
            self._poll_once()

    def _poll_once(self) -> None:
        """Fetch a 24h window, filter against high-watermark + seen-set, dispatch new."""
        try:
            filings = self._fetch_fn(
                _now_ist().replace(microsecond=0)
                - _make_24h_delta()
            )
        except Exception as e:  # noqa: BLE001 — never crash the producer
            logger.warning("[NSEPoller] fetch raised: %s", e)
            return

        # The exchange_filings fetch already filters to >= cutoff; we add
        # an additional filter on the local high-watermark with 30-min slack
        # to catch late-arriving same-window updates (filed_at is sometimes
        # set ~5-15 min before the row appears in the API).
        cutoff_iso = self._high_watermark
        new_count = 0
        for f in filings:
            if cutoff_iso and f.filed_at < cutoff_iso:
                continue
            evt = _filing_to_raw(f)
            if self._dispatch(evt):
                new_count += 1
                # Update high-watermark with 30-min slack.
                if self._high_watermark is None or f.filed_at > self._high_watermark:
                    self._high_watermark = _slack_iso(f.filed_at, minutes=-30)
        if new_count:
            logger.info("[NSEPoller] dispatched %d new event(s)", new_count)

    def _dispatch(self, evt: RawTruedataEvent) -> bool:
        """Dedupe, persist replay artifact, enqueue. Returns True if newly dispatched."""
        with self._lock:
            if not mark_seen(evt.event_id, self._seen, self._cfg.data_dir):
                return False

        # Per-source replay artifact (audit trail). Distinct from the
        # canonical event_id used for downstream dedupe.
        try:
            append_record("official_events", evt, data_dir=self._cfg.data_dir)
        except OSError as e:
            logger.error("[NSEPoller] failed to persist event: %s", e)

        # Enqueue to verifier (drop oldest on overflow).
        try:
            self._out_queue.put_nowait(evt)
        except _queue.Full:
            logger.warning("[NSEPoller] queue full — dropping oldest")
            try:
                self._out_queue.get_nowait()
                self._out_queue.put_nowait(evt)
            except (_queue.Empty, _queue.Full):
                pass
        return True


def _make_24h_delta():
    from datetime import timedelta
    return timedelta(hours=24)


def _slack_iso(iso_str: str, minutes: int) -> str:
    """Return iso_str shifted by `minutes` (can be negative). Best-effort."""
    try:
        from datetime import timedelta
        dt = datetime.fromisoformat(iso_str)
        return (dt + timedelta(minutes=minutes)).isoformat()
    except (TypeError, ValueError):
        return iso_str
