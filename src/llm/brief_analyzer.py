"""
brief_analyzer.py — Focused LLM Analysis for Morning Brief v2
--------------------------------------------------------------
Three analysis functions, each with Groq → Gemini → mechanical fallback.
Plus Brave Search integration for real-time news beyond Finnhub.

Groq (primary): llama-3.3-70b, ~300ms, 14,400 req/day
Gemini (fallback): gemini-2.5-flash
Brave Search: real-time web search for overnight news + corporate catalysts
"""
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ── Brave Search ─────────────────────────────────────────────────────────

def fetch_brave_news(queries: List[str], max_results_per_query: int = 5) -> List[dict]:
    """
    Fetch real-time news via Brave Search API.

    Args:
        queries: List of search queries (e.g. ["NSE stock results today", "India market news"])
        max_results_per_query: Max results per query

    Returns:
        List of {"title": ..., "description": ..., "url": ..., "age": ...}
    """
    import requests

    api_key = os.getenv("BRAVE_API_KEY")
    if not api_key:
        logger.warning("[Brave] BRAVE_API_KEY not set")
        return []

    all_results = []
    seen_urls = set()

    for query in queries:
        try:
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                params={"q": query, "count": max_results_per_query, "freshness": "pd"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"[Brave] Query '{query}' returned {resp.status_code}")
                continue

            data = resp.json()
            for r in data.get("web", {}).get("results", []):
                url = r.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                all_results.append({
                    "title": r.get("title", ""),
                    "description": r.get("description", "")[:300],
                    "url": url,
                    "age": r.get("age", ""),
                })
        except Exception as e:
            logger.warning(f"[Brave] Query '{query}' failed: {e}")

    logger.info(f"[Brave] Fetched {len(all_results)} unique results from {len(queries)} queries")
    return all_results


# ── LLM Helpers ──────────────────────────────────────────────────────────

def _call_groq(prompt: str, max_tokens: int = 2000) -> Optional[dict]:
    """Call Groq LLaMA 3.3 70B and parse JSON response."""
    try:
        from groq import Groq
    except ImportError:
        return None

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        raw = response.choices[0].message.content.strip()
        return _extract_json(raw)
    except Exception as e:
        logger.warning(f"[BriefAnalyzer/Groq] Call failed: {e}")
        return None


def _call_gemini(prompt: str) -> Optional[dict]:
    """Call Gemini 2.5 Flash and parse JSON response."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        raw = response.text.strip()
        return _extract_json(raw)
    except Exception as e:
        logger.warning(f"[BriefAnalyzer/Gemini] Call failed: {e}")
        return None


def _extract_json(raw: str) -> Optional[dict]:
    """Extract JSON from LLM response (handles fenced blocks, think tags)."""
    import re
    # Strip <think> blocks
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)

    # Try fenced JSON
    match = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try bare JSON
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ── Analysis Function 1: News Digest ────────────────────────────────────

def analyze_news_digest(
    events: List[dict],
    finnhub_news: list,
    brave_news: List[dict],
    global_data: dict,
) -> dict:
    """
    Produce a structured digest of overnight events and their India impact.

    Returns:
        {
            "global_summary": "2-3 paragraph digest",
            "india_impacts": [{"event": ..., "sectors": ..., "impact": ..., "reasoning": ...}],
            "key_themes": ["theme1", "theme2"],
        }
    """
    # Build context from all news sources
    news_items = []

    for n in finnhub_news[:10]:
        news_items.append(f"[Finnhub] {n.get('headline', '')}")

    for n in brave_news[:15]:
        news_items.append(f"[Web] {n.get('title', '')} — {n.get('description', '')[:150]}")

    for e in events[:10]:
        src = e.get("source", "NSE")
        news_items.append(f"[{src}] {e.get('symbol', '?')}: {e.get('headline', '')[:120]}")

    news_block = "\n".join(news_items) if news_items else "No news available."

    # Build macro context
    macro_lines = []
    for label, key in [("SPY", "spy_change"), ("QQQ", "qqq_change"),
                        ("Crude", "crude_change"), ("USD/INR", "usd_inr_change"),
                        ("EUR/USD", "eur_usd_change")]:
        val = global_data.get(key)
        if val is not None:
            macro_lines.append(f"{label}: {val:+.2f}%")
    vix = global_data.get("vix_level")
    if vix:
        macro_lines.append(f"India VIX: {vix:.1f}")
    macro_block = ", ".join(macro_lines) if macro_lines else "Macro data unavailable"

    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).strftime("%Y-%m-%d")

    prompt = f"""You are VoltEdge's senior market analyst. Today is {today}.

