"""NSE/BSE confirmation state machine for TrueData events.

Implements the phase-aware trigger/confirm policy from the design doc §2.2:

    | Phase           | Time (IST)     | Policy                       |
    |-----------------|----------------|------------------------------|
    | Pre-market      | 03:30 - 09:15  | Block until official confirm |
    | Intraday        | 09:15 - 14:30  | Emit immediately at 0.7,     |
    |                 |                | upgrade to 1.0 on confirm    |
    | Late session    | 14:30 - 15:30  | Emit immediately at 0.7      |
    | After-hours     | 15:30 - 03:30  | Block until official confirm |

State transitions:
    INITIAL
      → AWAITING_CONFIRM (pre-market or after-hours)
      → LIVE_UNCONFIRMED (intraday or late) — emits at 0.7 immediately

    AWAITING_CONFIRM | LIVE_UNCONFIRMED
      → CONFIRMED      (NSE/BSE filing matched, no conflict)
      → CONFLICT       (category or metric disagreement)
      → EXPIRED_NO_CONFIRM (timeout after verifier_timeout_s)

This module owns the per-event timer + retry loop. It pushes
`VerifiedEvent` records onto an output queue once a terminal state is
reached (or a 0.7 emission for intraday).
"""

from __future__ import annotations

import logging
import queue as _queue
import threading
import time
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from src.data_ingestion.exchange_filings import (
    FilingEvent, _normalise_filing_category, fetch_filings_for_symbol,
)
from src.event_intelligence.config import EventIntelConfig
from src.event_intelligence.models import RawTruedataEvent, VerifiedEvent

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_PRE_MARKET_END = dt_time(9, 15)
_INTRADAY_START = dt_time(9, 15)
_INTRADAY_END = dt_time(14, 30)
_LATE_SESSION_END = dt_time(15, 30)
_AFTER_HOURS_START_NEXT = dt_time(3, 30)


def _phase(now_ist: datetime) -> str:
    """Return one of: PRE_MARKET, INTRADAY, LATE_SESSION, AFTER_HOURS."""
    t = now_ist.timetz().replace(tzinfo=None)
    if dt_time(3, 30) <= t < _PRE_MARKET_END:
        return "PRE_MARKET"
    if _INTRADAY_START <= t < _INTRADAY_END:
        return "INTRADAY"
    if _INTRADAY_END <= t < _LATE_SESSION_END:
        return "LATE_SESSION"
    return "AFTER_HOURS"


def _phase_policy(phase: str) -> str:
    """B (blocking), C (non-blocking emit + async upgrade)."""
    if phase in ("PRE_MARKET", "AFTER_HOURS"):
        return "B"
    return "C"


# Token-overlap similarity, used to dedupe official vs vendor headlines.
def _subject_similarity(a: str, b: str) -> float:
    a_tokens = {t for t in (a or "").upper().split() if len(t) > 2}
    b_tokens = {t for t in (b or "").upper().split() if len(t) > 2}
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))


def _match_official(
    raw: RawTruedataEvent, candidates: List[FilingEvent]
) -> Optional[FilingEvent]:
    """Pick the official filing that best matches the TrueData event."""
    if not candidates:
        return None
    best: Optional[FilingEvent] = None
    best_score = 0.0
    for f in candidates:
        if (f.symbol or "").upper() != (raw.symbol or "").upper():
            continue
        score = _subject_similarity(raw.headline, f.headline)
        if score > best_score:
            best = f
            best_score = score
    # Require at least 30% token overlap to claim a match.
    return best if best_score >= 0.3 else None


def _detect_conflict(
    raw: RawTruedataEvent, official: FilingEvent
) -> Optional[str]:
    """Return a conflict-notes string if vendor and official disagree on category."""
    if not raw.category or not official.category:
        return None
    vendor_norm = _normalise_filing_category(raw.category, raw.headline) or ""
    official_norm = _normalise_filing_category(official.category, official.headline) or ""
    if vendor_norm and official_norm and vendor_norm != official_norm:
        return f"category mismatch: vendor={vendor_norm} official={official_norm}"
    return None


