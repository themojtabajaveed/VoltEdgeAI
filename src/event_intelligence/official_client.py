"""Official-source orchestrator (Plan B Phase B3).

Owns one `NSEPoller` and (optionally) one `BSEPoller`. Mirrors the
public surface of `TrueDataClient` so `main.py` swaps producers via
the `source_mode` config flag without touching wiring.

When `source_mode == "official"`, this is the only producer. When
`source_mode == "dual"`, this runs alongside `TrueDataClient`; both
feed the same raw queue and the canonical event_id (Phase B0)
collapses cross-source duplicates downstream.
"""

from __future__ import annotations

import logging
import queue as _queue
from typing import Any, Dict, Optional

from src.event_intelligence.bse_poller import BSEPoller
from src.event_intelligence.config import EventIntelConfig
from src.event_intelligence.nse_poller import NSEPoller

logger = logging.getLogger(__name__)


class OfficialClient:
    """Composite producer: NSE + (optionally) BSE, single public surface.

    BSE is opt-in via cfg.bse_enabled (default True). On geo-restricted
    hosts where BSE is empty for hours, the BSEPoller's own degradation
    handles the situation; OfficialClient.is_alive() returns True as
    long as the NSE thread is healthy.
    """

    def __init__(
        self,
        cfg: EventIntelConfig,
        out_queue: _queue.Queue,
    ) -> None:
        self._cfg = cfg
        self._out_queue = out_queue
        self._nse = NSEPoller(cfg, out_queue)
        self._bse: Optional[BSEPoller] = (
            BSEPoller(cfg, out_queue) if getattr(cfg, "bse_enabled", True) else None
        )

    def start(self) -> None:
        self._nse.start()
        if self._bse is not None:
            self._bse.start()
        logger.info(
            "[OfficialClient] started (nse=%s, bse=%s)",
            self._nse.is_alive(),
            self._bse.is_alive() if self._bse is not None else False,
        )

    def stop(self, timeout_s: float = 5.0) -> None:
        self._nse.stop(timeout_s)
        if self._bse is not None:
            self._bse.stop(timeout_s)

    def is_alive(self) -> bool:
        # NSE is mandatory; BSE optional.
        return self._nse.is_alive()

    def feed_synthetic(self, payload: Dict[str, Any]) -> None:
        """Route a synthetic dict to the appropriate poller for tests.

        Expects payload to contain `exchange` ("NSE" or "BSE") plus the
        FilingEvent constructor kwargs. This is a thin testing affordance
        that mirrors `TrueDataClient.feed_synthetic` semantics.
        """
        from src.data_ingestion.exchange_filings import FilingEvent
        exchange = (payload.get("exchange") or "NSE").upper()
        # Build a FilingEvent. All optional fields are defaulted.
        filing = FilingEvent(
            symbol=payload["symbol"],
            company_name=payload.get("company_name", payload["symbol"]),
            headline=payload["headline"],
            category=payload.get("category", ""),
            filed_at=payload.get("filed_at"),
            exchange=exchange,
            urgency=float(payload.get("urgency", 5.0)),
            raw=payload.get("raw", payload),
        )
        if exchange == "NSE":
            self._nse.feed_synthetic(filing)
        elif exchange == "BSE" and self._bse is not None:
            self._bse.feed_synthetic(filing)
        else:
            logger.warning(
                "[OfficialClient] feed_synthetic: no poller for exchange=%s",
                exchange,
            )
