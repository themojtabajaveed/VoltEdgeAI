"""Backfill surprise_pct (and derived subtype/urgency/direction/material) for
signals classified before the quarterly DB was populated.

Usage:
    .venv/bin/python scripts/backfill_surprise.py
    .venv/bin/python scripts/backfill_surprise.py --dry-run
"""
from __future__ import annotations

import glob
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.models_quarterly import get_prior_quarters
from src.event_intelligence.classifier import (
    _bucket_results_subtype,
    _compute_surprise_pct,
    _urgency_from_subtype,
    _direction_from_subtype,
)
from src.event_intelligence.parsers.base import ParseStatus
from src.event_intelligence.writers import atomic_write_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# HYDRA eligibility thresholds (mirror hydra_bridge.py)
_MIN_URGENCY = 7.0
_MIN_CONFIDENCE = 0.7


class _FakeRaw:
    def __init__(self, filed_at: str, headline: str = ""):
        self.vendor_filed_at = filed_at
        self.headline = headline


class _FakeVerified:
    def __init__(self, filed_at: str, headline: str = ""):
        self.official_filed_at = filed_at
        self.official_subject = headline
        self.raw = _FakeRaw(filed_at, headline)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    files = sorted(glob.glob("data/earnings_signals_*.json"))

    stats = {"files": 0, "inspected": 0, "backfilled": 0, "skipped_no_priors": 0, "new_hydra": 0}

    for path in files:
        stats["files"] += 1
        with open(path) as f:
            signals = json.load(f)

        modified = False
        for sig in signals:
            if sig.get("event_type") != "FINANCIAL_RESULTS":
                continue
            earnings = sig.get("earnings")
            if not earnings or earnings.get("surprise_pct") is not None:
                continue
            pat = earnings.get("pat")
            if pat is None:
                continue

            stats["inspected"] += 1
            priors = get_prior_quarters(sig["symbol"], n=8)
            if len(priors) < 2:
                stats["skipped_no_priors"] += 1
                continue

            extracted = {"pat": pat, "revenue": earnings.get("revenue"), "eps": earnings.get("eps")}
            verified = _FakeVerified(sig.get("filed_at", ""), sig.get("summary", ""))
            surprise = _compute_surprise_pct(extracted, priors, verified)

            if surprise is None:
                stats["skipped_no_priors"] += 1
                continue

            # Cascade: recompute subtype, urgency, direction, material
            old_subtype = sig.get("event_subtype")
            old_urgency = sig.get("urgency")
            is_annual = "ANNUAL" in (old_subtype or "")
            new_subtype = _bucket_results_subtype(is_annual, surprise)
            parse_status = ParseStatus(sig.get("parse_status", "HEURISTIC"))
            new_urgency = _urgency_from_subtype(new_subtype, parse_status)
            new_direction = _direction_from_subtype(new_subtype)
            new_material = new_subtype not in ("QUARTERLY_INLINE", "OTHER")

            was_hydra = (old_urgency or 0) >= _MIN_URGENCY and sig.get("material")
            now_hydra = new_urgency >= _MIN_URGENCY and new_material and sig.get("source_confidence", 0) >= _MIN_CONFIDENCE

            earnings["surprise_pct"] = round(surprise, 2)
            sig["event_subtype"] = new_subtype
            sig["urgency"] = new_urgency
            sig["direction_hint"] = new_direction
            sig["material"] = new_material

            modified = True
            stats["backfilled"] += 1
            if now_hydra and not was_hydra:
                stats["new_hydra"] += 1

            logger.info(
                "  %s: surprise=%+.1f%% subtype=%s→%s urgency=%.1f→%.1f%s",
                sig["symbol"], surprise, old_subtype, new_subtype,
                old_urgency or 0, new_urgency,
                " [NEW HYDRA-ELIGIBLE]" if (now_hydra and not was_hydra) else "",
            )

        if modified and not dry_run:
            atomic_write_json(path, signals)
            logger.info("  → wrote %s", path)

    logger.info("─" * 50)
    logger.info("Files scanned:              %d", stats["files"])
    logger.info("Signals inspected:          %d", stats["inspected"])
    logger.info("Signals backfilled:         %d", stats["backfilled"])
    logger.info("Skipped (insufficient DB):  %d", stats["skipped_no_priors"])
    logger.info("Became HYDRA-eligible:      %d", stats["new_hydra"])
    if dry_run:
        logger.info("(DRY RUN — no files written)")


if __name__ == "__main__":
    main()