Summarize overnight developments (3:30 PM yesterday → 9:00 AM today) for an Indian equity trader.

## Macro Data
{macro_block}

## News & Events (last 17.5 hours)
{news_block}

Return ONLY valid JSON:
{{
  "global_summary": "2-3 paragraphs covering: (1) US session highlights, (2) Asia/overnight developments, (3) India morning implications. Each paragraph ends with India market impact.",
  "india_impacts": [
    {{"event": "key event name", "sectors": "affected Indian sectors", "impact": "bullish/bearish/neutral", "reasoning": "1 sentence"}}
  ],
  "key_themes": ["theme1", "theme2", "theme3"]
}}

Be specific. Use real numbers. No vague statements. Max 3-4 india_impacts for the MOST important events only."""

    # Try Groq → Gemini → mechanical fallback
    result = _call_groq(prompt, max_tokens=1500)
    if result and "global_summary" in result:
        logger.info("[BriefAnalyzer] News digest: Groq OK")
        return result

    result = _call_gemini(prompt)
    if result and "global_summary" in result:
        logger.info("[BriefAnalyzer] News digest: Gemini fallback OK")
        return result

    # Mechanical fallback
    logger.warning("[BriefAnalyzer] News digest: both LLMs failed, using mechanical fallback")
    headlines = [n.get("headline", n.get("title", "")) for n in (finnhub_news[:5] + brave_news[:5])]
    return {
        "global_summary": "**LLM unavailable — raw headlines below.**\n\n" + "\n".join(f"- {h}" for h in headlines if h),
        "india_impacts": [],
        "key_themes": ["Data available but analysis unavailable"],
    }


# ── Analysis Function 2: Hot Stocks + Trade Plans ───────────────────────

def analyze_hot_stocks(
    hot_events: List[dict],
    movers: Optional[dict],
    regime_score: int,
    brave_news: List[dict],
    macro_plays: Optional[List[dict]] = None,
    verified_prices: Optional[Dict[str, tuple]] = None,
) -> dict:
    """
    Identify top 5 catalyst-driven stocks and generate trade plans.

    Returns:
        {
            "hot_stocks": [
                {
                    "symbol": "NSE_SYMBOL",
                    "direction": "LONG" | "SHORT",
                    "catalyst": "1 sentence",
                    "catalyst_strength": "HIGH" | "MEDIUM",
                    "entry_zone": "xxx-xxx",
                    "stop_loss": "xxx",
                    "target": "xxx",
                    "risk_reward": "x:x",
                    "if_gaps_past": "wait for pullback to xxx or skip",
                    "conviction": "HIGH" | "MEDIUM"
                }
            ]
        }
    """
    # Build candidates from events + movers + brave news + macro plays
    candidates = []

    # Inject macro-driven sector plays (machine-confirmed, skip catalyst validation)
    if macro_plays:
        for mp in macro_plays:
            candidates.append(
                f"[MACRO-PLAY confirmed=True] {mp.get('direction', '?')} {mp.get('symbol', '?')}: "
                f"{mp.get('catalyst', '')} | conviction={mp.get('conviction', 0)} | "
                f"source=MACRO_RULES | catalyst_strength={mp.get('catalyst_strength', 'HIGH')}"
            )

    for e in hot_events[:15]:
        candidates.append(
            f"[EVENT urgency={e.get('urgency', 0):.0f}] {e.get('symbol', '?')}: "
            f"{e.get('headline', '')[:120]} | direction={e.get('direction', 'NEUTRAL')} | "
            f"type={e.get('event_type', '?')}"
        )

    if movers:
        for g in movers.get("gainers", [])[:5]:
            candidates.append(
                f"[YESTERDAY-GAINER momentum only, not a catalyst] {g['symbol']}: {g['pct_change']:+.2f}% | "
                f"prev_close={g.get('prev_close', '?')} | last_price={g.get('last_price', '?')}"
            )
        for l in movers.get("losers", [])[:5]:
            candidates.append(
                f"[YESTERDAY-LOSER momentum only, not a catalyst] {l['symbol']}: {l['pct_change']:+.2f}% | "
                f"prev_close={l.get('prev_close', '?')} | last_price={l.get('last_price', '?')}"
            )

    # Add Brave news mentions with stock names
    for b in brave_news[:10]:
        title = b.get("title", "")
        desc = b.get("description", "")
        candidates.append(f"[NEWS] {title[:100]} — {desc[:100]}")

    if not candidates:
        return {"hot_stocks": []}

    candidates_block = "\n".join(candidates)
    regime_label = "BULLISH" if regime_score >= 55 else "BEARISH" if regime_score < 40 else "NEUTRAL"

    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).strftime("%Y-%m-%d")

    # Verified-price grounding — prevents LLM from hallucinating stale/split-adjusted
    # prices (e.g. WIPRO 490 vs actual ~207). Caller passes {symbol: (price, source)}.
    verified_prices_block = ""
    if verified_prices:
        lines_vp = [
            f"  - {sym}: ₹{p:.2f} (source: {src})"
            for sym, (p, src) in sorted(verified_prices.items())
        ]
        verified_prices_block = (
            "\n## Verified Current Market Prices (GROUND TRUTH — USE THESE EXACT VALUES)\n"
            + "\n".join(lines_vp)
            + "\n\nCRITICAL: All entry zones, stop losses, and targets MUST be within ±15% "
              "of the verified price above. Do NOT use any other price reference, training-data "
              "memory, or pre-split prices. If a symbol is not in the verified list, anchor off "
              "the candidate's prev_close / last_price fields instead.\n"
        )

    prompt = f"""You are VoltEdge's pre-market trade planner. Today is {today}.
