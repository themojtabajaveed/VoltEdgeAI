"""
brief_pipeline.py — Morning Brief v2 Pipeline
----------------------------------------------
Replaces monolithic Gemini-dependent brief with a multi-stage pipeline:

  Stage 1: Data Collection (parallel) — global data, events, news, movers
  Stage 2: Event Classification (Groq batch) — urgency scoring
  Stage 3: Analysis (3 parallel Groq calls) — digest, hot stocks, regime
  Stage 4: Assembly (deterministic) — template-based markdown
  Stage 5: Persist + Email

Fallback chain: Groq → Gemini → mechanical (never empty).
Total runtime: ~6-10 seconds vs 15-30s with old Gemini+Search.
"""
import os
import re
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class BriefData:
    """All raw data collected in Stage 1."""
    global_data: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    finnhub_news: list = field(default_factory=list)
    brave_news: list = field(default_factory=list)
    movers: Optional[dict] = None
    intel: object = None  # PreMarketIntelligence
    section_0_md: str = ""
    yesterday_learnings: list = field(default_factory=list)  # From system_learnings.json
    macro_plays: list = field(default_factory=list)  # From macro_sector_mapper


@dataclass
class BriefAnalysis:
    """Structured analysis from Stage 3."""
    news_digest: dict = field(default_factory=dict)
    hot_stocks: dict = field(default_factory=dict)
    regime: dict = field(default_factory=dict)


