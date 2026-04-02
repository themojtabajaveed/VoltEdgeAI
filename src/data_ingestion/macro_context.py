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

The runner calls `refresh_macro_context()` periodically (every 90 min)
and the scorer/catalyst analyzer can read the cached context.

v2 (2026-04-01): Direction-aware tiered risk system.
  RISK-OFF dampens LONG, boosts SHORT. RISK-ON does the opposite.
  Tiers are based on FII flow magnitude vs 7-day rolling average,
  NOT fixed thresholds — adapts to market conditions automatically.
"""
import json
import logging
import os
from datetime import datetime, date
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

FII_HISTORY_PATH = "data/fii_history.json"
FII_HISTORY_MAX_ENTRIES = 30


# Lazy imports to avoid circular dependencies
def _import_finnhub():
    from src.data_ingestion.finnhub_client import get_macro_risk_signal, fetch_global_sentiment
    return get_macro_risk_signal, fetch_global_sentiment

def _import_nse():
    from src.data_ingestion.nse_scraper import get_institutional_signal, fetch_bulk_block_deals
    return get_institutional_signal, fetch_bulk_block_deals


# ── Tiered Risk System ────────────────────────────────────────────────────

class MacroRiskTier(str, Enum):
    """
    5-tier macro risk classification.
    Tier determines direction-specific conviction adjustments.
    """
    RISK_ON  = "RISK_ON"    # FII buying, positive macro → favor LONG
    CLEAR    = "CLEAR"      # No significant macro signal → neutral
    CAUTION  = "CAUTION"    # Mild stress → slight LONG dampener
    RISK_OFF = "RISK_OFF"   # Significant stress → strong LONG dampener, SHORT boost
    EXTREME  = "EXTREME"    # Crisis-level → full LONG halt, SHORT still ok


# Direction-aware dampener table: (points_adjustment, min_conviction_to_trade)
# Positive adjustment = boost, Negative = dampen
TIER_DAMPENERS: Dict[MacroRiskTier, Dict[str, Tuple[int, int]]] = {
    MacroRiskTier.RISK_ON: {
        "LONG":  (+10, 60),   # Boost LONG, lower bar
        "SHORT": (-15, 80),   # Dampen SHORT, higher bar
        "BUY":   (+10, 60),   # Alias
    },
    MacroRiskTier.CLEAR: {
        "LONG":  (0, 70),     # Neutral
        "SHORT": (0, 70),
        "BUY":   (0, 70),
    },
    MacroRiskTier.CAUTION: {
        "LONG":  (-10, 65),   # Slight dampener
        "SHORT": (+5, 60),    # Slight boost
        "BUY":   (-10, 65),
    },
    MacroRiskTier.RISK_OFF: {
        "LONG":  (-20, 75),   # Strong dampener — only high-conviction LONG survives
        "SHORT": (+10, 60),   # Boost — risk-off is SHORT-friendly
        "BUY":   (-20, 75),
    },
    MacroRiskTier.EXTREME: {
        "LONG":  (-999, 999), # Full halt — no LONG trades
        "SHORT": (+15, 65),   # Strong boost — best SHORT environment
        "BUY":   (-999, 999),
    },
}


# ── FII History for Rolling Averages ──────────────────────────────────────

def _load_fii_history() -> List[Dict]:
    """Load FII flow history from disk. Returns list of {date, fii_net_cr}."""
    if os.path.exists(FII_HISTORY_PATH):
        try:
            with open(FII_HISTORY_PATH) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data[-FII_HISTORY_MAX_ENTRIES:]
        except Exception:
            pass
    return []


def _save_fii_history(history: List[Dict]) -> None:
    """Persist FII history to disk."""
    os.makedirs("data", exist_ok=True)
    trimmed = history[-FII_HISTORY_MAX_ENTRIES:]
    with open(FII_HISTORY_PATH, "w") as f:
        json.dump(trimmed, f, indent=2)


def _record_fii_flow(fii_net_cr: float) -> None:
    """Add today's FII flow to history (one entry per date)."""
    today_str = str(date.today())
    history = _load_fii_history()

    # Update existing entry for today or append new
    for entry in history:
        if entry.get("date") == today_str:
            entry["fii_net_cr"] = fii_net_cr
            _save_fii_history(history)
            return

    history.append({"date": today_str, "fii_net_cr": fii_net_cr})
    _save_fii_history(history)