Market regime: {regime_label} (score {regime_score}/100).

From the candidates below, select up to 5 stocks with the STRONGEST, most VERIFIABLE catalysts.
A hot stock must have a SPECIFIC RECENT CATALYST — earnings, contract win, regulatory approval,
merger, block deal, sector policy change, or global peer movement. NOT technical patterns.

Only select stocks with HIGH or MEDIUM catalyst strength:
- HIGH: earnings beat/miss, M&A, regulatory approval/rejection, major contract
- MEDIUM: block deal, dividend, sector event, global peer movement
- LOW (exclude): technical pattern, routine filing, vague mention

MACRO-PLAY candidates have machine-confirmed catalysts from live market data. They are ALWAYS valid catalysts with HIGH strength. Still provide entry/SL/target based on typical price levels.
{verified_prices_block}
## Candidates
{candidates_block}

For each selected stock, provide a trade plan. Use the verified price above as the anchor
(or prev_close / last_price if unverified). Entry should be realistic (not yesterday's close
if the stock will gap).

Return ONLY valid JSON:
{{
  "hot_stocks": [
    {{
      "symbol": "NSE_SYMBOL",
      "direction": "LONG or SHORT",
      "catalyst": "1 sentence describing the specific catalyst",
      "catalyst_strength": "HIGH or MEDIUM",
      "entry_zone": "price range like 1450-1470",
      "stop_loss": "price",
      "target": "price",
      "risk_reward": "ratio like 1:2.5",
      "if_gaps_past": "what to do if stock gaps past entry — usually wait for pullback",
      "conviction": "HIGH or MEDIUM"
    }}
  ]
}}