def generate_morning_brief() -> Optional[str]:
    """
    Main entry point for morning brief v2.
    Returns path to saved report, or None on failure.
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).date()
    now_ist = datetime.now(IST).strftime("%H:%M:%S")

    print(f"[VoltEdge] 📋 Morning Brief v2 starting at {now_ist} IST")

    # ── Duplicate guard ──────────────────────────────────────────────
    existing_paths = [
        os.path.join("logs", "daily_reports", f"{today}_morning_brief.md"),
        os.path.join("logs", "daily_reports", f"voltedge_{today}", f"{today}_morning_brief.md"),
    ]
    for ep in existing_paths:
        if os.path.exists(ep):
            print(f"[VoltEdge] Morning brief already exists at {ep} — skipping.")
            return ep

    # ── Stage 1: Data Collection ─────────────────────────────────────
    print(f"[VoltEdge] Stage 1: Collecting data...")
    brief_data = _collect_data()

    # ── Stage 2: Event Classification ────────────────────────────────
    print(f"[VoltEdge] Stage 2: Classifying {len(brief_data.events)} events...")
    classified_events = _classify_events(brief_data.events)

    # Brief pause to let Groq TPM window reset after batch classification
    import time
    time.sleep(3)

    # ── Stage 3: LLM Analysis ────────────────────────────────────────
    regime_score = 50
    if brief_data.intel:
        regime_score = brief_data.intel.composite_score
    intel_summary = brief_data.intel.format_log_line() if brief_data.intel else "Pre-market intelligence unavailable"

    print(f"[VoltEdge] Stage 3: Running parallel LLM analysis...")
    analysis = _run_analysis(
        classified_events=classified_events,
        finnhub_news=brief_data.finnhub_news,
        brave_news=brief_data.brave_news,
        global_data=brief_data.global_data,
        movers=brief_data.movers,
        regime_score=regime_score,
        intel_summary=intel_summary,
        yesterday_learnings=brief_data.yesterday_learnings,
        macro_plays=brief_data.macro_plays,
    )

    # ── Stage 4: Assembly ────────────────────────────────────────────
    print(f"[VoltEdge] Stage 4: Assembling report...")
    report_md = _assemble_report(
        today=today,
        brief_data=brief_data,
        classified_events=classified_events,
        analysis=analysis,
    )

    # ── Stage 5: Persist + Email ─────────────────────────────────────
    report_path = _persist_and_email(today, report_md, analysis, IST)

    elapsed = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[VoltEdge] ✅ Morning Brief v2 complete at {elapsed} IST → {report_path}")
    return report_path


# ═══════════════════════════════════════════════════════════════════════
# STAGE 1: DATA COLLECTION
# ═══════════════════════════════════════════════════════════════════════

def _collect_data() -> BriefData:
    """Fetch all data sources in parallel."""
    data = BriefData()

    def _fetch_global():
        try:
            from src.data_ingestion.pre_market_intelligence import (
                fetch_all_global_data, fetch_and_compute,
            )
            kite_client = _get_kite_client()
            global_data = fetch_all_global_data(kite_client=kite_client)
            intel = fetch_and_compute(preloaded_data=global_data)
            return global_data, intel
        except Exception as e:
            logger.warning(f"[Brief/S1] Global data fetch failed: {e}")
            return {}, None

    def _fetch_events():
        try:
            from src.data_ingestion.event_scanner import EventScanner
            scanner = EventScanner()
            return scanner.scan_since_close()
        except Exception as e:
            logger.warning(f"[Brief/S1] EventScanner failed: {e}")
            return []

    def _fetch_finnhub():
        try:
            from src.data_ingestion.finnhub_client import fetch_global_sentiment
            return fetch_global_sentiment()
        except Exception as e:
            logger.warning(f"[Brief/S1] Finnhub failed: {e}")
            return []

    def _fetch_brave():
        try:
            from src.llm.brief_analyzer import fetch_brave_news
            queries = [
                "NSE India stock market news today results earnings",
                "India corporate announcements contracts approvals today",
                "Nifty Sensex market outlook overnight global cues",
            ]
            return fetch_brave_news(queries, max_results_per_query=7)
        except Exception as e:
            logger.warning(f"[Brief/S1] Brave Search failed: {e}")
            return []

    def _fetch_movers():
        try:
            from src.data_ingestion.nse_scraper import get_nse_top_gainers_losers
            return get_nse_top_gainers_losers(count=5)
        except Exception as e:
            logger.warning(f"[Brief/S1] NSE movers failed: {e}")
            return None

    def _fetch_yesterday_learnings():
        """Load yesterday's system learnings for day-over-day learning loop."""
        try:
            learn_path = os.path.join("data", "system_learnings.json")
            if not os.path.exists(learn_path):
                return []
            with open(learn_path) as f:
                all_learnings = json.load(f)
            # Find the most recent entry (yesterday or last trading day)
            import zoneinfo as _zi
            _IST = _zi.ZoneInfo("Asia/Kolkata")
            _today = datetime.now(_IST).date()
            for i in range(1, 5):
                key = str(_today - timedelta(days=i))
                if key in all_learnings:
                    return all_learnings[key]
            return []
        except Exception as e:
            logger.warning(f"[Brief/S1] Yesterday learnings load failed: {e}")
            return []

    def _fetch_macro_impacts():
        """Scan macro data for sector impact plays (uses Yahoo cache — no dependency)."""
        try:
            from src.data_ingestion.yahoo_macro_fetcher import read_macro_cache
            from src.data_ingestion.macro_sector_mapper import scan_macro_impacts
            macro_cache = read_macro_cache(max_age_seconds=1800)  # 30 min
            if macro_cache:
                return scan_macro_impacts(macro_cache)
            return []
        except Exception as e:
            logger.warning(f"[Brief/S1] Macro impact scan failed: {e}")
            return []

    # Run all fetches in parallel
    with ThreadPoolExecutor(max_workers=7, thread_name_prefix="BriefS1") as pool:
        futures = {
            pool.submit(_fetch_global): "global",
            pool.submit(_fetch_events): "events",
            pool.submit(_fetch_finnhub): "finnhub",
            pool.submit(_fetch_brave): "brave",
            pool.submit(_fetch_movers): "movers",
            pool.submit(_fetch_yesterday_learnings): "learnings",
            pool.submit(_fetch_macro_impacts): "macro_impacts",
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                if name == "global":
                    data.global_data, data.intel = result
                    if data.intel:
                        data.section_0_md = data.intel.format_email_table()
                elif name == "events":
                    data.events = result or []
                elif name == "finnhub":
                    data.finnhub_news = result or []
                elif name == "brave":
                    data.brave_news = result or []
                elif name == "movers":
                    data.movers = result
                elif name == "learnings":
                    data.yesterday_learnings = result or []
                elif name == "macro_impacts":
                    data.macro_plays = result or []
            except Exception as e:
                logger.warning(f"[Brief/S1] {name} future failed: {e}")

    logger.info(
        f"[Brief/S1] Data collected: global={bool(data.global_data)}, "
        f"events={len(data.events)}, finnhub={len(data.finnhub_news)}, "
        f"brave={len(data.brave_news)}, movers={data.movers is not None}, "
        f"yesterday_learnings={len(data.yesterday_learnings)}, "
        f"macro_plays={len(data.macro_plays)}"
    )
    return data


def _get_kite_client():
    """Try to initialize Kite client for VIX/PCR."""
    try:
        from kiteconnect import KiteConnect
        ka = os.getenv("ZERODHA_API_KEY")
        at = os.getenv("ZERODHA_ACCESS_TOKEN")
        if ka and at:
            kite = KiteConnect(api_key=ka)
            kite.set_access_token(at)
            return kite
    except Exception as e:
        logger.warning(f"[Brief] Kite init failed: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════
# STAGE 2: EVENT CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════

def _classify_events(events: list) -> list:
    """Classify events via Groq and return as dicts sorted by urgency."""
    if not events:
        return []

    # Convert MarketEvent objects to dicts for Groq
    # Cap at 25 events to avoid Groq TPM rate limit (12K tokens/min on free tier)
    # Prioritize: NSE_ANNOUNCEMENTS first (more likely to be material), then deals
    events_sorted = sorted(events, key=lambda e: (
        0 if getattr(e, "source", "") == "NSE_ANNOUNCEMENTS" else 1,
        getattr(e, "timestamp", None) or "",
    ))
    events_to_classify = events_sorted[:25]
    if len(events) > 25:
        logger.info(f"[Brief/S2] Capped classification to 25 of {len(events)} events")

    event_dicts = []
    for e in events_to_classify:
        event_dicts.append({
            "symbol": getattr(e, "symbol", ""),
            "headline": getattr(e, "headline", ""),
            "category": getattr(e, "category", ""),
            "body": getattr(e, "body", ""),
            "source": getattr(e, "source", ""),
            "timestamp": str(getattr(e, "timestamp", "")),
        })

    try:
        from src.llm.groq_client import classify_events_batch
        classified = classify_events_batch(event_dicts)
        # Sort by urgency descending
        classified.sort(key=lambda x: float(x.get("urgency", 0)), reverse=True)
        hot_count = sum(1 for e in classified if float(e.get("urgency", 0)) >= 6)
        logger.info(f"[Brief/S2] Classified {len(classified)} events, {hot_count} hot (urgency≥6)")
        return classified
    except Exception as e:
        logger.warning(f"[Brief/S2] Classification failed: {e}")
        # Return unclassified with default urgency
        for d in event_dicts:
            d["urgency"] = 5
            d["direction"] = "NEUTRAL"
            d["event_type"] = "UNCLASSIFIED"
        return event_dicts


# ═══════════════════════════════════════════════════════════════════════
# STAGE 3: LLM ANALYSIS
# ═══════════════════════════════════════════════════════════════════════

_BEARISH_KEYWORDS = {
    "crash", "war", "collapse", "bearish", "negative",
    "crude spike", "escalation", "sell-off", "plunge", "crisis",
    "blockade", "sanctions", "recession", "default",
}

_BULLISH_KEYWORDS = {
    "rally", "surge", "breakout", "bullish", "positive",
    "upbeat", "ceasefire", "stimulus", "rate cut",
}


def _check_regime_consistency(regime: dict, news_digest: dict) -> dict:
    """Override regime if it clearly contradicts the news digest."""
    if not regime or not news_digest:
        return regime or {}

    trend = regime.get("trend", "sideways").lower()
    news_text = (
        news_digest.get("global_summary", "")
        + " "
        + " ".join(t for t in news_digest.get("key_themes", []))
    ).lower()

    # Bearish news overriding bullish regime
    bearish_hits = [kw for kw in _BEARISH_KEYWORDS if kw in news_text]
    if trend == "bullish" and len(bearish_hits) >= 2:
        logger.warning(
            f"[Regime] CONSISTENCY CHECK: Overriding {trend} → cautious/sideways — "
            f"news contains bearish signals: {bearish_hits[:3]}"
        )
        regime["trend"] = "sideways"
        regime["strength"] = min(regime.get("strength", 0.5), 0.5)
        regime["reasoning"] = (
            f"[OVERRIDE] Originally {trend}, but news digest contains bearish "
            f"signals ({', '.join(bearish_hits[:3])}). Downgraded to cautious."
        )

    # Bullish news overriding bearish regime
    bullish_hits = [kw for kw in _BULLISH_KEYWORDS if kw in news_text]
    if trend == "bearish" and len(bullish_hits) >= 2:
        logger.warning(
            f"[Regime] CONSISTENCY CHECK: Overriding {trend} → cautious/sideways — "
            f"news contains bullish signals: {bullish_hits[:3]}"
        )
        regime["trend"] = "sideways"
        regime["strength"] = min(regime.get("strength", 0.5), 0.5)
        regime["reasoning"] = (
            f"[OVERRIDE] Originally {trend}, but news digest contains bullish "
            f"signals ({', '.join(bullish_hits[:3])}). Upgraded to cautious."
        )

    return regime


def _run_analysis(
    classified_events: list,
    finnhub_news: list,
    brave_news: list,
    global_data: dict,
    movers: Optional[dict],
    regime_score: int,
    intel_summary: str,
    yesterday_learnings: Optional[list] = None,
    macro_plays: Optional[list] = None,
) -> BriefAnalysis:
    """Run 3 parallel Groq analysis calls. Injects yesterday's learnings into context."""
    from src.llm.brief_analyzer import (
        analyze_news_digest,
        analyze_hot_stocks,
        analyze_regime,
    )

    analysis = BriefAnalysis()
    hot_events = [e for e in classified_events if float(e.get("urgency", 0)) >= 6]

    # Build learning context block for Groq injection
    learning_context = ""
    if yesterday_learnings:
        bullets = "\n".join(f"  - {l}" for l in yesterday_learnings[:5])
        learning_context = f"\n\nYesterday's system learnings:\n{bullets}\n"

    def _call_digest():
        # Inject learnings into the global_data context for the digest call
        enriched_global = dict(global_data)
        if learning_context:
            enriched_global["_yesterday_learnings"] = learning_context
        return analyze_news_digest(classified_events[:15], finnhub_news, brave_news, enriched_global)

    def _call_hot_stocks():
        # Inject learnings into brave_news context for hot stocks
        enriched_brave = list(brave_news)
        if learning_context:
            enriched_brave.append({"title": "Yesterday's System Learnings", "description": learning_context, "url": "", "age": ""})
        return analyze_hot_stocks(hot_events, movers, regime_score, enriched_brave,
                                  macro_plays=macro_plays)

    # Step 1: Run digest + hot_stocks in parallel (regime depends on digest)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="BriefS3") as pool:
        f_digest = pool.submit(_call_digest)
        f_stocks = pool.submit(_call_hot_stocks)

        try:
            analysis.news_digest = f_digest.result() or {}
        except Exception as e:
            logger.warning(f"[Brief/S3] News digest failed: {e}")

        try:
            analysis.hot_stocks = f_stocks.result() or {}
        except Exception as e:
            logger.warning(f"[Brief/S3] Hot stocks failed: {e}")

    # Step 2: Run regime SEQUENTIALLY with digest themes + news summary
    themes = analysis.news_digest.get("key_themes", [])
    news_summary = analysis.news_digest.get("global_summary", "")[:500]
    try:
        analysis.regime = analyze_regime(
            global_data, intel_summary, themes, news_summary=news_summary
        ) or {}
    except Exception as e:
        logger.warning(f"[Brief/S3] Regime failed: {e}")

    # Step 3: Consistency check — override regime if it contradicts news
    analysis.regime = _check_regime_consistency(analysis.regime, analysis.news_digest)

    return analysis


# ═══════════════════════════════════════════════════════════════════════
# STAGE 4: REPORT ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════

def _assemble_report(
    today,
    brief_data: BriefData,
    classified_events: list,
    analysis: BriefAnalysis,
) -> str:
    """Assemble final markdown report from structured data."""
    sections = []

    # ── Header ───────────────────────────────────────────────────────
    sections.append(f"# VoltEdge Morning Brief — {today}\n")

    # ── Section 0: Machine-Computed Regime Intelligence ──────────────
    if brief_data.section_0_md:
        sections.append(brief_data.section_0_md)
        sections.append("\n---\n")

    # ── Section 1: Overnight Markets (machine data) ─────────────────
    sections.append(_build_markets_section(brief_data.global_data, analysis.news_digest, brief_data.movers))

    # ── Section 2: What Happened Since Close ─────────────────────────
    sections.append(_build_news_digest_section(analysis.news_digest))

    # ── Section 3: Corporate Events & Catalysts ──────────────────────
    sections.append(_build_events_section(classified_events))

    # ── Section 4: Hot Stocks & Trade Plans ──────────────────────────
    sections.append(_build_hot_stocks_section(analysis.hot_stocks))

    # ── Section 5: Tactical Directives ───────────────────────────────
    sections.append(_build_directives_section(analysis.regime))

    # ── Section 6: Risk Regime ───────────────────────────────────────
    sections.append(_build_regime_section(
        analysis.regime,
        hot_stocks=analysis.hot_stocks.get("hot_stocks", []),
    ))

    # ── JSON block for trading engine ────────────────────────────────
    sections.append(_build_json_block(analysis))

    return "\n".join(sections)


def _fetch_usdinr_fallback() -> tuple[str, str]:
    """
    Fetch the USD/INR spot rate via yfinance as a fallback when the primary
    data pipeline returns None for macro_quotes["USD/INR"].

    Returns (rate_str, note_str) where note_str is empty on a clean fetch
    and contains " *(estimated)*" when the static fallback is used.
    """
    for ticker in ("INR=X", "USDINR=X"):
        try:
            import yfinance as yf
            price = yf.Ticker(ticker).fast_info.get("last_price")
            if price and float(price) > 0:
                logger.info(f"[Brief/USD-INR] Fallback fetch succeeded via {ticker}: {price:.2f}")
                return f"{float(price):.2f}", ""
        except Exception as e:
            logger.warning(f"[Brief/USD-INR] yfinance fallback ({ticker}) failed: {e}")

    # Last resort: static estimate clearly labelled
    logger.warning("[Brief/USD-INR] All fallback sources failed — using static estimate 84.0")
    return "~84.0", " *(estimated)*"


def _build_markets_section(global_data: dict, news_digest: dict, movers: Optional[dict] = None) -> str:
    """Section 1: Overnight markets table — filled from machine data."""
    lines = ["## 1. Overnight Markets\n"]

    # Indices table
    lines.append("| Index / Asset | Chg% | Bias |")
    lines.append("|---------------|------|------|")

    spy = global_data.get("spy_change")
    qqq = global_data.get("qqq_change")
    if spy is not None:
        bias = "🟢 Bullish" if spy > 0.3 else "🔴 Bearish" if spy < -0.3 else "➖ Flat"
        lines.append(f"| S&P 500 (SPY) | {spy:+.2f}% | {bias} |")
    if qqq is not None:
        bias = "🟢 Bullish" if qqq > 0.3 else "🔴 Bearish" if qqq < -0.3 else "➖ Flat"
        lines.append(f"| Nasdaq (QQQ) | {qqq:+.2f}% | {bias} |")

    lines.append("")

    # Macro table
    lines.append("| Asset | Value | Chg% | India Impact |")
    lines.append("|-------|-------|------|--------------|")

    macro_quotes = global_data.get("macro_quotes", {})
    crude = macro_quotes.get("Brent Crude (USD/bbl)")
    if crude:
        impact = "Import cost ↓" if crude.change_pct < 0 else "Import cost ↑"
        lines.append(f"| Crude Oil | ${crude.price:.2f} | {crude.change_pct:+.2f}% | {impact} |")

    gold = macro_quotes.get("Gold (USD/oz)")
    if gold:
        impact = "Risk-off signal" if gold.change_pct > 0.5 else "Risk appetite OK"
        lines.append(f"| Gold | ${gold.price:.2f} | {gold.change_pct:+.2f}% | {impact} |")

    eur_usd = macro_quotes.get("EUR/USD")
    if eur_usd:
        # EUR/USD down = dollar strong = bearish for EM
        impact = "USD strong (EM pressure)" if eur_usd.change_pct < -0.2 else "USD stable"
        lines.append(f"| EUR/USD (DXY proxy) | {eur_usd.price:.4f} | {eur_usd.change_pct:+.2f}% | {impact} |")

    usd_inr = macro_quotes.get("USD/INR")
    if usd_inr:
        impact = "Rupee weakening" if usd_inr.change_pct > 0.1 else "Rupee stable"
        lines.append(f"| USD/INR | ₹{usd_inr.price:.2f} | {usd_inr.change_pct:+.2f}% | {impact} |")
    else:
        # Primary source unavailable — try yfinance fallback so we never show NA
        _rate, _note = _fetch_usdinr_fallback()
        _impact = "Rupee data via fallback" + _note
        lines.append(f"| USD/INR | ₹{_rate} | — | {_impact} |")

    # FII/DII
    fii = global_data.get("fii_net_cr")
    dii = global_data.get("dii_net_cr")
    if fii is not None:
        fii_signal = "FII buying" if fii > 0 else "FII selling"
        lines.append(f"| FII Cash Flow | ₹{fii:+.0f}Cr | — | {fii_signal} |")
    if dii is not None:
        lines.append(f"| DII Cash Flow | ₹{dii:+.0f}Cr | — | {'DII support' if dii > 0 else 'DII selling'} |")

    vix = global_data.get("vix_level")
    if vix is not None:
        vix_signal = "Low fear" if vix < 15 else "Elevated" if vix > 20 else "Normal"
        lines.append(f"| India VIX | {vix:.1f} | — | {vix_signal} |")

    pcr = global_data.get("pcr")
    if pcr is not None:
        pcr_signal = "Extreme fear (contrarian bullish)" if pcr > 1.2 else "Extreme greed (contrarian bearish)" if pcr < 0.7 else "Balanced"
        lines.append(f"| Nifty PCR | {pcr:.3f} | — | {pcr_signal} |")

    # Yesterday's movers
    if movers:
        lines.append("\n### Yesterday's NSE Movers\n")
        gainers = movers.get("gainers", [])
        losers = movers.get("losers", [])
        if gainers:
            g_str = ", ".join(f"**{g['symbol']}** ({g['pct_change']:+.1f}%)" for g in gainers[:5])
            lines.append(f"**Top Gainers:** {g_str}")
        if losers:
            l_str = ", ".join(f"**{l['symbol']}** ({l['pct_change']:+.1f}%)" for l in losers[:5])
            lines.append(f"**Top Losers:** {l_str}")

    lines.append("\n---\n")
    return "\n".join(lines)


def _build_news_digest_section(news_digest: dict) -> str:
    """Section 2: What happened since close."""
    lines = ["## 2. What Happened Since Close (17.5h Window)\n"]

    summary = news_digest.get("global_summary", "")
    if summary:
        lines.append(summary)
    else:
        lines.append("*News analysis unavailable.*")

    # India impacts table
    impacts = news_digest.get("india_impacts", [])
    if impacts:
        lines.append("\n### India Market Impacts\n")
        lines.append("| Event | Affected Sectors | Impact | Reasoning |")
        lines.append("|-------|-----------------|--------|-----------|")
        for imp in impacts:
            lines.append(
                f"| {imp.get('event', '?')} | {imp.get('sectors', '?')} | "
                f"{imp.get('impact', '?')} | {imp.get('reasoning', '?')} |"
            )

    lines.append("\n---\n")
    return "\n".join(lines)


def _build_events_section(classified_events: list) -> str:
    """Section 3: Corporate events table."""
    lines = ["## 3. Corporate Events & Catalysts\n"]

    # Filter to top events by urgency
    top_events = [e for e in classified_events if float(e.get("urgency", 0)) >= 4][:15]

    if not top_events:
        lines.append("*No significant corporate events detected since market close.*\n")
    else:
        lines.append("| Symbol | Event | Type | Urgency | Direction |")
        lines.append("|--------|-------|------|---------|-----------|")
        for e in top_events:
            urgency = float(e.get("urgency", 0))
            urgency_icon = "🔴" if urgency >= 8 else "🟠" if urgency >= 6 else "🟡"
            lines.append(
                f"| {e.get('symbol', '?')} | {e.get('headline', '?')[:80]} | "
                f"{e.get('event_type', '?')} | {urgency_icon} {urgency:.0f}/10 | "
                f"{e.get('direction', 'NEUTRAL')} |"
            )

    lines.append("\n---\n")
    return "\n".join(lines)


def _build_hot_stocks_section(hot_stocks_data: dict) -> str:
    """Section 4: Hot stocks with trade plans."""
    lines = ["## 4. Hot Stocks & Trade Plans\n"]

    stocks = hot_stocks_data.get("hot_stocks", [])
    if not stocks:
        lines.append("*No catalyst-driven hot stocks identified for today.*\n")
    else:
        for i, stock in enumerate(stocks, 1):
            symbol = stock.get("symbol", "?")
            catalyst = stock.get("catalyst", "Unknown catalyst")
            direction = stock.get("direction", "?")
            conviction = stock.get("conviction", "MEDIUM")

            lines.append(f"### {i}. {symbol} — {catalyst}\n")
            lines.append(f"- **Direction:** {direction}")
            lines.append(f"- **Entry Zone:** {stock.get('entry_zone', 'N/A')}")
            lines.append(f"- **Stop Loss:** {stock.get('stop_loss', 'N/A')}")
            lines.append(f"- **Target:** {stock.get('target', 'N/A')} (R:R {stock.get('risk_reward', 'N/A')})")
            lines.append(f"- **If gaps past entry:** {stock.get('if_gaps_past', 'Wait for pullback')}")
            lines.append(f"- **Conviction:** {conviction} | Catalyst Strength: {stock.get('catalyst_strength', '?')}")
            lines.append("")

    lines.append("---\n")
    return "\n".join(lines)


def _build_directives_section(regime: dict) -> str:
    """Section 5: Tactical directives."""
    lines = ["## 5. Tactical Directives\n"]

    directives = regime.get("directives", [])
    if directives:
        for i, d in enumerate(directives, 1):
            lines.append(f"{i}. {d}")
    else:
        lines.append("1. Use standard conviction thresholds")
        lines.append("2. Monitor first 15 minutes for gap direction")
        lines.append("3. No special directives — standard operating mode")

    lines.append("\n---\n")
    return "\n".join(lines)


def _build_regime_section(regime: dict, hot_stocks: Optional[list] = None) -> str:
    """
    Section 6: Risk Regime — renders structured regime data as human-readable
    markdown. Defensively handles raw JSON strings that the LLM sometimes
    returns inside individual fields (or as the entire regime value).
    """
    lines = ["## 6. Risk Regime\n"]

    # ── Defensive: regime itself might be a raw JSON string ──────────────
    if isinstance(regime, str):
        try:
            regime = json.loads(regime)
        except json.JSONDecodeError:
            logger.warning("[Brief/S6] regime is a non-JSON string — showing raw with warning")
            lines.append("⚠️ Format error — raw output below\n")
            lines.append(f"```\n{regime}\n```")
            return "\n".join(lines)

    if not regime:
        lines.append("*Regime analysis unavailable.*\n")
        return "\n".join(lines)

    # ── Regime headline ───────────────────────────────────────────────────
    trend = str(regime.get("trend", "sideways")).upper()
    strength = regime.get("strength", 0.5)
    try:
        strength = float(strength)
    except (TypeError, ValueError):
        strength = 0.5
    lines.append(f"**Regime:** {trend} (Strength: {strength:.1f}/1.0)\n")

    # ── Reasoning — parse out if LLM buried raw JSON inside this field ───
    reasoning = regime.get("reasoning", "")
    if reasoning and isinstance(reasoning, str):
        try:
            parsed_r = json.loads(reasoning)
            # Extract a text field from the nested JSON blob
            reasoning = (
                parsed_r.get("reasoning")
                or parsed_r.get("summary")
                or parsed_r.get("analysis")
                or str(parsed_r)
            )
            logger.warning("[Brief/S6] reasoning field contained raw JSON — extracted text successfully")
        except (json.JSONDecodeError, TypeError):
            pass  # Normal string — use as-is
    if reasoning:
        lines.append(f"*{reasoning}*\n")

    # ── Sectors — also handle if stored as JSON strings ──────────────────
    def _coerce_list(val) -> list:
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            return [val] if val.strip() else []
        return []

    sectors_long = _coerce_list(regime.get("top_sectors_long", []))
    sectors_short = _coerce_list(regime.get("top_sectors_short", []))

    if sectors_long:
        lines.append(f"**Sectors to go LONG:** {', '.join(sectors_long)}")
    if sectors_short:
        lines.append(f"**Sectors to go SHORT / Avoid:** {', '.join(sectors_short)}\n")

    # ── Predictions for tomorrow table ───────────────────────────────────
    if hot_stocks:
        lines.append("**Predictions for tomorrow:**")
        lines.append("| Symbol | Direction | Key Level | Reason |")
        lines.append("|--------|-----------|-----------|--------|")
        for stock in hot_stocks[:5]:
            symbol = stock.get("symbol", "?")
            direction = "BULLISH" if stock.get("direction") == "LONG" else "BEARISH"
            key_level = stock.get("entry_zone", "N/A")
            reason = (stock.get("catalyst") or "N/A")[:90]
            lines.append(f"| {symbol} | {direction} | {key_level} | {reason} |")
        lines.append("")

    return "\n".join(lines)


def _build_json_block(analysis: BriefAnalysis) -> str:
    """JSON block for trading engine consumption."""
    regime = analysis.regime
    hot_stocks = analysis.hot_stocks.get("hot_stocks", [])

    predictions = []
    for stock in hot_stocks[:5]:
        predictions.append({
            "symbol": stock.get("symbol", ""),
            "predicted_direction": "bullish" if stock.get("direction") == "LONG" else "bearish",
            "key_level": stock.get("entry_zone", "0"),
            "reason": stock.get("catalyst", ""),
        })

    regime_json = {
        "trend": regime.get("trend", "sideways"),
        "strength": regime.get("strength", 0.5),
        "top_sectors_long": regime.get("top_sectors_long", []),
        "top_sectors_short": regime.get("top_sectors_short", []),
        "predictions": predictions,
    }

    return f"\n```json\n{json.dumps(regime_json, indent=2)}\n```\n"


# ═══════════════════════════════════════════════════════════════════════
# STAGE 5: PERSIST + EMAIL
# ═══════════════════════════════════════════════════════════════════════

def _persist_and_email(today, report_md: str, analysis: BriefAnalysis, IST) -> str:
    """Save report, regime JSON, predictions, and send email."""
    report_path = ""

    # Save markdown report
    try:
        os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
        report_path = os.path.join("logs", "daily_reports", f"{today}_morning_brief.md")
        with open(report_path, "w") as f:
            f.write(report_md)
        print(f"[VoltEdge] Report saved: {report_path}")
    except Exception as e:
        logger.error(f"[Brief/S5] Failed to save report: {e}")

    # Save regime JSON
    regime = analysis.regime
    try:
        os.makedirs("data", exist_ok=True)
        with open("data/daily_regime.json", "w") as f:
            json.dump({
                "trend": regime.get("trend", "sideways"),
                "strength": regime.get("strength", 0.5),
            }, f, indent=2)
        logger.info(f"[Brief/S5] Regime saved: {regime.get('trend')}/{regime.get('strength')}")
    except Exception as e:
        logger.error(f"[Brief/S5] Failed to save regime: {e}")

    # Save predictions to feedback log
    try:
        predictions = analysis.hot_stocks.get("hot_stocks", [])
        if predictions:
            _persist_predictions(today, predictions)
    except Exception as e:
        logger.warning(f"[Brief/S5] Prediction persistence failed: {e}")

    # Email dispatch
    try:
        from src.reports.email_sender import send_report_email
        subject = f"VoltEdge Morning Brief — {today}"
        email_ok = send_report_email(
            subject=subject,
            body_md=report_md,
            attachment_path=report_path if report_path else None,
        )
        now_ist = datetime.now(IST).strftime("%H:%M:%S")
        if email_ok:
            logger.info(f"[EMAIL] Pre-market email sent at {now_ist}")
        else:
            logger.error(f"[EMAIL] Pre-market email FAILED at {now_ist}")
    except Exception as e:
        logger.error(f"[EMAIL] Email dispatch error: {e}")

    return report_path


def _persist_predictions(today, hot_stocks: list) -> None:
    """Save hot stock predictions for evening feedback loop scoring."""
    path = "data/prediction_log.json"
    try:
        if os.path.exists(path):
            with open(path) as f:
                log = json.load(f)
        else:
            log = {"predictions": [], "system_lessons": []}

        for stock in hot_stocks[:5]:
            pred = {
                "date": str(today),
                "symbol": stock.get("symbol", ""),
                "predicted_direction": "bullish" if stock.get("direction") == "LONG" else "bearish",
                "key_level": stock.get("entry_zone", ""),
                "reason": stock.get("catalyst", ""),
                "score": None,
                "actual_change_pct": None,
            }
            # Remove duplicates for same symbol+date
            log["predictions"] = [
                p for p in log["predictions"]
                if not (p.get("date") == str(today) and p.get("symbol") == pred["symbol"])
            ]
            log["predictions"].append(pred)

        os.makedirs("data", exist_ok=True)
        with open(path, "w") as f:
            json.dump(log, f, indent=2)
        logger.info(f"[Brief/S5] Saved {len(hot_stocks[:5])} predictions to feedback log")
    except Exception as e:
        logger.warning(f"[Brief/S5] Prediction log save failed: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    generate_morning_brief()
