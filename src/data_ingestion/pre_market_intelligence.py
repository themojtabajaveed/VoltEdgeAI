"""
pre_market_intelligence.py — Forward-Looking Composite Pre-Market Score
-----------------------------------------------------------------------
Replaces the rearview-mirror FII-only regime logic with a multi-signal
composite score that predicts TODAY's market direction.

Signal tiers (by predictive power):
  A: US market close (SPY/QQQ via Finnhub)          weight ±18
  B: Crude oil, USD strength (via EUR/USD)           weight ±20
  C: India VIX, PCR                                  weight ±9
  D: FII/DII cash flows (yesterday — lowest weight)  weight ±11

Score: 0-100, baseline 50 (neutral).
  70-100 → RISK_ON    (global signals bullish)
  55-69  → CLEAR      (neutral to mildly positive)
  40-54  → CAUTION    (mixed or mildly negative)
  25-39  → RISK_OFF   (clearly bearish)
  0-24   → EXTREME    (crisis-level)

Fallback: if all signals fail, returns None → caller uses FII-ratio logic.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SignalContribution:
    """One signal's contribution to the composite score."""
    name: str
    value_str: str      # Human-readable value (e.g., "+1.2%", "₹-11163Cr")
    points: int         # Signed contribution to score
    available: bool     # Was data fetched successfully?
    label: str = ""     # Direction label for log (e.g., "🟢 Bullish")


@dataclass
class PreMarketIntelligence:
    """Complete pre-market composite assessment."""
    composite_score: int = 50
    signals: List[SignalContribution] = field(default_factory=list)
    signals_available: int = 0
    signals_total: int = 8

    @property
    def tier_name(self) -> str:
        s = self.composite_score
        if s >= 70:
            return "RISK_ON"
        elif s >= 55:
            return "CLEAR"
        elif s >= 40:
            return "CAUTION"
        elif s >= 25:
            return "RISK_OFF"
        return "EXTREME"

    def format_log_line(self) -> str:
        """Single audit line for journalctl — grep-friendly."""
        parts = []
        for sig in self.signals:
            if sig.available:
                parts.append(f"{sig.name}={sig.value_str}({sig.points:+d})")
        signals_str = " ".join(parts)
        return (
            f"[PreMkt] Score={self.composite_score}/100 → {self.tier_name} | "
            f"{signals_str} | Signals: {self.signals_available}/{self.signals_total} available"
        )

    def format_email_table(self) -> str:
        """Markdown table for email Section 0."""
        lines = [
            "## 0. VoltEdge Regime Intelligence (Machine-Generated)\n",
            "| Signal | Value | Δ% | Contribution | Direction |",
            "|--------|-------|-----|-------------|-----------|",
        ]
        for sig in self.signals:
            if sig.available:
                pts_str = f"{sig.points:+d}"
                lines.append(f"| {sig.name} | {sig.value_str} | — | {pts_str} | {sig.label} |")
            else:
                lines.append(f"| {sig.name} | N/A | — | 0 | ⚪ Unavailable |")

        unavailable = [s.name for s in self.signals if not s.available]
        unavail_str = f"⚠️ Signals unavailable: {', '.join(unavailable)} ({len(unavailable)}/{self.signals_total} missing)" if unavailable else "✅ All signals available"

        lines.append("")
        lines.append(f"**Composite Score: {self.composite_score}/100 → Tier: {self.tier_name}**")

        # Verdict
        if self.composite_score >= 70:
            verdict = "Global signals bullish. Favor LONG setups."
        elif self.composite_score >= 55:
            verdict = "Neutral-to-positive signals. Normal conviction thresholds."
        elif self.composite_score >= 40:
            verdict = "Mixed signals. Elevated conviction bar for new entries."
        elif self.composite_score >= 25:
            verdict = "Bearish signals dominate. Favor SHORT setups, dampen LONG."
        else:
            verdict = "Crisis-level bearish. Minimal exposure recommended."

        lines.append(f"**Verdict: {verdict}**")
        lines.append(f"\n{unavail_str}")
        lines.append("\n⚡ This section is machine-computed from live market data. All other sections are AI-generated analysis.")

        return "\n".join(lines)


