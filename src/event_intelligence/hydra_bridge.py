"""HYDRA Bridge — connect event-intelligence output to HYDRA watchlist.

Reads today's `earnings_signals_{date}.json`, filters for material +
high-urgency signals, and converts them to WatchlistEntry objects that
HYDRA can consume directly at scan time (08:15 IST).

This is the integration layer that makes the event-intelligence
pipeline's output actionable by the trading system.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from zoneinfo import ZoneInfo

from src.event_intelligence.models import EarningsSignal
from src.event_intelligence.priced_in import assess_priced_in
from src.strategies.base import WatchlistEntry

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Configurable thresholds (v1 defaults — tune from postmortem evidence)
_MIN_URGENCY = 7.0
_MIN_SOURCE_CONFIDENCE = 0.7
_MAX_SIGNAL_AGE_HOURS = 20  # reject signals older than this


def _load_todays_signals(data_dir: str = "data") -> List[dict]:
    """Load earnings_signals for today (IST date)."""
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    path = Path(data_dir) / f"earnings_signals_{today_ist}.json"

    if not path.exists():
        logger.debug("[HydraBridge] no signals file for %s", today_ist)
        return []

    try:
        with open(path) as f:
            records = json.load(f)
        if isinstance(records, list):
            return records
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[HydraBridge] failed to read %s: %s", path, e)
        return []


def _is_fresh(record: dict) -> bool:
    """Reject signals older than _MAX_SIGNAL_AGE_HOURS."""
    classified_at = record.get("classified_at")
    if not classified_at:
        return True  # no timestamp = assume fresh (defensive)
    try:
        ts = datetime.fromisoformat(classified_at)
        age = datetime.now(IST) - ts
        return age < timedelta(hours=_MAX_SIGNAL_AGE_HOURS)
    except (ValueError, TypeError):
        return True


def _signal_to_watchlist_entry(record: dict) -> Optional[WatchlistEntry]:
    """Convert a raw earnings_signals JSON record to a WatchlistEntry."""
    symbol = record.get("symbol", "")
    if not symbol:
        return None

    direction = record.get("direction_hint", "BUY")
    if direction not in ("BUY", "SHORT"):
        direction = "BUY" if direction != "SHORT" else "SHORT"

    urgency = float(record.get("urgency", 0.0))
    source_confidence = float(record.get("source_confidence", 1.0))
    effective_urgency = urgency * source_confidence

    event_type = record.get("event_type", "")
    event_subtype = record.get("event_subtype", "")
    summary = record.get("summary", f"{event_type}: {event_subtype}")

    catalyst_strength = "HIGH" if urgency >= 8.5 else "MEDIUM" if urgency >= 7.0 else "LOW"

    earnings = record.get("earnings") or {}
    surprise_pct = earnings.get("surprise_pct")

    return WatchlistEntry(
        symbol=symbol,
        direction=direction,
        event_summary=summary,
        urgency=effective_urgency,
        catalyst_strength=catalyst_strength,
        gap_pct=0.0,
        volume_signal="NORMAL",
        sector_momentum="NEUTRAL",
        technical_setup="NEUTRAL",
        filing_subject=summary,
        filing_category=record.get("dawn_category", "") or event_type,
        filing_urgency=effective_urgency,
        filed_at=record.get("filed_at"),
        deal_size_inr=None,
        earnings_surprise_pct=surprise_pct,
        conviction_modifier=_compute_conviction_modifier(record),
        metadata={
            "source": "event_intelligence",
            "event_id": record.get("event_id", ""),
            "event_type": event_type,
            "event_subtype": event_subtype,
            "parse_status": record.get("parse_status", ""),
            "source_confidence": source_confidence,
        },
    )


def _compute_conviction_modifier(record: dict) -> int:
    """Compute pre-packed conviction adjustment based on signal quality."""
    modifier = 0
    parse_status = record.get("parse_status", "HEURISTIC")
    urgency = float(record.get("urgency", 0.0))

    # Parse quality bonus: non-heuristic extractions get a boost
    # PDF_TABLE/PDF_REGEX are the current values; SUCCESS/PARTIAL are legacy compat.
    if parse_status in ("PDF_TABLE", "PDF_REGEX", "SUCCESS", "PARTIAL"):
        modifier += 8

    # High-urgency events from deep parsing
    if urgency >= 9.0 and parse_status != "HEURISTIC":
        modifier += 4

    # Source confidence modulation
    source_confidence = float(record.get("source_confidence", 1.0))
    if source_confidence >= 1.0:
        modifier += 3
    elif source_confidence < 0.7:
        modifier -= 5

    return modifier


def get_event_intel_signals(
    data_dir: str = "data",
    min_urgency: float = _MIN_URGENCY,
    min_source_confidence: float = _MIN_SOURCE_CONFIDENCE,
    apply_priced_in: bool = True,
) -> List[WatchlistEntry]:
    """Load and filter today's event intelligence signals for HYDRA consumption.

    Returns WatchlistEntry objects sorted by effective urgency (descending).
    Only returns material, fresh, high-urgency signals.

    When apply_priced_in=True (default), each signal's urgency is discounted
    by the priced-in assessment engine before the min_urgency gate. Signals
    that were above threshold on raw urgency but fall below after discount
    are excluded — this is intentional (they're stale information).
    """
    records = _load_todays_signals(data_dir)
    if not records:
        return []

    entries: List[WatchlistEntry] = []
    for record in records:
        if not record.get("material", False):
            continue

        source_confidence = float(record.get("source_confidence", 0.0))
        if source_confidence < min_source_confidence:
            continue
        if not _is_fresh(record):
            continue

        # Apply priced-in discount to urgency before threshold check.
        # Threshold uses raw_urgency × source_confidence × (1 - discount)
        # so that both quality and novelty factor into the gate decision.
        raw_urgency = float(record.get("urgency", 0.0))
        priced_in_discount = 0.0
        conflict_signal = False

        if apply_priced_in:
            try:
                assessment = assess_priced_in(record, data_dir=data_dir)
                priced_in_discount = assessment.priced_in_discount
                conflict_signal = assessment.conflict_signal
                # Inject assessment metadata into record for downstream use
                record["_priced_in_discount"] = priced_in_discount
                record["_effective_urgency"] = assessment.effective_urgency
                record["_novelty"] = assessment.novelty
                record["_conflict_signal"] = conflict_signal
            except Exception as e:
                logger.debug("[HydraBridge] priced-in check failed for %s: %s",
                            record.get("symbol"), e)

        gate_urgency = raw_urgency * source_confidence * (1.0 - priced_in_discount)

        if gate_urgency < min_urgency:
            continue

        entry = _signal_to_watchlist_entry(record)
        if entry:
            # Apply priced-in discount multiplicatively ON TOP of the
            # source_confidence that _signal_to_watchlist_entry already applied.
            # entry.urgency = raw × source_confidence (from _signal_to_watchlist_entry)
            # We want: raw × source_confidence × (1 - discount)
            if priced_in_discount > 0:
                entry.urgency = entry.urgency * (1.0 - priced_in_discount)
                entry.filing_urgency = entry.urgency
            # Tag conflict signals in metadata
            if conflict_signal and entry.metadata:
                entry.metadata["conflict_signal"] = True
            entries.append(entry)

    entries.sort(key=lambda e: e.urgency, reverse=True)

    if entries:
        logger.info(
            "[HydraBridge] loaded %d event-intel signals (top: %s urgency=%.1f)",
            len(entries), entries[0].symbol, entries[0].urgency,
        )

    return entries
