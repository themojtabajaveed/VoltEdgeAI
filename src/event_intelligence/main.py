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


def _load_prior_quarters(symbol: str) -> list:
    """Best-effort load of prior quarterly results from DB."""
    try:
        from src.db.models_quarterly import get_prior_quarters
        return get_prior_quarters(symbol)
    except Exception:
        return []


def _store_quarterly_result(signal) -> None:
    """Auto-store classified FINANCIAL_RESULTS into quarterly DB.

    Converts extracted PAT/Revenue/EPS into crores and upserts.
    Best-effort: never raises.
    """
    try:
        from datetime import date as _date, datetime as _datetime
        from src.db.models_quarterly import upsert_quarter

        earnings = signal.earnings
        if not earnings or (earnings.pat is None and earnings.revenue is None):
            return

        # Determine quarter_end from filed_at
        filed_dt = _datetime.fromisoformat(signal.filed_at)
        month = filed_dt.month
        year = filed_dt.year

        # Map filing month to the quarter being reported
        if month in (4, 5, 6):
            quarter_end = _date(year, 3, 31)
            fq, fy = 4, year
        elif month in (7, 8, 9):
            quarter_end = _date(year, 6, 30)
            fq, fy = 1, year + 1
        elif month in (10, 11, 12):
            quarter_end = _date(year, 9, 30)
            fq, fy = 2, year + 1
        else:  # Jan, Feb, Mar
            quarter_end = _date(year - 1, 12, 31)
            fq, fy = 3, year

        # Unit normalization: earnings payload values come from Gemini (abs INR)
        # or PDF parser (lakhs). Use same anchor-based detection as classifier.
        # For auto-store, we skip if we can't determine unit reliably.
        from src.db.models_quarterly import get_prior_quarters as _gp
        prior = _gp(signal.symbol)
        same_q = [q for q in prior if q.get("fiscal_quarter") == fq and q.get("pat_cr")]

        pat_cr = None
        revenue_cr = None
        if earnings.pat is not None and same_q:
            anchor = same_q[-1]["pat_cr"]
            for divisor in (1.0, 100.0, 1e7):
                ratio = (earnings.pat / divisor) / anchor if anchor else 0
                if 0.05 <= abs(ratio) <= 20:
                    pat_cr = earnings.pat / divisor
                    break
        if earnings.revenue is not None and pat_cr is not None and earnings.pat:
            # Apply same divisor to revenue
            divisor_used = earnings.pat / pat_cr if pat_cr else 1
            revenue_cr = earnings.revenue / divisor_used if divisor_used else None

        upsert_quarter(
            symbol=signal.symbol,
            quarter_end=quarter_end,
            fiscal_quarter=fq,
            fiscal_year=fy,
            revenue_cr=revenue_cr,
            pat_cr=pat_cr,
            eps=earnings.eps,
            parse_status=signal.parse_status,
            source="pipeline",
        )
        logger.debug("[Classify] auto-stored quarterly result for %s Q%d FY%d", signal.symbol, fq, fy)
    except Exception as e:
        logger.warning("[Classify] auto-store failed for %s: %s", signal.symbol, e)


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

def _parse_nse_announcement_meta(content: bytes) -> dict:
    """Extract enrichment fields from NSE announcement-metadata XBRL.

    Returns a dict with keys: result_period, result_type, description.
    Never raises; returns {} on any error.
    This is metadata only — no financial numbers are present in this document.
    """
    from xml.etree import ElementTree as ET
    try:
        root = ET.fromstring(content)
        meta: dict = {}
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "ResultPeriod" and elem.text:
                meta["result_period"] = elem.text.strip()
            elif local == "ResultType" and elem.text:
                meta["result_type"] = elem.text.strip()
            elif local == "DescriptionOfAnnouncement" and elem.text:
                meta["description"] = elem.text.strip()[:200]
        return meta
    except Exception:
        return {}