If no stocks have HIGH or MEDIUM catalysts, return {{"hot_stocks": []}}.
Max 5 stocks. Use actual NSE trading symbols."""

    result = _call_groq(prompt, max_tokens=2500)
    if result and "hot_stocks" in result:
        logger.info(f"[BriefAnalyzer] Hot stocks: Groq OK, {len(result['hot_stocks'])} stocks")
        return result

    result = _call_gemini(prompt)
    if result and "hot_stocks" in result:
        logger.info(f"[BriefAnalyzer] Hot stocks: Gemini fallback OK, {len(result['hot_stocks'])} stocks")
        return result

    # Mechanical fallback: top events by urgency with no trade plans
    logger.warning("[BriefAnalyzer] Hot stocks: both LLMs failed, using event fallback")
    fallback_stocks = []
    for e in hot_events[:5]:
        fallback_stocks.append({
            "symbol": e.get("symbol", "?"),
            "direction": e.get("direction", "NEUTRAL"),
            "catalyst": e.get("headline", "")[:100],
            "catalyst_strength": "HIGH" if e.get("urgency", 0) >= 8 else "MEDIUM",
            "entry_zone": "N/A (LLM unavailable)",
            "stop_loss": "N/A",
            "target": "N/A",
            "risk_reward": "N/A",
            "if_gaps_past": "N/A",
            "conviction": "MEDIUM",
        })
    return {"hot_stocks": fallback_stocks}


# ── Analysis Function 3: Regime Assessment + Directives ─────────────────

def analyze_regime(
    global_data: dict,
    intel_summary: str,
    news_themes: List[str],
    news_summary: str = "",
) -> dict:
    """
    Generate regime assessment and 3 tactical directives.

    Returns:
        {
            "trend": "bullish" | "bearish" | "sideways",
            "strength": 0.0-1.0,
            "reasoning": "1-2 sentences",
            "directives": ["directive1", "directive2", "directive3"],
            "top_sectors_long": ["SECTOR1", "SECTOR2"],
            "top_sectors_short": ["SECTOR1"],
        }
    """
    macro_lines = []
    for label, key in [("SPY", "spy_change"), ("QQQ", "qqq_change"),
                        ("Crude", "crude_change"), ("USD/INR", "usd_inr_change"),
                        ("FII Net ₹Cr", "fii_net_cr"), ("DII Net ₹Cr", "dii_net_cr"),
                        ("India VIX", "vix_level"), ("Nifty PCR", "pcr")]:
        val = global_data.get(key)
        if val is not None:
            if isinstance(val, float) and abs(val) < 100:
                macro_lines.append(f"{label}: {val:+.2f}" if "change" in key else f"{label}: {val}")
            else:
                macro_lines.append(f"{label}: {val}")
    macro_block = "\n".join(macro_lines) if macro_lines else "Limited data"
    themes_block = ", ".join(news_themes[:5]) if news_themes else "No themes identified"

    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).strftime("%Y-%m-%d")

    news_block = ""
    if news_summary:
        news_block = f"""
## News Summary (from overnight digest)
{news_summary}

IMPORTANT RULE: If the news summary describes major bearish events (crude spike >3%, geopolitical escalation, war, crash) that are NOT captured in the signal scores due to missing data, you MUST factor that into the regime verdict. When signals are incomplete (< 5 of 9 available), the news context should be the PRIMARY input for regime. Missing signals should make the regime MORE cautious, not more bullish.
"""

    prompt = f"""You are VoltEdge's risk regime analyst. Today is {today}.

Given the machine-computed data and news themes, provide a regime assessment and 3 tactical directives for the Indian equity trading engine.

## Machine Intelligence
{intel_summary}

## Macro Data
{macro_block}

## Key News Themes
{themes_block}
{news_block}
Return ONLY valid JSON:
{{
  "trend": "bullish or bearish or sideways",
  "strength": 0.0 to 1.0,
  "reasoning": "1-2 sentences explaining the primary driver of this regime call",
  "directives": [
    "Specific tactical rule 1 for today's trading",
    "Specific tactical rule 2",
    "Specific tactical rule 3"
  ],
  "top_sectors_long": ["SECTOR1", "SECTOR2"],
  "top_sectors_short": ["SECTOR1"]
}}