def _resolve(
    raw: RawTruedataEvent,
    candidates: List[FilingEvent],
    initial_state: str,
) -> VerifiedEvent:
    """Match raw against candidates and return a VerifiedEvent (terminal or transient)."""
    matched = _match_official(raw, candidates)
    if matched is None:
        # No official confirmation yet.
        if initial_state == "AWAITING_CONFIRM":
            return VerifiedEvent(
                raw=raw, state="AWAITING_CONFIRM", source_confidence=0.0,
            )
        return VerifiedEvent(
            raw=raw, state="LIVE_UNCONFIRMED", source_confidence=0.7,
        )

    conflict = _detect_conflict(raw, matched)
    if conflict:
        return VerifiedEvent(
            raw=raw,
            state="CONFLICT",
            source_confidence=0.5,
            nse_filing_id=str(matched.raw.get("filing_id") or "") or None
                if matched.exchange == "NSE" else None,
            bse_filing_id=str(matched.raw.get("filing_id") or "") or None
                if matched.exchange == "BSE" else None,
            official_subject=matched.headline,
            official_filed_at=matched.filed_at,
            official_category=matched.category,
            nse_xbrl_meta_url=matched.raw.get("nse_xbrl_meta_url"),
            xbrl_url=None,  # financial-statement XBRL path not yet discovered
            pdf_url=matched.raw.get("pdf_url") or matched.raw.get("attchmntFile"),
            conflict_notes=conflict,
        )

    return VerifiedEvent(
        raw=raw,
        state="CONFIRMED",
        source_confidence=1.0,
        nse_filing_id=str(matched.raw.get("filing_id") or "") or None
            if matched.exchange == "NSE" else None,
        bse_filing_id=str(matched.raw.get("filing_id") or "") or None
            if matched.exchange == "BSE" else None,
        official_subject=matched.headline,
        official_filed_at=matched.filed_at,
        official_category=matched.category,
        nse_xbrl_meta_url=matched.raw.get("nse_xbrl_meta_url"),
        xbrl_url=None,  # financial-statement XBRL path not yet discovered
        pdf_url=matched.raw.get("pdf_url") or matched.raw.get("attchmntFile"),
    )