def _parse_document(verified: VerifiedEvent, cfg: EventIntelConfig) -> ParsedDocument:
    """PDF primary for financial numbers; NSE announcement meta for enrichment only.

    Flow:
      1. Fetch NSE announcement-metadata XBRL (ResultPeriod, ResultType) if available.
         This is enrichment only — does NOT contain financial statement data.
      2. Fetch PDF and parse for financial numbers (primary source).
      3. Heuristic fallback if PDF unavailable or parse fails.

    parse_xbrl() is intentionally not called here: the NSE /api/xbrl/{seq_id}
    endpoint returns announcement metadata, not financial statements. When a
    financial-statement XBRL path is discovered, re-introduce parse_xbrl() here.
    """
    fetch_kwargs = dict(
        proxy_url=cfg.fetch_proxy_url,
        timeout=cfg.fetch_timeout_s,
        max_retries=cfg.fetch_max_retries,
        retry_delays=cfg.fetch_retry_delays,
        cache_dir=cfg.data_dir,
    )

    # Step 1: NSE announcement metadata (enrichment — no financial numbers).
    nse_meta: dict = {}
    if verified.nse_xbrl_meta_url:
        meta_content = fetch_attachment(verified.nse_xbrl_meta_url, **fetch_kwargs)
        if meta_content:
            nse_meta = _parse_nse_announcement_meta(meta_content)
            logger.info(
                "[Parse] %s NSE meta: period=%s type=%s",
                verified.raw.symbol,
                nse_meta.get("result_period", "?"),
                nse_meta.get("result_type", "?"),
            )
        else:
            logger.debug(
                "[Parse] %s NSE announcement meta unavailable — PDF-only path",
                verified.raw.symbol,
            )

    # Step 2: PDF is the primary source for PAT / Revenue / EPS.
    if verified.pdf_url:
        pdf_content = fetch_attachment(verified.pdf_url, **fetch_kwargs)
        if pdf_content:
            doc = parse_pdf(pdf_content)
            if nse_meta:
                doc.fields.update(nse_meta)  # inject period/type for classifier context
            if doc.status in (
                ParseStatus.PDF_TABLE, ParseStatus.PDF_REGEX,
                ParseStatus.SUCCESS, ParseStatus.PARTIAL,  # legacy compat
            ):
                logger.debug(
                    "[Parse] %s PDF parse status=%s fields=%s",
                    verified.raw.symbol, doc.status.value,
                    {k: v for k, v in doc.fields.items() if k in ("revenue", "pat", "eps")},
                )
                return doc

    # Step 3: heuristic fallback.
    logger.info("[Parse] %s falling back to heuristic (no PDF content)", verified.raw.symbol)
    doc = parse_heuristic(
        verified.official_subject or verified.raw.headline,
        verified.official_category or verified.raw.category,
    )
    if nse_meta:
        doc.fields.update(nse_meta)
    return doc


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
            parsed = _parse_document(verified, cfg)
            prior_quarters = _load_prior_quarters(verified.raw.symbol)
            signal_record = classify(verified, parsed, prior_quarters=prior_quarters, cfg=cfg)
            append_record("earnings_signals", signal_record, data_dir=cfg.data_dir)

            # Auto-store financial results into quarterly DB for future surprise computation.
            if signal_record.event_type == "FINANCIAL_RESULTS" and signal_record.earnings:
                _store_quarterly_result(signal_record)

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
    from dotenv import load_dotenv
    load_dotenv()

    cfg = cfg or load_config()
    _setup_logging(cfg)

    if not cfg.gemini_api_key:
        logger.error(
            "[Main] GEMINI_API_KEY is empty — classifier will degrade to "
            "heuristic-only (urgency capped at 5.0). Check .env or systemd Environment=."
        )

    logger.info(
        "[Main] starting event-intelligence pipeline "
        "(source_mode=%s, shadow_only=%s, gemini_key_loaded=%s)",
        cfg.source_mode, cfg.shadow_only, bool(cfg.gemini_api_key),
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