# ── Signal Scoring Functions ──────────────────────────────────────────────

def _score_us_market(spy_change: Optional[float], qqq_change: Optional[float]) -> List[SignalContribution]:
    """Score US market close signal. SPY=primary, QQQ delta=secondary."""
    results = []

    if spy_change is not None:
        if spy_change > 1.0:
            pts = 12
        elif spy_change > 0.3:
            pts = 7
        elif spy_change > -0.3:
            pts = 0
        elif spy_change > -1.0:
            pts = -7
        else:
            pts = -12

        label = "🟢 Bullish" if pts > 0 else ("🔴 Bearish" if pts < 0 else "➖ Flat")
        results.append(SignalContribution(
            name="SPY", value_str=f"{spy_change:+.1f}%", points=pts,
            available=True, label=label,
        ))
    else:
        results.append(SignalContribution(name="SPY", value_str="N/A", points=0, available=False))

    if spy_change is not None and qqq_change is not None:
        delta = qqq_change - spy_change
        if delta > 0.5:
            pts = 3
        elif delta < -0.5:
            pts = -3
        else:
            pts = 0
        label = "🟢 Tech leads" if pts > 0 else ("🔴 Tech lags" if pts < 0 else "➖ Inline")
        results.append(SignalContribution(
            name="QQQ δ", value_str=f"{delta:+.1f}%", points=pts,
            available=True, label=label,
        ))
    else:
        results.append(SignalContribution(name="QQQ δ", value_str="N/A", points=0, available=False))

    return results


def _score_crude(crude_change: Optional[float]) -> SignalContribution:
    """Score crude oil signal. Crude up = bad for India (net importer)."""
    if crude_change is None:
        return SignalContribution(name="Crude", value_str="N/A", points=0, available=False)

    # INVERTED: crude down = positive for India
    if crude_change < -2.0:
        pts = 8
    elif crude_change < -0.5:
        pts = 4
    elif crude_change < 0.5:
        pts = 0
    elif crude_change < 2.0:
        pts = -4
    else:
        pts = -8

    label = "🟢 Positive for India" if pts > 0 else ("🔴 Negative for India" if pts < 0 else "➖ Flat")
    return SignalContribution(
        name="Crude", value_str=f"{crude_change:+.1f}%", points=pts,
        available=True, label=label,
    )


def _score_usd_strength(eur_usd_change: Optional[float]) -> SignalContribution:
    """Score USD strength via EUR/USD inverse. Dollar weak = good for EM."""
    if eur_usd_change is None:
        return SignalContribution(name="USD Strength", value_str="N/A", points=0, available=False)

    # EUR/USD up = dollar weakening = positive for India
    if eur_usd_change > 0.5:
        pts = 5
        label = "🟢 Dollar weak (EM inflow)"
        val = "weak"
    elif eur_usd_change < -0.5:
        pts = -5
        label = "🔴 Dollar strong (EM outflow)"
        val = "strong"
    else:
        pts = 0
        label = "➖ Flat"
        val = "flat"

    return SignalContribution(
        name="USD Strength (via EUR/USD)", value_str=val, points=pts,
        available=True, label=label,
    )


def _score_usd_inr(usd_inr_change: Optional[float]) -> SignalContribution:
    """Score USD/INR direction. Rupee weakening = FII outflow pressure."""
    if usd_inr_change is None:
        return SignalContribution(name="USD/INR", value_str="N/A", points=0, available=False)

    # USD/INR up = rupee weakening = negative
    if usd_inr_change > 0.3:
        pts = -3
        label = "🔴 Rupee weak"
    elif usd_inr_change < -0.3:
        pts = 3
        label = "🟢 Rupee strong"
    else:
        pts = 0
        label = "➖ Stable"

    return SignalContribution(
        name="USD/INR", value_str=f"{usd_inr_change:+.2f}%", points=pts,
        available=True, label=label,
    )