Directives must be concrete and actionable. Not vague. Example:
- "Avoid IT stocks today — Infosys results miss may drag sector"
- "Tighten stops to 1.5% on all longs — VIX elevated at 22"
- "Favor PSU banks on FII buying + rate cut expectation"
"""

    result = _call_groq(prompt, max_tokens=1000)
    if result and "trend" in result:
        logger.info(f"[BriefAnalyzer] Regime: Groq OK, trend={result['trend']}")
        return result

    result = _call_gemini(prompt)
    if result and "trend" in result:
        logger.info(f"[BriefAnalyzer] Regime: Gemini fallback OK, trend={result['trend']}")
        return result

    # Mechanical fallback from composite score
    logger.warning("[BriefAnalyzer] Regime: both LLMs failed, using mechanical fallback")
    score = 50  # default
    try:
        from src.data_ingestion.pre_market_intelligence import compute_pre_market_intelligence
        intel = compute_pre_market_intelligence(
            spy_change=global_data.get("spy_change"),
            qqq_change=global_data.get("qqq_change"),
            crude_change=global_data.get("crude_change"),
            eur_usd_change=global_data.get("eur_usd_change"),
            usd_inr_change=global_data.get("usd_inr_change"),
            fii_net_cr=global_data.get("fii_net_cr"),
            dii_net_cr=global_data.get("dii_net_cr"),
            vix_level=global_data.get("vix_level"),
            pcr=global_data.get("pcr"),
        )
        if intel:
            score = intel.composite_score
    except Exception:
        pass

    if score >= 60:
        trend, strength = "bullish", round(score / 100, 2)
    elif score <= 40:
        trend, strength = "bearish", round((100 - score) / 100, 2)
    else:
        trend, strength = "sideways", 0.5

    return {
        "trend": trend,
        "strength": strength,
        "reasoning": f"Mechanical regime from composite score {score}/100. LLM analysis unavailable.",
        "directives": [
            "Use standard conviction thresholds — no LLM override available",
            "Monitor first 15 minutes for gap direction before entering",
            "Reduce position sizes by 30% due to incomplete analysis",
        ],
        "top_sectors_long": [],
        "top_sectors_short": [],
    }


# ── Mid-Session Analysis Functions ────────────────────────────────────────


def fetch_mover_reasons(
    gainers: List[dict],
    losers: List[dict],
    max_per_side: int = 3,
) -> Dict[str, str]:
    """
    Use Brave Search to find reasons behind today's top movers.

    Args:
        gainers: List of {"symbol": ..., "pct_change": ...}
        losers: List of {"symbol": ..., "pct_change": ...}
        max_per_side: Max stocks per side to query

    Returns:
        {symbol: "reason string"} dict
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).strftime("%Y-%m-%d")

    symbols_to_query = []
    for g in gainers[:max_per_side]:
        symbols_to_query.append(g.get("symbol", ""))
    for l in losers[:max_per_side]:
        symbols_to_query.append(l.get("symbol", ""))

    symbols_to_query = [s for s in symbols_to_query if s]
    if not symbols_to_query:
        return {}

    queries = [f"{sym} NSE stock {today} reason news" for sym in symbols_to_query]
    brave_results = fetch_brave_news(queries, max_results_per_query=3)

    if not brave_results:
        logger.warning("[MoverReasons] Brave Search returned no results")
        return {sym: "No catalyst identified" for sym in symbols_to_query}

    # Group results by symbol
    symbol_news: Dict[str, List[str]] = {sym: [] for sym in symbols_to_query}
    for r in brave_results:
        title = r.get("title", "").upper()
        desc = r.get("description", "")
        for sym in symbols_to_query:
            if sym.upper() in title or sym.upper() in desc.upper():
                symbol_news[sym].append(f"{r.get('title', '')} — {desc[:150]}")

    # Ask Groq to extract reasons from news snippets
    reasons: Dict[str, str] = {}
    for sym in symbols_to_query:
        snippets = symbol_news.get(sym, [])
        if not snippets:
            reasons[sym] = "No catalyst identified"
            continue

        snippets_block = "\n".join(snippets[:5])
        prompt = f"""From these news snippets about {sym} (NSE), extract the ONE primary reason the stock moved today in 1 short sentence (max 15 words). If no clear reason, say "Sector/market momentum".

News:
{snippets_block}

Return ONLY valid JSON: {{"reason": "the reason"}}"""

        result = _call_groq(prompt, max_tokens=100)
        if result and "reason" in result:
            reasons[sym] = result["reason"]
        else:
            # Try extracting from title directly
            reasons[sym] = snippets[0].split(" — ")[0][:80] if snippets else "No catalyst identified"

    logger.info(f"[MoverReasons] Got reasons for {len(reasons)} symbols")
    return reasons