def _compute_fii_7d_avg() -> Optional[float]:
    """Compute 7-day average of FII net flows. Returns None if insufficient data."""
    history = _load_fii_history()
    if len(history) < 3:
        # Need at least 3 days of data for a meaningful average
        return None
    recent = history[-7:]  # Last 7 entries (or fewer if not enough)
    values = [e["fii_net_cr"] for e in recent if "fii_net_cr" in e]
    if not values:
        return None
    return sum(values) / len(values)


# ── MacroContext Dataclass ────────────────────────────────────────────────

@dataclass
class MacroContext:
    """Single snapshot of all macro intelligence at a point in time."""
    timestamp: datetime = field(default_factory=datetime.now)

    # Commodities / Forex
    crude_change_pct: float = 0.0
    gold_change_pct: float = 0.0
    usd_inr_change_pct: float = 0.0
    dxy_change_pct: float = 0.0
    dxy_price: float = 0.0
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

    # v2: Tier system
    _risk_tier: Optional[MacroRiskTier] = field(default=None, repr=False)
    _fii_7d_avg: Optional[float] = field(default=None, repr=False)
    _fii_ratio: Optional[float] = field(default=None, repr=False)
    _nifty_open_change_pct: float = 0.0    # Set by runner after first Nifty tick
    _circuit_breaker_active: bool = False   # Set by runner if index circuit hit

    # v3: Composite pre-market intelligence score
    _composite_score: Optional[int] = field(default=None, repr=False)
    _composite_intelligence: Optional[object] = field(default=None, repr=False)  # PreMarketIntelligence

    def is_risk_off(self) -> bool:
        """True if macro conditions suggest caution. Backward-compatible."""
        tier = self.get_risk_tier()
        return tier in (MacroRiskTier.CAUTION, MacroRiskTier.RISK_OFF, MacroRiskTier.EXTREME)

    def set_composite_intelligence(self, intel) -> None:
        """Inject pre-market intelligence into macro context.
        Called by runner after compute_pre_market_intelligence()."""
        self._composite_intelligence = intel
        self._composite_score = intel.composite_score if intel else None
        # Reset cached tier so it gets recomputed with composite data
        self._risk_tier = None

    def get_risk_tier(self) -> MacroRiskTier:
        """
        Compute the macro risk tier.

        v3 logic (priority order):
          1. Circuit breaker → EXTREME (override everything)
          2. Nifty gap down >2% at open → EXTREME
          3. Composite pre-market score (if available) → tier from score
          4. FALLBACK: FII-ratio logic (if composite unavailable)

        The composite score integrates US markets, crude, DXY, FII/DII,
        VIX, and PCR into a single 0-100 score. When available, it
        replaces the old FII-only tier determination.
        """
        if self._risk_tier is not None:
            return self._risk_tier

        # Circuit breaker override — all bets off
        if self._circuit_breaker_active:
            self._risk_tier = MacroRiskTier.EXTREME
            return self._risk_tier

        # Nifty opening gap check
        if self._nifty_open_change_pct <= -2.0:
            self._risk_tier = MacroRiskTier.EXTREME
            return self._risk_tier

        # ── v3: Composite score path (primary) ────────────────────────
        if self._composite_score is not None:
            score = self._composite_score
            if score >= 70:
                self._risk_tier = MacroRiskTier.RISK_ON
            elif score >= 55:
                self._risk_tier = MacroRiskTier.CLEAR
            elif score >= 40:
                self._risk_tier = MacroRiskTier.CAUTION
            elif score >= 25:
                self._risk_tier = MacroRiskTier.RISK_OFF
            else:
                self._risk_tier = MacroRiskTier.EXTREME
            return self._risk_tier

        # ── v2 FALLBACK: FII-ratio logic (when composite unavailable) ──
        logger.info(
            "[PreMkt] Composite unavailable — falling back to FII-ratio "
            "logic. Reason: pre-market intelligence not computed or all signals failed"
        )

        fii = self.fii_net_cr  # Negative = selling
        avg_7d = self._fii_7d_avg if self._fii_7d_avg is not None else _compute_fii_7d_avg()
        self._fii_7d_avg = avg_7d

        # Compute FII ratio vs 7-day average
        if avg_7d is not None and avg_7d < 0:
            self._fii_ratio = abs(fii) / abs(avg_7d) if fii < 0 else 0.0
        elif avg_7d is not None and avg_7d >= 0 and fii < 0:
            self._fii_ratio = 2.0
        else:
            self._fii_ratio = None

        # Determine tier from FII ratio
        if self._fii_ratio is not None and fii < 0:
            ratio = self._fii_ratio
            if ratio >= 3.0:
                self._risk_tier = MacroRiskTier.EXTREME
            elif ratio >= 1.5:
                self._risk_tier = MacroRiskTier.RISK_OFF
            elif ratio >= 1.0:
                self._risk_tier = MacroRiskTier.CAUTION
            else:
                self._risk_tier = MacroRiskTier.CAUTION if fii < -500 else MacroRiskTier.CLEAR
        elif self._fii_ratio is None and fii < 0:
            if fii < -12000:
                self._risk_tier = MacroRiskTier.EXTREME
            elif fii < -5000:
                self._risk_tier = MacroRiskTier.RISK_OFF
            elif fii < -2000:
                self._risk_tier = MacroRiskTier.CAUTION
            else:
                self._risk_tier = MacroRiskTier.CLEAR
        elif fii > 500 and self.macro_bias == "risk_on":
            self._risk_tier = MacroRiskTier.RISK_ON
        elif fii > 0 and self.institutional_signal == "bullish":
            self._risk_tier = MacroRiskTier.RISK_ON
        else:
            self._risk_tier = MacroRiskTier.CLEAR

        # Commodity stress can escalate tier by one level
        commodity_stress = (
            (self.macro_bias == "risk_off")
            and self._risk_tier in (MacroRiskTier.CLEAR, MacroRiskTier.CAUTION)
        )
        if commodity_stress:
            if self._risk_tier == MacroRiskTier.CLEAR:
                self._risk_tier = MacroRiskTier.CAUTION
            elif self._risk_tier == MacroRiskTier.CAUTION:
                self._risk_tier = MacroRiskTier.RISK_OFF

        return self._risk_tier

    def get_direction_dampener(self, direction: str) -> Tuple[int, int]:
        """
        Get (points_adjustment, min_conviction) for a given trade direction.

        Args:
            direction: "LONG", "SHORT", or "BUY"

        Returns:
            (adjustment_points, minimum_conviction_to_trade)
            adjustment_points: positive = boost, negative = dampen
            Add this to the raw conviction score.

        Example:
            dampener, min_conv = macro_ctx.get_direction_dampener("LONG")
            adjusted_score = raw_score + dampener
            if adjusted_score < min_conv: skip
        """
        tier = self.get_risk_tier()

        # EXTREME tier with circuit breaker = halt ALL trades
        if tier == MacroRiskTier.EXTREME and self._circuit_breaker_active:
            return (-999, 999)  # Block everything — liquidity risk too high

        tier_rules = TIER_DAMPENERS.get(tier, TIER_DAMPENERS[MacroRiskTier.CLEAR])
        # Normalize direction
        d = direction.upper()
        if d == "BUY":
            d = "LONG"
        return tier_rules.get(d, (0, 70))

    def get_score_modifier(self) -> float:
        """
        DEPRECATED — backward-compatible multiplicative modifier.
        New code should use get_direction_dampener() instead.
        Kept so any old code paths don't break.
        """
        tier = self.get_risk_tier()
        if tier == MacroRiskTier.EXTREME:
            return 0.7
        elif tier == MacroRiskTier.RISK_OFF:
            return 0.85
        elif tier == MacroRiskTier.CAUTION:
            return 0.90
        elif tier == MacroRiskTier.RISK_ON:
            return 1.10
        return 1.0

    def format_tier_log(self) -> str:
        """
        Single audit line for journalctl — complete regime decision visibility.

        Example:
          [MacroRisk] Tier-2 RISK_OFF | FII=-11163Cr | 7d_avg=-4200Cr
          | ratio=2.66x | LONG: -20pts (min 75) | SHORT: +10pts (min 60)
        """
        tier = self.get_risk_tier()
        tier_num = {
            MacroRiskTier.RISK_ON: "R+",
            MacroRiskTier.CLEAR: "0",
            MacroRiskTier.CAUTION: "1",
            MacroRiskTier.RISK_OFF: "2",
            MacroRiskTier.EXTREME: "3",
        }.get(tier, "?")

        avg_str = f"{self._fii_7d_avg:+.0f}Cr" if self._fii_7d_avg is not None else "N/A"
        ratio_str = f"{self._fii_ratio:.2f}x" if self._fii_ratio is not None else "N/A"

        long_adj, long_min = self.get_direction_dampener("LONG")
        short_adj, short_min = self.get_direction_dampener("SHORT")

        long_str = "HALT" if long_adj <= -999 else f"{long_adj:+d}pts (min {long_min})"
        short_str = "HALT" if short_adj <= -999 else f"{short_adj:+d}pts (min {short_min})"

        composite_str = f"composite={self._composite_score}/100 | " if self._composite_score is not None else ""

        return (
            f"[MacroRisk] Tier-{tier_num} {tier.value} | {composite_str}"
            f"FII={self.fii_net_cr:+.0f}Cr | 7d_avg={avg_str} | ratio={ratio_str} | "
            f"LONG: {long_str} | SHORT: {short_str}"
        )

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
_last_values: Optional[Dict] = None  # For staleness detection
REFRESH_INTERVAL_SECONDS = 5400  # 90 minutes (was 7200)


