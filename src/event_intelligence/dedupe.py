"""Deterministic event-ID hashing and persistent seen-set.

Two flavors of event_id:

  - compute_event_id(...|source_id)            — per-source replay key.
    Lets a single source's audit log distinguish two filings that
    share symbol+date but came from different vendor IDs.

  - compute_canonical_event_id(...|headline)   — cross-source dedupe key.
    NSE, BSE, and TrueData of the same real-world announcement all
    hash to the same value. Used for the seen-set so dual-mode
    collapses to one downstream record.

The seen-set survives restarts via `data/event_intel_seen_YYYY-MM-DD.json`,
loaded at startup, written atomically on every new event.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from typing import Optional, Set
from zoneinfo import ZoneInfo

from src.data_ingestion.exchange_filings import _normalise_dedup_subject

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_SEEN_LOCK = threading.Lock()


def compute_event_id(
    symbol: str,
    event_type: str,
    filed_at_iso: str,
    source_id: Optional[str] = None,
) -> str:
    """Build a stable 16-char hex event_id keyed on source identifier.

    Per-source replay key. The date portion of `filed_at_iso` is used (not
    the time) so the same announcement at slightly different second-level
    timestamps still hashes equal within one source. For cross-source
    dedupe use `compute_canonical_event_id` instead.
    """
    if not symbol or not event_type or not filed_at_iso:
        raise ValueError("symbol, event_type, filed_at_iso are required")

    # Extract YYYY-MM-DD portion. Handle both with-offset and naive ISO.
    date_part = filed_at_iso[:10]

    blob = f"{symbol.upper()}|{event_type.upper()}|{date_part}|{source_id or ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def compute_canonical_event_id(
    symbol: str,
    event_type: str,
    filed_at_iso: str,
    headline: str,
) -> str:
    """Cross-source canonical event_id (Plan B).

    Hash basis: symbol | event_type | date(filed_at) | normalised_subject_60.
    The same announcement on NSE + BSE + TrueData collapses to one ID
    because none of those terms depend on the source identifier.

    Corrigenda/amendments collapse onto the original because
    `_normalise_dedup_subject` (reused from exchange_filings) strips
    CORRIGENDUM/ADDENDUM/AMENDMENT/REVISED/REVISION/UPDATE before
    truncating to 60 chars.
    """
    if not symbol or not event_type or not filed_at_iso:
        raise ValueError("symbol, event_type, filed_at_iso are required")
    date_part = filed_at_iso[:10]
    norm_subject = _normalise_dedup_subject(headline or "")
    blob = f"{symbol.upper()}|{event_type.upper()}|{date_part}|{norm_subject}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _seen_path(data_dir: str = "data") -> str:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(data_dir, f"event_intel_seen_{today}.json")


def load_seen_set(data_dir: str = "data") -> Set[str]:
    """Load today's seen-event IDs from disk. Returns empty set if absent."""
    path = _seen_path(data_dir)
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            payload = json.load(f)
        return set(payload.get("event_ids", []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[Dedupe] Failed to load seen-set %s: %s", path, e)
        return set()


def persist_seen_set(seen: Set[str], data_dir: str = "data") -> None:
    """Atomically rewrite the on-disk seen-set."""
    os.makedirs(data_dir, exist_ok=True)
    path = _seen_path(data_dir)
    tmp = path + ".tmp"
    payload = {"event_ids": sorted(seen), "count": len(seen)}
    try:
        with _SEEN_LOCK:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
    except OSError as e:
        logger.error("[Dedupe] Failed to persist seen-set %s: %s", path, e)


def mark_seen(event_id: str, seen: Set[str], data_dir: str = "data") -> bool:
    """Add `event_id` to the seen-set and persist. Returns True if newly added."""
    if event_id in seen:
        return False
    seen.add(event_id)
    persist_seen_set(seen, data_dir)
    return True