def analyze_mid_session(
    market_data: dict,
    mover_reasons: Dict[str, str],
    predictions_status: List[dict],
) -> dict:
    """
    Generate morning narrative and commentary for mid-session report.

    Args:
        market_data: {nifty_pct, vix, ad_ratio, phase, sector_changes, top_gainers, top_losers}
        mover_reasons: {symbol: reason} from fetch_mover_reasons()
        predictions_status: [{symbol, predicted, actual_pct, status}]

    Returns:
        {
            "narrative": "3-4 sentence morning market summary",
            "mover_commentary": "2-3 sentences on why top movers moved",
            "prediction_commentary": "1-2 sentences on morning brief accuracy"
        }
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)

    # Build market context
    nifty_pct = market_data.get("nifty_pct", 0)
    vix = market_data.get("vix", 0)
    ad_ratio = market_data.get("ad_ratio", 0)
    phase = market_data.get("phase", "UNKNOWN")

    # Sector data
    sectors = market_data.get("sector_changes", {})
    sector_str = ", ".join(f"{k}: {v:+.2f}%" for k, v in sorted(sectors.items(), key=lambda x: x[1], reverse=True)[:5]) if sectors else "N/A"

    # Movers with reasons
    movers_lines = []
    for g in market_data.get("top_gainers", [])[:3]:
        sym = g.get("symbol", "?")
        reason = mover_reasons.get(sym, "—")
        movers_lines.append(f"GAINER: {sym} {g.get('pct_change', 0):+.1f}% — {reason}")
    for l in market_data.get("top_losers", [])[:3]:
        sym = l.get("symbol", "?")
        reason = mover_reasons.get(sym, "—")
        movers_lines.append(f"LOSER: {sym} {l.get('pct_change', 0):+.1f}% — {reason}")
    movers_block = "\n".join(movers_lines) if movers_lines else "No mover data"

    # Predictions
    pred_lines = []
    for p in predictions_status:
        pred_lines.append(f"{p['symbol']}: predicted {p['predicted']} → actual {p.get('actual_pct', 'N/A')}% → {p.get('status', '?')}")
    pred_block = "\n".join(pred_lines) if pred_lines else "No predictions to check"

    prompt = f"""You are VoltEdge's mid-session analyst. Time: {now.strftime('%H:%M')} IST, {now.strftime('%Y-%m-%d')}.

## Market State
- Nifty: {nifty_pct:+.2f}% | Phase: {phase} | VIX: {vix:.1f} | A/D: {ad_ratio:.2f}
- Sectors: {sector_str}

## Top Movers & Reasons
{movers_block}

## Morning Brief Predictions vs Reality
{pred_block}

Write:
1. A 3-4 sentence morning market narrative (what happened from open to now, key themes, institutional tone)
2. A 2-3 sentence commentary on why the top movers moved (use the reasons provided)
3. A 1-2 sentence commentary on morning brief prediction accuracy

Return ONLY valid JSON:
{{
  "narrative": "3-4 sentences",
  "mover_commentary": "2-3 sentences",
  "prediction_commentary": "1-2 sentences"
}}

