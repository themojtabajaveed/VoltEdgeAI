"""TrueData Corporate Data WebSocket consumer.

Phase A — captures the live announcement stream into
`data/truedata_events_YYYY-MM-DD.json` and pushes normalized
`RawTruedataEvent` records to a shared queue for the verifier stage.

NOTE — Phase-A discovery items still pending at the time of authoring:
  - Exact WebSocket URL and authentication scheme
  - Event-payload schema (field names, nesting, timestamp format)
  - Heartbeat / keepalive convention
  - Reconnect token semantics

The plumbing below (queue, dedupe, atomic write, daemon thread,
exponential backoff, graceful shutdown) is complete and tested. The
boundary that needs the discovery output is `_normalize_payload()`
and the `websockets`/`websocket-client` choice in `_connect()`. Both
are clearly marked with `# TODO(phase-A-discovery)` comments and a
default implementation that talks to a generic JSON-WebSocket so the
rest of the pipeline can be exercised end-to-end against a stub.

Mirrors thread/queue patterns from `src/data_ingestion/market_live.py`.
"""

from __future__ import annotations

import json
import logging
import queue as _queue
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Set
from zoneinfo import ZoneInfo

from src.event_intelligence.config import EventIntelConfig
from src.event_intelligence.dedupe import (
    compute_event_id, load_seen_set, mark_seen,
)
from src.event_intelligence.models import RawTruedataEvent
from src.event_intelligence.writers import append_record

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def _normalize_payload(payload: Dict[str, Any]) -> Optional[RawTruedataEvent]:
    """Convert a TrueData WebSocket message into a RawTruedataEvent.

    # TODO(phase-A-discovery): replace these field accessors with the
    # real TrueData payload shape once the API docs are in hand. The
    # placeholder below assumes a generic JSON shape:
    #   {
    #     "symbol": "...",
    #     "exchange": "NSE"|"BSE",
    #     "headline": "...",
    #     "category": "...",
    #     "filed_at": "<ISO IST>",
    #     "id": "<vendor event id>"
    #   }
    """
    try:
        symbol = (payload.get("symbol") or "").strip().upper()
        if not symbol:
            return None

        exchange = (payload.get("exchange") or "NSE").strip().upper()
        headline = (payload.get("headline") or payload.get("subject") or "").strip()
        category = (payload.get("category") or "").strip()
        vendor_filed_at = payload.get("filed_at") or payload.get("timestamp")
        vendor_event_id = (
            payload.get("id")
            or payload.get("event_id")
            or payload.get("announcementId")
        )

        # Provisional event_id at ingest time. The classifier later
        # recomputes with the canonical event_type, but we need *some*
        # ID now for the seen-set.
        event_id = compute_event_id(
            symbol=symbol,
            event_type="UNCLASSIFIED",
            filed_at_iso=str(vendor_filed_at or _now_ist_iso()),
            source_id=str(vendor_event_id) if vendor_event_id else None,
        )

        return RawTruedataEvent(
            event_id=event_id,
            symbol=symbol,
            exchange=exchange,
            headline=headline,
            category=category,
            received_at=_now_ist_iso(),
            vendor_filed_at=str(vendor_filed_at) if vendor_filed_at else None,
            truedata_event_id=str(vendor_event_id) if vendor_event_id else None,
            raw=payload,
        )
    except Exception as e:  # noqa: BLE001 — never propagate from ingest path
        logger.warning("[TrueData] failed to normalize payload: %s", e)
        return None