def refresh_macro_context(force: bool = False) -> MacroContext:
    """
    Refresh all macro data sources. Caches for 90 minutes.
    Call this from the runner on every cycle — it handles caching internally.
    """
    global _cached_context, _last_refresh, _last_values

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
                ctx.dxy_price = q.price

                # DXY sanity validation (P0-3)
                if q.price > 0 and (q.price < 70 or q.price > 130):
                    logger.error(
                        f"[Macro] DXY value out of valid range: {q.price:.2f} — discarding. "
                        f"Expected 70-130 for USD index / 20-35 for UUP ETF proxy."
                    )
                    ctx.dxy_change_pct = 0.0  # Don't let bad data influence regime

        # Staleness detection
        new_values = {name: q.price for name, q in quotes.items()}
        if _last_values is not None and new_values == _last_values:
            stale_minutes = (now - _last_refresh).total_seconds() / 60 if _last_refresh else 0
            if stale_minutes >= 90:
                logger.warning(
                    f"[Macro] All macro values unchanged for {stale_minutes:.0f}+ min — "
                    f"possible stale cache. Values: {new_values}"
                )
        _last_values = new_values

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

        # Record FII flow for rolling average computation
        if ctx.fii_net_cr != 0:
            _record_fii_flow(ctx.fii_net_cr)

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
    logger.info(ctx.format_tier_log())
    return ctx


def get_cached_context() -> MacroContext:
    """Get the current cached macro context (or refresh if none exists)."""
    if _cached_context is None:
        return refresh_macro_context()
    return _cached_context
