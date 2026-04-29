"""BSE corporate-announcements poller (Plan B Phase B2).

Mirrors `nse_poller.py` but on a slower cadence. BSE has been
intermittently empty from this VM (likely geo-restriction); we treat
NSE as primary and BSE as opportunistic cross-confirmation.

Reuses `_fetch_bse_filings()` from `src.data_ingestion.exchange_filings`.
"""

from __future__ import annotations

import logging
import queue as _queue
import threading
from datetime import datetime, time as dt_time, timedelta
from typing import Optional, Set
from zoneinfo import ZoneInfo

from src.data_ingestion.exchange_filings import (
    FilingEvent, _fetch_bse_filings,
)
from src.event_intelligence.config import EventIntelConfig
from src.event_intelligence.dedupe import (
    compute_canonical_event_id, load_seen_set, mark_seen,
)
from src.event_intelligence.models import RawTruedataEvent
from src.event_intelligence.writers import append_record

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


# Slower BSE cadence schedule. Same shape as NSE but lower frequency.
_SCHEDULE = [
    (dt_time(0, 0),    3600),   # overnight: hourly
    (dt_time(3, 30),   1800),   # early pre-market: 30 min
    (dt_time(6, 0),    600),    # pre-market peak: 10 min
    (dt_time(9, 15),   300),    # intraday hot: 5 min
    (dt_time(14, 30),  300),    # late + after-hours: 5 min
    (dt_time(22, 0),   1800),   # post-cluster: 30 min
]


def _now_ist() -> datetime:
    return datetime.now(IST)


def _now_iso() -> str:
    return _now_ist().isoformat()


def _current_interval_s(now_ist: datetime) -> float:
    t = now_ist.time().replace(tzinfo=None)
    interval = _SCHEDULE[0][1]
    for start, secs in _SCHEDULE:
        if t >= start:
            interval = secs
        else:
            break
    return float(interval)


def _filing_to_raw(filing: FilingEvent) -> RawTruedataEvent:
    scrip_or_id = str(filing.raw.get("filing_id") or filing.raw.get("SCRIP_CD") or "") or None
    canonical_id = compute_canonical_event_id(
        symbol=filing.symbol,
        event_type="UNCLASSIFIED",
        filed_at_iso=filing.filed_at,
        headline=filing.headline,
    )
    return RawTruedataEvent(
        event_id=canonical_id,
        symbol=filing.symbol,
        exchange="BSE",
        headline=filing.headline,
        category=filing.category,
        received_at=_now_iso(),
        vendor_filed_at=filing.filed_at,
        truedata_event_id=None,
        raw=dict(filing.raw),
        source="BSE_OFFICIAL",
        nse_filing_id=None,
        bse_filing_id=scrip_or_id,
    )


class BSEPoller:
    """Phase-aware BSE corporate-announcements poller.

    Empty-response handling: after three consecutive empty fetches, drop
    cadence to once-per-hour to avoid wasted load. NSE remains the
    primary source so this never blocks the pipeline.
    """

    _MAX_EMPTY_BEFORE_DROP = 3
    _DROP_INTERVAL_S = 3600.0

    def __init__(
        self,
        cfg: EventIntelConfig,
        out_queue: _queue.Queue,
        fetch_fn=None,
    ) -> None:
        self._cfg = cfg
        self._out_queue = out_queue
        self._fetch_fn = fetch_fn or _fetch_bse_filings
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: Set[str] = set()
        self._lock = threading.Lock()
        self._high_watermark: Optional[str] = None
        self._consecutive_empty = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._seen = load_seen_set(self._cfg.data_dir)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="bse-poller", daemon=True
        )
        self._thread.start()
        logger.info("[BSEPoller] started")

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def feed_synthetic(self, filing: FilingEvent) -> None:
        evt = _filing_to_raw(filing)
        self._dispatch(evt)

    # ── internals ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        self._poll_once()
        while not self._stop_event.is_set():
            interval = (
                self._DROP_INTERVAL_S
                if self._consecutive_empty >= self._MAX_EMPTY_BEFORE_DROP
                else _current_interval_s(_now_ist())
            )
            if self._stop_event.wait(timeout=interval):
                break
            self._poll_once()

    def _poll_once(self) -> None:
        try:
            filings = self._fetch_fn(_now_ist() - timedelta(hours=24))
        except Exception as e:  # noqa: BLE001
            logger.warning("[BSEPoller] fetch raised: %s", e)
            return

        if not filings:
            self._consecutive_empty += 1
            if self._consecutive_empty == self._MAX_EMPTY_BEFORE_DROP:
                logger.warning(
                    "[BSEPoller] %d consecutive empty fetches — dropping to hourly cadence",
                    self._consecutive_empty,
                )
            return
        # Reset on first non-empty fetch.
        self._consecutive_empty = 0

        cutoff_iso = self._high_watermark
        new_count = 0
        for f in filings:
            if cutoff_iso and f.filed_at < cutoff_iso:
                continue
            evt = _filing_to_raw(f)
            if self._dispatch(evt):
                new_count += 1
                if self._high_watermark is None or f.filed_at > self._high_watermark:
                    self._high_watermark = _slack_iso(f.filed_at, minutes=-30)
        if new_count:
            logger.info("[BSEPoller] dispatched %d new event(s)", new_count)

    def _dispatch(self, evt: RawTruedataEvent) -> bool:
        with self._lock:
            if not mark_seen(evt.event_id, self._seen, self._cfg.data_dir):
                return False
        try:
            append_record("official_events", evt, data_dir=self._cfg.data_dir)
        except OSError as e:
            logger.error("[BSEPoller] failed to persist event: %s", e)
        try:
            self._out_queue.put_nowait(evt)
        except _queue.Full:
            logger.warning("[BSEPoller] queue full — dropping oldest")
            try:
                self._out_queue.get_nowait()
                self._out_queue.put_nowait(evt)
            except (_queue.Empty, _queue.Full):
                pass
        return True


def _slack_iso(iso_str: str, minutes: int) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return (dt + timedelta(minutes=minutes)).isoformat()
    except (TypeError, ValueError):
        return iso_str