def _score_fii_dii(fii_net_cr: Optional[float], dii_net_cr: Optional[float]) -> List[SignalContribution]:
    """Score FII/DII cash flows. Now weighted ±8 max (was 100% of signal)."""
    results = []

    if fii_net_cr is not None:
        if fii_net_cr > 1000:
            pts = 6
        elif fii_net_cr > 500:
            pts = 3
        elif fii_net_cr > -500:
            pts = 0
        elif fii_net_cr > -1000:
            pts = -4
        else:
            pts = -6

        label = "🟢 FII buying" if pts > 0 else ("🔴 FII selling" if pts < 0 else "➖ Neutral")
        results.append(SignalContribution(
            name="FII Cash", value_str=f"₹{fii_net_cr:+.0f}Cr", points=pts,
            available=True, label=label,
        ))

        # DII offset: if DII absorbs >80% of FII selling, add buffer
        if fii_net_cr < -500 and dii_net_cr is not None and dii_net_cr > 0:
            absorption_pct = dii_net_cr / abs(fii_net_cr) * 100
            if absorption_pct >= 80:
                results.append(SignalContribution(
                    name="DII Offset", value_str=f"₹{dii_net_cr:+.0f}Cr ({absorption_pct:.0f}%)",
                    points=3, available=True, label="🟢 DII absorbing",
                ))
            else:
                results.append(SignalContribution(
                    name="DII Offset", value_str=f"₹{dii_net_cr:+.0f}Cr ({absorption_pct:.0f}%)",
                    points=0, available=True, label="➖ Partial offset",
                ))
        elif dii_net_cr is not None:
            results.append(SignalContribution(
                name="DII Offset", value_str=f"₹{dii_net_cr:+.0f}Cr",
                points=0, available=True, label="➖ N/A (no FII selling)",
            ))
    else:
        results.append(SignalContribution(name="FII Cash", value_str="N/A", points=0, available=False))

    return results


def _score_vix(vix_level: Optional[float]) -> SignalContribution:
    """Score India VIX. High VIX = fear = reduce size."""
    if vix_level is None:
        return SignalContribution(name="India VIX", value_str="N/A", points=0, available=False)

    if vix_level < 13:
        pts = 3
        label = "🟢 Calm market"
    elif vix_level <= 18:
        pts = 0
        label = "➖ Normal"
    elif vix_level <= 22:
        pts = -2
        label = "🟡 Elevated"
    else:
        pts = -4
        label = "🔴 High fear"

    return SignalContribution(
        name="India VIX", value_str=f"{vix_level:.1f}", points=pts,
        available=True, label=label,
    )


def _score_pcr(pcr: Optional[float]) -> SignalContribution:
    """Score Nifty PCR. Contrarian indicator."""
    if pcr is None:
        return SignalContribution(name="Nifty PCR", value_str="N/A", points=0, available=False)

    if pcr > 1.3:
        pts = 3
        label = "🟢 Contrarian bullish"
    elif pcr >= 0.7:
        pts = 0
        label = "➖ Balanced"
    else:
        pts = -3
        label = "🔴 Contrarian bearish"

    return SignalContribution(
        name="Nifty PCR", value_str=f"{pcr:.2f}", points=pts,
        available=True, label=label,
    )


# ── Main Composite Function ──────────────────────────────────────────────

def compute_pre_market_intelligence(
    spy_change: Optional[float] = None,
    qqq_change: Optional[float] = None,
    crude_change: Optional[float] = None,
    eur_usd_change: Optional[float] = None,
    usd_inr_change: Optional[float] = None,
    fii_net_cr: Optional[float] = None,
    dii_net_cr: Optional[float] = None,
    vix_level: Optional[float] = None,
    pcr: Optional[float] = None,
) -> Optional[PreMarketIntelligence]:
    """
    Compute the composite pre-market score from all available signals.

    All parameters are optional — unavailable signals contribute 0
    and the score gracefully degrades toward neutral (50).

    Returns PreMarketIntelligence or None if no signals at all.
    """
    signals: List[SignalContribution] = []

    # Tier A: US market (±18 max)
    signals.extend(_score_us_market(spy_change, qqq_change))

    # Tier B: Commodities + Currency (±20 max)
    signals.append(_score_crude(crude_change))
    signals.append(_score_usd_strength(eur_usd_change))
    signals.append(_score_usd_inr(usd_inr_change))

    # Tier C: Domestic sentiment (±9 max)
    signals.append(_score_vix(vix_level))
    signals.append(_score_pcr(pcr))

    # Tier D: Yesterday's flows (±11 max)
    signals.extend(_score_fii_dii(fii_net_cr, dii_net_cr))

    available_count = sum(1 for s in signals if s.available)

    if available_count == 0:
        logger.warning("[PreMkt] No signals available — cannot compute composite score")
        return None

    total_points = sum(s.points for s in signals)
    raw_score = 50 + total_points
    composite = max(0, min(100, raw_score))

    result = PreMarketIntelligence(
        composite_score=composite,
        signals=signals,
        signals_available=available_count,
        signals_total=len(signals),
    )

    logger.info(result.format_log_line())
    return result


