"""Event-intelligence pipeline entry point.

Wires Phases A → B → C → D → E into a single long-running process. Run as
`voltedge-events.service` (separate from the main `voltedge.service`).

Threads:
    1. TrueData WebSocket consumer (truedata_client.TrueDataClient)
    2. NSE/BSE verifier (verifier.Verifier)
    3. Parse + classify worker (this module)
    4. Heartbeat ticker (this module)

Inter-thread communication: bounded `queue.Queue` (default max 1000).
On overflow: drop oldest with a warning. Graceful shutdown drains the
queues and persists the seen-set.
"""

from __future__ import annotations

import logging
import os
import queue as _queue
import signal
import sys
import threading
import time
from typing import Optional

from src.event_intelligence.classifier import classify
from src.event_intelligence.config import EventIntelConfig, load_config
from src.event_intelligence.models import EarningsSignal, VerifiedEvent
from src.event_intelligence.official_client import OfficialClient
from src.event_intelligence.parsers.base import ParsedDocument, ParseStatus
from src.event_intelligence.parsers.fetcher import fetch_attachment
from src.event_intelligence.parsers.heuristic import parse_heuristic
from src.event_intelligence.parsers.pdf_parser import parse_pdf
from src.event_intelligence.parsers.xbrl_parser import parse_xbrl
from src.event_intelligence.shadow_router import shadow_route
from src.event_intelligence.truedata_client import TrueDataClient
from src.event_intelligence.verifier import Verifier
from src.event_intelligence.writers import append_record, write_heartbeat

logger = logging.getLogger(__name__)


def _setup_logging(cfg: EventIntelConfig) -> None:
    os.makedirs(cfg.log_dir, exist_ok=True)
    log_file = os.path.join(cfg.log_dir, "event_intel.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ── Parse + classify worker ─────────────────────────────────────────────────

def _parse_document(verified: VerifiedEvent) -> ParsedDocument:
    """XBRL primary, PDF fallback, heuristic ultimate fallback."""
    if verified.xbrl_url:
        content = fetch_attachment(verified.xbrl_url)
        if content:
            doc = parse_xbrl(content)
            if doc.status in (ParseStatus.SUCCESS, ParseStatus.PARTIAL):
                return doc
            logger.info(
                "[Parse] XBRL parse degraded for %s — falling back to PDF",
                verified.raw.symbol,
            )

    if verified.pdf_url:
        content = fetch_attachment(verified.pdf_url)
        if content:
            doc = parse_pdf(content)
            if doc.status == ParseStatus.PARTIAL and verified.xbrl_url:
                doc.status = ParseStatus.XBRL_FALLBACK_PDF
            if doc.status in (ParseStatus.SUCCESS, ParseStatus.PARTIAL, ParseStatus.XBRL_FALLBACK_PDF):
                return doc

    return parse_heuristic(
        verified.official_subject or verified.raw.headline,
        verified.official_category or verified.raw.category,
    )


def _classify_worker(
    cfg: EventIntelConfig,
    in_queue: _queue.Queue,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            verified: VerifiedEvent = in_queue.get(timeout=1.0)
        except _queue.Empty:
            continue

        try:
            parsed = _parse_document(verified)
            signal_record = classify(verified, parsed)
            append_record("earnings_signals", signal_record, data_dir=cfg.data_dir)

            # Schedule artifact for board-meeting intimations.
            if signal_record.event_type == "BOARD_MEETING":
                append_record("earnings_schedule", signal_record, data_dir=cfg.data_dir)

            # Shadow-route every material classified event.
            if signal_record.material:
                shadow_route(signal_record, data_dir=cfg.data_dir)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "[Classify] worker error for %s: %s", verified.raw.symbol, e
            )


def _heartbeat_worker(
    cfg: EventIntelConfig, stop_event: threading.Event
) -> None:
    while not stop_event.wait(timeout=30.0):
        try:
            write_heartbeat(cfg.data_dir)
        except OSError as e:
            logger.warning("[Heartbeat] write failed: %s", e)


# ── Producer selection (Plan B) ─────────────────────────────────────────────

def _build_producers(cfg: EventIntelConfig, raw_q: _queue.Queue) -> list:
    """Pick producer(s) by cfg.source_mode.

    Returns a list of objects sharing the start/stop/is_alive surface.
    The verifier short-circuits non-TRUEDATA records via its
    `getattr(raw, "source", "TRUEDATA") != "TRUEDATA"` branch, so dual
    mode is just two producers writing to the same queue — the
    canonical event_id makes cross-source duplicates collapse.
    """
    mode = (cfg.source_mode or "official").lower()
    if mode == "truedata":
        return [TrueDataClient(cfg, out_queue=raw_q)]
    if mode == "official":
        return [OfficialClient(cfg, out_queue=raw_q)]
    if mode == "dual":
        return [
            OfficialClient(cfg, out_queue=raw_q),
            TrueDataClient(cfg, out_queue=raw_q),
        ]
    logger.warning(
        "[Main] unknown source_mode=%s — defaulting to official", mode
    )
    return [OfficialClient(cfg, out_queue=raw_q)]


# ── Main loop ───────────────────────────────────────────────────────────────

def run(cfg: Optional[EventIntelConfig] = None) -> None:
    cfg = cfg or load_config()
    _setup_logging(cfg)
    logger.info(
        "[Main] starting event-intelligence pipeline "
        "(source_mode=%s, shadow_only=%s)",
        cfg.source_mode, cfg.shadow_only,
    )

    raw_q: _queue.Queue = _queue.Queue(maxsize=cfg.queue_max_items)
    verified_q: _queue.Queue = _queue.Queue(maxsize=cfg.queue_max_items)

    stop_event = threading.Event()

    producers = _build_producers(cfg, raw_q)
    verifier = Verifier(cfg, in_queue=raw_q, out_queue=verified_q)

    classify_thread = threading.Thread(
        target=_classify_worker,
        name="event-classify",
        args=(cfg, verified_q, stop_event),
        daemon=True,
    )
    heartbeat_thread = threading.Thread(
        target=_heartbeat_worker,
        name="event-heartbeat",
        args=(cfg, stop_event),
        daemon=True,
    )

    for p in producers:
        p.start()
    verifier.start()
    classify_thread.start()
    heartbeat_thread.start()

    # SIGTERM → graceful shutdown.
    def _on_signal(signum: int, _frame) -> None:
        logger.info("[Main] received signal %s — shutting down", signum)
        stop_event.set()
        for p in producers:
            p.stop()
        verifier.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        while not stop_event.is_set():
            # Watchdog: if any thread dies unexpectedly, exit non-zero so
            # systemd restarts us.
            for p in producers:
                if not p.is_alive():
                    logger.error(
                        "[Main] producer %s died — exiting for systemd restart",
                        type(p).__name__,
                    )
                    stop_event.set()
                    break
            if stop_event.is_set():
                break
            if not verifier.is_alive():
                logger.error("[Main] Verifier thread died — exiting for systemd restart")
                stop_event.set()
                break
            if not classify_thread.is_alive():
                logger.error("[Main] Classify thread died — exiting for systemd restart")
                stop_event.set()
                break
            time.sleep(5.0)
    finally:
        stop_event.set()
        for p in producers:
            p.stop()
        verifier.stop()
        logger.info("[Main] shutdown complete")


if __name__ == "__main__":
    run()
