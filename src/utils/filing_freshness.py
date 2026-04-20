"""
filing_freshness.py — Filing freshness classification (Layers 1 + 2)
---------------------------------------------------------------------
Implements the first two layers of the 4-layer event evaluation model:

  Layer 1 (WHEN):      How many NSE market minutes elapsed since filing_ts?
  Layer 2 (HOW MUCH):  Did the stock already react beyond the staleness threshold?

Layer 3 (event quality) and Layer 4 (market confirmation) are in
separate modules and applied downstream.
"""
from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Optional

from src.utils.market_calendar import market_minutes_of_exposure

logger = logging.getLogger(__name__)

# ── Default thresholds (overridden by config dict at call site) ────────────────

_DEFAULTS: dict[str, float] = {
    "fresh_market_minutes_max": 30,
    "warm_market_minutes_max": 90,
    "price_reaction_fresh_pct": 1.0,
    "price_reaction_warm_pct": 2.0,
}


class FilingFreshness(Enum):
    """Freshness tier of a corporate filing relative to current market time."""
    PRISTINE = "PRISTINE"  # 0 market-min elapsed — maximally fresh
    FRESH    = "FRESH"     # 1–fresh_max market-min AND reaction < fresh_pct
    WARM     = "WARM"      # fresh_max+1–warm_max market-min AND reaction < warm_pct
    STALE    = "STALE"     # >warm_max market-min OR reaction exceeded threshold


def classify_filing_freshness(
    filing_ts: datetime,
    as_of: datetime,
    price_at_filing: Optional[float],
    price_now: float,
    config: dict,
) -> tuple[FilingFreshness, str]:
    """Classify a filing as PRISTINE / FRESH / WARM / STALE.

    Layer 1 uses market_minutes_of_exposure() to count NSE session minutes.
    Layer 2 checks price reaction — only for FRESH/WARM. PRISTINE is immune:
    a gap-up on a zero-exposure filing is the expected behaviour, not staleness.
    Layer 2 is skipped entirely when price_at_filing is None.

    Args:
        filing_ts:       Timestamp of the exchange filing (IST or naive→assumed IST).
        as_of:           Evaluation time, typically datetime.now(IST).
        price_at_filing: LTP at filing_ts, or previous close for pre-market filings.
                         None if unavailable — Layer 2 is skipped.
        price_now:       Current LTP.
        config:          Dict with keys: fresh_market_minutes_max,
                         warm_market_minutes_max, price_reaction_fresh_pct,
                         price_reaction_warm_pct.

    Returns:
        (FilingFreshness, reason_str) — reason_str feeds directly into [R4 L1/L2] log lines.
    """
    fresh_max = int(config.get("fresh_market_minutes_max", _DEFAULTS["fresh_market_minutes_max"]))
    warm_max  = int(config.get("warm_market_minutes_max",  _DEFAULTS["warm_market_minutes_max"]))
    fresh_pct = float(config.get("price_reaction_fresh_pct", _DEFAULTS["price_reaction_fresh_pct"]))
    warm_pct  = float(config.get("price_reaction_warm_pct",  _DEFAULTS["price_reaction_warm_pct"]))

    # ── Layer 1: market-time exposure ────────────────────────────────────────
    mkt_min = market_minutes_of_exposure(filing_ts, as_of)

    if mkt_min == 0:
        l1 = FilingFreshness.PRISTINE
    elif mkt_min <= fresh_max:
        l1 = FilingFreshness.FRESH
    elif mkt_min <= warm_max:
        l1 = FilingFreshness.WARM
    else:
        return (
            FilingFreshness.STALE,
            f"STALE (L1): {mkt_min} market-min > {warm_max} threshold",
        )

    # ── Layer 2: price reaction ───────────────────────────────────────────────
    # PRISTINE is immune: gap moves on zero-exposure filings are expected, not stale.
    # Skip if price_at_filing is unknown.
    if l1 != FilingFreshness.PRISTINE and price_at_filing is not None:
        reaction_pct = abs(price_now - price_at_filing) / price_at_filing * 100.0
        threshold = fresh_pct if l1 == FilingFreshness.FRESH else warm_pct
        if reaction_pct > threshold:
            return (
                FilingFreshness.STALE,
                f"STALE (L2): price reacted {reaction_pct:.1f}% > {threshold}% threshold "
                f"({l1.value} window, {mkt_min} market-min)",
            )
        return (
            l1,
            f"{l1.value}: {mkt_min} market-min, "
            f"reaction {reaction_pct:.1f}% < {threshold}% threshold",
        )

    # ── PRISTINE or no price data ─────────────────────────────────────────────
    if l1 == FilingFreshness.PRISTINE:
        if price_at_filing is not None:
            reaction_pct = abs(price_now - price_at_filing) / price_at_filing * 100.0
            reason = (
                f"PRISTINE: 0 market-min elapsed "
                f"(price reaction {reaction_pct:.1f}% — L2 immunity)"
            )
        else:
            reason = "PRISTINE: 0 market-min elapsed"
        return FilingFreshness.PRISTINE, reason

    # FRESH or WARM with no price data — Layer 2 skipped
    return l1, f"{l1.value}: {mkt_min} market-min (no price data — L2 skipped)"