Be specific. Use real numbers and stock names. No vague statements."""

    result = _call_groq(prompt, max_tokens=800)
    if result and "narrative" in result:
        logger.info("[MidSession] Narrative: Groq OK")
        return result

    result = _call_gemini(prompt)
    if result and "narrative" in result:
        logger.info("[MidSession] Narrative: Gemini fallback OK")
        return result

    # Mechanical fallback
    logger.warning("[MidSession] Narrative: both LLMs failed, using mechanical fallback")
    direction = "up" if nifty_pct > 0 else "down" if nifty_pct < 0 else "flat"
    breadth = "strong" if ad_ratio > 0.6 else "weak" if ad_ratio < 0.4 else "mixed"
    return {
        "narrative": f"Market is in {phase} phase. Nifty {direction} {abs(nifty_pct):.2f}% with {breadth} breadth (A/D {ad_ratio:.2f}). VIX at {vix:.1f}.",
        "mover_commentary": "LLM analysis unavailable. See scoreboard below for top movers.",
        "prediction_commentary": "LLM analysis unavailable. See accuracy table below for prediction outcomes.",
    }


def analyze_second_half(
    market_data: dict,
    signals_summary: dict,
    risk_flags: List[str],
) -> dict:
    """
    Generate second-half outlook for mid-session report.

    Args:
        market_data: {nifty_pct, vix, ad_ratio, phase, sector_changes}
        signals_summary: {total, by_strategy: {HYDRA: n, VIPER: n}, above_threshold: n}
        risk_flags: List of risk flag strings

    Returns:
        {
            "outlook": "3-4 sentence afternoon outlook",
            "risk_flags": ["flag1", "flag2"],
            "watchlist_highlight": "1 sentence on most interesting signal"
        }
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)

    nifty_pct = market_data.get("nifty_pct", 0)
    vix = market_data.get("vix", 0)
    ad_ratio = market_data.get("ad_ratio", 0)
    phase = market_data.get("phase", "UNKNOWN")

    sectors = market_data.get("sector_changes", {})
    sector_str = ", ".join(f"{k}: {v:+.2f}%" for k, v in sorted(sectors.items(), key=lambda x: x[1], reverse=True)[:5]) if sectors else "N/A"

    flags_block = "\n".join(f"- {f}" for f in risk_flags) if risk_flags else "No flags"

    total_signals = signals_summary.get("total", 0)
    by_strat = signals_summary.get("by_strategy", {})
    above_thresh = signals_summary.get("above_threshold", 0)

    prompt = f"""You are VoltEdge's mid-session risk analyst. Time: {now.strftime('%H:%M')} IST.

## Current Market State
- Nifty: {nifty_pct:+.2f}% | Phase: {phase} | VIX: {vix:.1f} | A/D: {ad_ratio:.2f}
- Sectors: {sector_str}

## System State
- Active signals: {total_signals} (by strategy: {by_strat})
- Signals above conviction threshold (70): {above_thresh}

## Risk Flags
{flags_block}

Write:
1. A 3-4 sentence afternoon outlook (likely trajectory, key levels to watch, what could change the picture)
2. List of current risk flags (keep from input + add any you identify)
3. A 1-sentence watchlist highlight (most interesting setup for the afternoon)

Return ONLY valid JSON:
{{
  "outlook": "3-4 sentences",
  "risk_flags": ["flag1", "flag2"],
  "watchlist_highlight": "1 sentence"
}}

Be specific. Use real numbers. No generic advice."""

    result = _call_groq(prompt, max_tokens=600)
    if result and "outlook" in result:
        logger.info("[MidSession] Outlook: Groq OK")
        return result

    result = _call_gemini(prompt)
    if result and "outlook" in result:
        logger.info("[MidSession] Outlook: Gemini fallback OK")
        return result

    # Mechanical fallback
    logger.warning("[MidSession] Outlook: both LLMs failed, using mechanical fallback")
    return {
        "outlook": f"Phase: {phase}. Nifty {nifty_pct:+.2f}% at midday. {total_signals} signals active, {above_thresh} above threshold. Continue monitoring.",
        "risk_flags": risk_flags if risk_flags else ["None"],
        "watchlist_highlight": f"{total_signals} signals being tracked. System in observation mode.",
    }
