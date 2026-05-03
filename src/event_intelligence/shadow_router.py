"""Shadow router — runs the existing router in read-only mode against
EarningsSignal records and persists the would-be RouteDecision.

v1 is shadow-only: we never feed the live system. The output goes to
`data/event_intel_shadow_router_YYYY-MM-DD.json` for nightly postmortem.

We construct the minimum `WatchlistEntry` and `PreMarketSignals` that
the router needs from the EarningsSignal + adapted FilingEvent. Where
the router would normally read price/volume context from the
pre_market_data cache, we fetch the latest available cache and read
the symbol's row (best-effort — if absent, we synthesize neutral
defaults so the router can still execute its rule set).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from src.event_intelligence.adapter import to_filing_event
from src.event_intelligence.models import EarningsSignal
from src.event_intelligence.writers import append_record, daily_path

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def _load_premarket_signal(symbol: str, data_dir: str) -> Optional[Dict[str, Any]]:
    """Best-effort lookup of today's PreMarketSignal for `symbol`."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    path = os.path.join(data_dir, f"premarket_cache_{today}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[ShadowRouter] could not read premarket cache: %s", e)
        return None

    signals = payload.get("signals") or {}
    return signals.get(symbol.upper()) or signals.get(symbol)


def _route_with_safety(
    sig: EarningsSignal, data_dir: str
) -> Dict[str, Any]:
    """Run `route_candidate` against the adapted record. Catch any
    ImportError/AttributeError so a router-side schema drift doesn't
    take down the shadow pipeline."""
    try:
        from src.strategies.base import WatchlistEntry  # type: ignore
        from src.strategies.router import route_candidate  # type: ignore
        from src.data_ingestion.pre_market_data import PreMarketSignals  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.error("[ShadowRouter] import failed: %s — emitting stub decision", e)
        return _stub_decision(sig, reason=f"router import failed: {e}")

    fe = to_filing_event(sig)

    # Build a minimal WatchlistEntry. The router expects fields like
    # symbol, direction, source/strategy, conviction. The exact shape
    # changes as the codebase evolves; we attempt to construct the
    # broadest compatible version and degrade if the dataclass rejects
    # unknown kwargs.
    direction = sig.direction_hint if sig.direction_hint in ("BUY", "SHORT") else "BUY"
    entry_kwargs: Dict[str, Any] = {
        "symbol": sig.symbol,
        "direction": direction,
        "source": "EVENT_INTEL_V1",
        "strategy": "HYDRA",
    }
    try:
        entry = WatchlistEntry(**entry_kwargs)
    except TypeError:
        # Strip optional kwargs and retry minimal.
        try:
            entry = WatchlistEntry(symbol=sig.symbol, direction=direction)  # type: ignore[call-arg]
        except Exception as e:  # noqa: BLE001
            logger.warning("[ShadowRouter] WatchlistEntry construction failed: %s", e)
            return _stub_decision(sig, reason=f"WatchlistEntry: {e}")

    # PreMarketSignals: try to use today's cache row if available; else
    # synthesize neutral defaults so the router runs to completion.
    pm_dict = _load_premarket_signal(sig.symbol, data_dir) or {}
    pm_dict.setdefault("symbol", sig.symbol)
    pm_dict.setdefault("as_of", datetime.now(IST).isoformat())
    pm_dict.setdefault("filing_category", fe.category)
    pm_dict.setdefault("filing_urgency", fe.urgency)
    pm_dict.setdefault("filing_impact_score", fe.urgency)

    try:
        pm = PreMarketSignals(**pm_dict)  # type: ignore[arg-type]
    except TypeError:
        # The cache has fields we don't know about; filter to known.
        try:
            from dataclasses import fields as dc_fields
            allowed = {f.name for f in dc_fields(PreMarketSignals)}
            filtered = {k: v for k, v in pm_dict.items() if k in allowed}
            pm = PreMarketSignals(**filtered)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001
            logger.warning("[ShadowRouter] PreMarketSignals construction failed: %s", e)
            return _stub_decision(sig, reason=f"PreMarketSignals: {e}")

    try:
        decision = route_candidate(entry, pm)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ShadowRouter] route_candidate raised: %s", e)
        return _stub_decision(sig, reason=f"route_candidate: {e}")

    return _decision_to_dict(decision)


def _decision_to_dict(decision: Any) -> Dict[str, Any]:
    if is_dataclass(decision):
        return asdict(decision)
    if hasattr(decision, "__dict__"):
        return dict(decision.__dict__)
    return {"raw": str(decision)}


def _stub_decision(sig: EarningsSignal, reason: str) -> Dict[str, Any]:
    return {
        "symbol": sig.symbol,
        "route": "UNROUTED",
        "confidence": 0.0,
        "rules_passed": [],
        "rules_failed": ["shadow_stub"],
        "reasoning": reason,
    }


def shadow_route(sig: EarningsSignal, data_dir: str = "data") -> Dict[str, Any]:
    """Run the shadow route for one EarningsSignal and persist the decision."""
    decision = _route_with_safety(sig, data_dir)

    # Assess priced-in discount for audit trail (best-effort, never blocks routing)
    priced_in_discount = 0.0
    priced_in_novelty = "PRISTINE"
    try:
        from dataclasses import asdict as _asdict
        from src.event_intelligence.priced_in import assess_priced_in
        sig_dict = _asdict(sig)
        assessment = assess_priced_in(sig_dict, data_dir=data_dir, check_preopen=False)
        priced_in_discount = assessment.priced_in_discount
        priced_in_novelty = assessment.novelty
    except Exception:
        pass

    record = {
        "event_id": sig.event_id,
        "symbol": sig.symbol,
        "event_type": sig.event_type,
        "event_subtype": sig.event_subtype,
        "filed_at": sig.filed_at,
        "classified_at": sig.classified_at,
        "direction_hint": sig.direction_hint,
        "raw_urgency": sig.urgency,
        "priced_in_discount": priced_in_discount,
        "effective_urgency": sig.urgency * (1.0 - priced_in_discount),
        "novelty": priced_in_novelty,
        "source_confidence": sig.source_confidence,
        "dawn_category": sig.dawn_category,
        "decision": decision,
    }
    append_record("event_intel_shadow_router", record, data_dir=data_dir)
    return decision
