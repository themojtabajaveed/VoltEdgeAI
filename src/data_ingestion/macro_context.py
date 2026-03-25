"""
macro_context.py
----------------
Unified macro intelligence layer. Pulls together all external data sources
and produces a single MacroContext that feeds into trading decisions.

Sources combined:
  1. Finnhub macro quotes (crude, gold, DXY, USD/INR)
  2. NSE FII/DII institutional flows
  3. NSE bulk/block deals (per-symbol)
  4. Market regime (Nifty/BankNifty sentiment from Kite)

The runner calls `refresh_macro_context()` periodically (every 2 hours)
and the scorer/catalyst analyzer can read the cached context.
"""
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy imports to avoid circular dependencies
def _import_finnhub():
    from src.data_ingestion.finnhub_client import get_macro_risk_signal, fetch_global_sentiment
    return get_macro_risk_signal, fetch_global_sentiment

def _import_nse():
    from src.data_ingestion.nse_scraper import get_institutional_signal, fetch_bulk_block_deals
    return get_institutional_signal, fetch_bulk_block_deals


@dataclass
class MacroContext:
    """Single snapshot of all macro intelligence at a point in time."""
    timestamp: datetime = field(default_factory=datetime.now)

    # Commodities / Forex
    crude_change_pct: float = 0.0
    gold_change_pct: float = 0.0
    usd_inr_change_pct: float = 0.0
    dxy_change_pct: float = 0.0
    macro_bias: str = "neutral"         # "risk_on" | "neutral" | "risk_off"
    macro_details: str = ""

    # FII/DII
    fii_net_cr: float = 0.0
    dii_net_cr: float = 0.0
    institutional_signal: str = "neutral"  # "bullish" | "neutral" | "bearish"
    institutional_summary: str = ""

    # Global news headlines (latest 5)
    global_headlines: List[str] = field(default_factory=list)

    # Bulk/block deals today
    deal_count: int = 0

    def is_risk_off(self) -> bool:
        """True if macro conditions suggest caution."""
        return self.macro_bias == "risk_off" or self.institutional_signal == "bearish"

    def get_score_modifier(self) -> float:
        """
        Returns a multiplier for technical scores based on macro conditions.
        risk_off: reduce scores by 20%  →  0.8
        risk_on:  boost scores by 10%   →  1.1
        neutral:  no change             →  1.0
        """
        if self.macro_bias == "risk_off" and self.institutional_signal == "bearish":
            return 0.7   # Very cautious — double risk-off
        elif self.macro_bias == "risk_off" or self.institutional_signal == "bearish":
            return 0.85  # Mild caution
        elif self.macro_bias == "risk_on" and self.institutional_signal == "bullish":
            return 1.15  # Strong conviction
        elif self.macro_bias == "risk_on" or self.institutional_signal == "bullish":
            return 1.05  # Mild boost
        return 1.0

    @property
    def summary(self) -> str:
        parts = []
        if self.macro_details:
            parts.append(f"Macro: {self.macro_bias} ({self.macro_details[:80]})")
        if self.institutional_summary:
            parts.append(f"Inst: {self.institutional_summary}")
        if self.deal_count > 0:
            parts.append(f"Deals: {self.deal_count} bulk/block today")
        return " | ".join(parts) if parts else "No macro data"


# ── Cached context ─────────────────────────────────────────────────────────

_cached_context: Optional[MacroContext] = None
_last_refresh: Optional[datetime] = None
REFRESH_INTERVAL_SECONDS = 7200  # 2 hours


def refresh_macro_context(force: bool = False) -> MacroContext:
    """
    Refresh all macro data sources. Caches for 2 hours.
    Call this from the runner on every cycle — it handles caching internally.
    """
    global _cached_context, _last_refresh

    now = datetime.now()
    if not force and _cached_context and _last_refresh:
        elapsed = (now - _last_refresh).total_seconds()
        if elapsed < REFRESH_INTERVAL_SECONDS:
            return _cached_context

    ctx = MacroContext(timestamp=now)

    # 1. Finnhub macro quotes
    try:
        get_macro_risk_signal, fetch_global_sentiment = _import_finnhub()
        macro = get_macro_risk_signal()
        ctx.macro_bias = macro.get("overall_macro_bias", "neutral")
        ctx.macro_details = macro.get("details", "")

        # Parse individual commodity changes from details
        from src.data_ingestion.finnhub_client import fetch_macro_quotes
        quotes = fetch_macro_quotes()
        for name, q in quotes.items():
            if "Crude" in name:
                ctx.crude_change_pct = q.change_pct
            elif "Gold" in name:
                ctx.gold_change_pct = q.change_pct
            elif "USD_INR" in q.symbol or "USD/INR" in name:
                ctx.usd_inr_change_pct = q.change_pct
            elif "DXY" in name:
                ctx.dxy_change_pct = q.change_pct

        # Global news headlines
        news = fetch_global_sentiment()
        if isinstance(news, list):
            ctx.global_headlines = [n.get("headline", "") for n in news[:5] if n.get("headline")]

    except Exception as e:
        logger.warning(f"Macro quotes fetch failed: {e}")

    # 2. NSE FII/DII
    try:
        get_institutional_signal, fetch_bulk_block_deals = _import_nse()
        inst = get_institutional_signal()
        ctx.fii_net_cr = inst.get("fii_net_cr", 0)
        ctx.dii_net_cr = inst.get("dii_net_cr", 0)
        ctx.institutional_signal = inst.get("signal", "neutral")
        ctx.institutional_summary = inst.get("summary", "")
    except Exception as e:
        logger.warning(f"FII/DII fetch failed: {e}")

    # 3. Bulk/block deals
    try:
        _, fetch_deals = _import_nse()
        deals = fetch_deals()
        ctx.deal_count = len(deals)
    except Exception as e:
        logger.warning(f"Bulk deals fetch failed: {e}")

    _cached_context = ctx
    _last_refresh = now

    logger.info(f"Macro context refreshed: {ctx.summary}")
    return ctx


def get_cached_context() -> MacroContext:
    """Get the current cached macro context (or refresh if none exists)."""
    if _cached_context is None:
        return refresh_macro_context()
    return _cached_context