class TrueDataClient:
    """Lazy-init WebSocket consumer with reconnect + dedupe + persistence.

    Public surface:
        client = TrueDataClient(cfg, out_queue)
        client.start()                  # spawns daemon thread
        client.stop()                   # graceful shutdown
        client.is_alive()               # health check for main loop
    """

    def __init__(
        self,
        cfg: EventIntelConfig,
        out_queue: _queue.Queue,
        on_event: Optional[Callable[[RawTruedataEvent], None]] = None,
    ) -> None:
        self._cfg = cfg
        self._out_queue = out_queue
        self._on_event = on_event
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: Set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.warning("[TrueData] start() called while already running")
            return
        self._seen = load_seen_set(self._cfg.data_dir)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="truedata-consumer", daemon=True
        )
        self._thread.start()
        logger.info("[TrueData] consumer thread started")

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                logger.warning("[TrueData] consumer thread did not stop cleanly")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def feed_synthetic(self, payload: Dict[str, Any]) -> None:
        """Inject a synthetic event for tests / manual triggers."""
        evt = _normalize_payload(payload)
        if evt is None:
            return
        self._dispatch(evt)

    # ── internals ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        backoff = self._cfg.truedata_reconnect_initial_s
        while not self._stop_event.is_set():
            try:
                self._connect_and_consume()
                # Clean disconnect — reset backoff.
                backoff = self._cfg.truedata_reconnect_initial_s
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[TrueData] connection error (%s); reconnect in %.1fs",
                    e, backoff,
                )
                if self._stop_event.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2.0, self._cfg.truedata_reconnect_max_s)
            else:
                # Clean exit from inner loop without exception — short pause
                # to avoid hot-spin if upstream closes immediately.
                if self._stop_event.wait(timeout=2.0):
                    break

    def _connect_and_consume(self) -> None:
        """Open the WebSocket and pump messages into _dispatch().

        # TODO(phase-A-discovery): swap the placeholder below for the
        # real TrueData WebSocket library and authentication. The
        # `websockets` package (asyncio) and `websocket-client` (sync)
        # are both options; the rest of the pipeline is sync, so
        # `websocket-client` is preferred for symmetry.
        """
        if not self._cfg.truedata_ws_url:
            logger.error("[TrueData] TRUEDATA_WS_URL not configured — sleeping")
            if self._stop_event.wait(timeout=60.0):
                return
            return

        try:
            import websocket  # type: ignore
        except ImportError:
            logger.error(
                "[TrueData] websocket-client not installed. "
                "Run: pip install websocket-client"
            )
            if self._stop_event.wait(timeout=60.0):
                return
            return

        url = self._cfg.truedata_ws_url
        # TODO(phase-A-discovery): inject auth header / login frame per
        # TrueData specification once known.
        headers = []
        if self._cfg.truedata_api_key:
            headers.append(f"Authorization: Bearer {self._cfg.truedata_api_key}")

        ws = websocket.WebSocket()
        ws.connect(url, header=headers, timeout=15)
        logger.info("[TrueData] connected: %s", url)

        try:
            while not self._stop_event.is_set():
                ws.settimeout(1.0)
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    logger.info("[TrueData] connection closed by peer")
                    return
                if not msg:
                    continue
                try:
                    payload = json.loads(msg) if isinstance(msg, (str, bytes)) else msg
                except json.JSONDecodeError:
                    logger.warning("[TrueData] non-JSON message ignored")
                    continue
                evt = _normalize_payload(payload)
                if evt is not None:
                    self._dispatch(evt)
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    def _dispatch(self, evt: RawTruedataEvent) -> None:
        """Dedupe, persist, enqueue."""
        with self._lock:
            if not mark_seen(evt.event_id, self._seen, self._cfg.data_dir):
                logger.debug("[TrueData] duplicate event_id=%s skipped", evt.event_id)
                return

        # Persist raw record (audit trail).
        try:
            append_record("truedata_events", evt, data_dir=self._cfg.data_dir)
        except OSError as e:
            logger.error("[TrueData] failed to persist event: %s", e)

        # Enqueue to verifier (drop oldest on overflow).
        try:
            self._out_queue.put_nowait(evt)
        except _queue.Full:
            logger.warning("[TrueData] verifier queue full — dropping oldest")
            try:
                self._out_queue.get_nowait()
                self._out_queue.put_nowait(evt)
            except (_queue.Empty, _queue.Full):
                pass

        if self._on_event is not None:
            try:
                self._on_event(evt)
            except Exception as e:  # noqa: BLE001
                logger.warning("[TrueData] on_event callback failed: %s", e)