class Verifier:
    """Background poller that resolves TrueData events to VerifiedEvent.

    Public surface mirrors TrueDataClient: start/stop/is_alive.
    Each pending event has its own deadline + retry cadence; concurrent
    events handled in a small thread pool (default 4 workers).
    """

    def __init__(
        self,
        cfg: EventIntelConfig,
        in_queue: _queue.Queue,
        out_queue: _queue.Queue,
    ) -> None:
        self._cfg = cfg
        self._in_queue = in_queue
        self._out_queue = out_queue
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # event_id → vendor-arrival timestamp (epoch seconds)
        self._pending: Dict[str, float] = {}
        # event_ids that have already been emitted at 0.7 (so we don't
        # emit twice during async upgrade).
        self._emitted_partial: Set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="event-verifier", daemon=True
        )
        self._thread.start()
        logger.info("[Verifier] started")

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internals ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        next_poll_at: Dict[str, float] = {}
        while not self._stop_event.is_set():
            # Drain new events from in_queue (non-blocking).
            self._drain_in_queue(next_poll_at)
            # Process pending events whose poll-time has come.
            self._tick_pending(next_poll_at)
            # Avoid tight loop.
            if self._stop_event.wait(timeout=1.0):
                break

    def _drain_in_queue(self, next_poll_at: Dict[str, float]) -> None:
        events_to_process = []
        # Get all currently queued events without blocking.
        while True:
            try:
                evt = self._in_queue.get_nowait()
            except _queue.Empty:
                break
            events_to_process.append(evt)

        for raw in events_to_process:
            now = time.time()
            with self._lock:
                if raw.event_id in self._pending:
                    continue
                self._pending[raw.event_id] = now
                next_poll_at[raw.event_id] = now  # poll immediately

            phase = _phase(datetime.now(IST))
            policy = _phase_policy(phase)
            if policy == "C":
                # Non-blocking: emit a 0.7 record now, then continue verifying.
                provisional = VerifiedEvent(
                    raw=raw, state="LIVE_UNCONFIRMED", source_confidence=0.7
                )
                self._emit(provisional, partial=True)

    def _tick_pending(self, next_poll_at: Dict[str, float]) -> None:
        now = time.time()
        # Snapshot keys to allow mutation during iteration.
        with self._lock:
            event_ids = list(self._pending.keys())

        for event_id in event_ids:
            with self._lock:
                if event_id not in self._pending:
                    continue
                arrived_at = self._pending[event_id]
                due_at = next_poll_at.get(event_id, 0.0)

            if now < due_at:
                continue

            # Lookup the original raw event from in_queue is not possible
            # (we already drained). We need to keep the raw record. Add
            # storage for that.
            raw = self._raw_for(event_id)
            if raw is None:
                # Should not happen — defensive.
                with self._lock:
                    self._pending.pop(event_id, None)
                next_poll_at.pop(event_id, None)
                continue

            # Timeout check.
            if now - arrived_at >= self._cfg.verifier_timeout_s:
                expired = VerifiedEvent(
                    raw=raw,
                    state="EXPIRED_NO_CONFIRM",
                    source_confidence=0.5
                    if event_id in self._emitted_partial
                    else 0.0,
                    conflict_notes=f"no official filing matched within {int(self._cfg.verifier_timeout_s)}s",
                )
                self._emit(expired, partial=False)
                with self._lock:
                    self._pending.pop(event_id, None)
                next_poll_at.pop(event_id, None)
                continue

            # Try to confirm.
            try:
                candidates = fetch_filings_for_symbol(
                    raw.symbol, raw.vendor_filed_at or raw.received_at
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[Verifier] fetch failed for %s: %s", raw.symbol, e)
                candidates = []

            phase = _phase(datetime.now(IST))
            policy = _phase_policy(phase)
            initial_state = "AWAITING_CONFIRM" if policy == "B" else "LIVE_UNCONFIRMED"
            resolved = _resolve(raw, candidates, initial_state)

            if resolved.state in ("CONFIRMED", "CONFLICT"):
                self._emit(resolved, partial=False)
                with self._lock:
                    self._pending.pop(event_id, None)
                next_poll_at.pop(event_id, None)
                continue

            # Reschedule.
            elapsed = now - arrived_at
            cadence = (
                self._cfg.verifier_poll_initial_s
                if elapsed < 5 * 60
                else self._cfg.verifier_poll_followup_s
            )
            next_poll_at[event_id] = now + cadence

    # The Verifier needs to retain raw events because in_queue.get
    # consumes them. Use a side-table keyed by event_id.
    _raw_table: Dict[str, RawTruedataEvent] = {}

    def _raw_for(self, event_id: str) -> Optional[RawTruedataEvent]:
        return self._raw_table.get(event_id)

    def _store_raw(self, raw: RawTruedataEvent) -> None:
        self._raw_table[raw.event_id] = raw

    def _emit(self, ve: VerifiedEvent, partial: bool) -> None:
        if partial:
            with self._lock:
                if ve.raw.event_id in self._emitted_partial:
                    return
                self._emitted_partial.add(ve.raw.event_id)
        try:
            self._out_queue.put_nowait(ve)
        except _queue.Full:
            logger.warning("[Verifier] out_queue full — dropping oldest")
            try:
                self._out_queue.get_nowait()
                self._out_queue.put_nowait(ve)
            except (_queue.Empty, _queue.Full):
                pass


def _drain_in_queue_with_storage(
    verifier: Verifier, next_poll_at: Dict[str, float]
) -> None:
    """Variant that stores raw events in the verifier's side-table.
    Bound to the class via monkey-patch below to keep _drain_in_queue
    pure for clarity above."""
    pass


# Re-bind _drain_in_queue to also store the raw record.
_original_drain = Verifier._drain_in_queue


def _drain_with_store(self: Verifier, next_poll_at: Dict[str, float]) -> None:
    events_to_process = []
    while True:
        try:
            evt = self._in_queue.get_nowait()
        except _queue.Empty:
            break
        events_to_process.append(evt)

    for raw in events_to_process:
        # ── Plan B: official-source short-circuit ──────────────────────
        # When the producer is the NSE/BSE official feed itself, the
        # record is born at source_confidence=1.0. Skip the polling state
        # machine and emit CONFIRMED directly. `getattr` keeps backward
        # compat with any RawTruedataEvent built before the source field
        # was introduced (defaults to TRUEDATA → existing path).
        if getattr(raw, "source", "TRUEDATA") != "TRUEDATA":
            self._emit_official_confirmed(raw)
            continue

        now = time.time()
        with self._lock:
            if raw.event_id in self._pending:
                continue
            self._pending[raw.event_id] = now
            self._store_raw(raw)
            next_poll_at[raw.event_id] = now

        phase = _phase(datetime.now(IST))
        policy = _phase_policy(phase)
        if policy == "C":
            provisional = VerifiedEvent(
                raw=raw, state="LIVE_UNCONFIRMED", source_confidence=0.7
            )
            self._emit(provisional, partial=True)


def _emit_official_confirmed(self: Verifier, raw: RawTruedataEvent) -> None:
    """Plan B: emit a CONFIRMED@1.0 VerifiedEvent built straight from raw fields.

    NSE/BSE feeds embed `attchmntFile` (PDF) and sometimes `xbrl` URLs
    directly in the row dict, so the parser can pick them up without
    a separate fetch.
    """
    raw_dict = raw.raw if isinstance(raw.raw, dict) else {}
    confirmed = VerifiedEvent(
        raw=raw,
        state="CONFIRMED",
        source_confidence=1.0,
        nse_filing_id=raw.nse_filing_id,
        bse_filing_id=raw.bse_filing_id,
        official_subject=raw.headline,
        official_filed_at=raw.vendor_filed_at,
        official_category=raw.category,
        nse_xbrl_meta_url=raw_dict.get("nse_xbrl_meta_url"),
        xbrl_url=None,  # financial-statement XBRL path not yet discovered
        pdf_url=raw_dict.get("pdf_url") or raw_dict.get("attchmntFile"),
    )
    self._emit(confirmed, partial=False)


Verifier._drain_in_queue = _drain_with_store  # type: ignore[assignment]
Verifier._emit_official_confirmed = _emit_official_confirmed  # type: ignore[assignment]