def fetch_and_compute(
    kite_client=None,
    macro_context=None,
    pcr_data=None,
) -> Optional[PreMarketIntelligence]:
    """
    High-level convenience: fetch all data sources and compute score.

    Args:
        kite_client: KiteConnect instance for India VIX fetch
        macro_context: Existing MacroContext with FII/DII data
        pcr_data: Existing PCRData from pcr_tracker

    Returns:
        PreMarketIntelligence or None if total failure.
    """
    spy_change = None
    qqq_change = None
    crude_change = None
    eur_usd_change = None
    usd_inr_change = None

    # 1. Fetch US market + macro quotes from Finnhub
    try:
        from src.data_ingestion.finnhub_client import fetch_us_market_quotes, fetch_macro_quotes
        us_quotes = fetch_us_market_quotes()
        spy = us_quotes.get("S&P 500 (SPY)")
        qqq = us_quotes.get("Nasdaq 100 (QQQ)")
        if spy:
            spy_change = spy.change_pct
        if qqq:
            qqq_change = qqq.change_pct
    except Exception as e:
        logger.warning(f"[PreMkt] US market fetch failed: {e}")

    try:
        from src.data_ingestion.finnhub_client import fetch_macro_quotes
        macro_quotes = fetch_macro_quotes()
        crude = macro_quotes.get("Brent Crude (USD/bbl)")
        eur_usd = macro_quotes.get("EUR/USD")
        usd_inr = macro_quotes.get("USD/INR")
        if crude:
            crude_change = crude.change_pct
        if eur_usd:
            eur_usd_change = eur_usd.change_pct
        if usd_inr:
            usd_inr_change = usd_inr.change_pct
    except Exception as e:
        logger.warning(f"[PreMkt] Macro quotes fetch failed: {e}")

    # 2. FII/DII from existing macro_context or fresh fetch
    fii_net = None
    dii_net = None
    if macro_context:
        fii_net = macro_context.fii_net_cr if macro_context.fii_net_cr != 0 else None
        dii_net = macro_context.dii_net_cr if macro_context.dii_net_cr != 0 else None
    else:
        try:
            from src.data_ingestion.nse_scraper import get_institutional_signal
            inst = get_institutional_signal()
            fii_net = inst.get("fii_net_cr") or None
            dii_net = inst.get("dii_net_cr") or None
        except Exception as e:
            logger.warning(f"[PreMkt] FII/DII fetch failed: {e}")

    # 3. India VIX from Kite
    vix_level = None
    if kite_client:
        try:
            vix_data = kite_client.ltp("NSE:INDIA VIX")
            if "NSE:INDIA VIX" in vix_data:
                raw_vix = vix_data["NSE:INDIA VIX"]["last_price"]
                if 8 <= raw_vix <= 50:
                    vix_level = raw_vix
                else:
                    logger.warning(f"[PreMkt] India VIX out of valid range (8-50): {raw_vix}")
        except Exception as e:
            logger.warning(f"[PreMkt] India VIX fetch failed (Kite may not be ready): {e}")

    # 4. PCR from existing data
    pcr = None
    if pcr_data and hasattr(pcr_data, 'pcr'):
        pcr = pcr_data.pcr

    return compute_pre_market_intelligence(
        spy_change=spy_change,
        qqq_change=qqq_change,
        crude_change=crude_change,
        eur_usd_change=eur_usd_change,
        usd_inr_change=usd_inr_change,
        fii_net_cr=fii_net,
        dii_net_cr=dii_net,
        vix_level=vix_level,
        pcr=pcr,
    )
