"""
eod_autopsy.py — 4:00 PM End-of-Day Market Autopsy
-----------------------------------------------------
Runs once daily at 16:00 IST (30 min after market close).

For each of the top 10 gainers and top 10 losers:
  1. Pulls intraday 5-min candles from Kite
  2. Computes technicals: EMA 9/20, RSI, MACD, VWAP, ADX, ORB
  3. Identifies the "trigger candle" (where the move started)
  4. Pulls overnight news catalyst via NewsData.io
  5. Feeds everything to Gemini for structured pattern analysis
  6. Saves to persistent pattern_db.json for long-term learning

This is the FEEDBACK LOOP that makes VoltEdge learn from every trading day.
"""
import os
import json
import logging
from datetime import datetime, date, timedelta
from typing import Optional
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Pattern Taxonomy ──────────────────────────────────────────────────────

PATTERN_TYPES = [
    "VWAP_RECLAIM_BREAKOUT",     # Opens weak, reclaims VWAP with volume, breaks out
    "GAP_AND_GO",                # Gaps up on news, holds the gap, continues
    "ORB_BREAKOUT",              # Breaks Opening Range High with volume
    "SECTOR_MOMENTUM",           # No stock-specific catalyst; sector rotation
    "INSTITUTIONAL_ACCUMULATION", # Slow grind up with increasing volume
    "EARNINGS_REACTION",         # Post-earnings gap + continuation
    "VWAP_REJECTION_BREAKDOWN",  # Fails to reclaim VWAP, sells off
    "GAP_FILL_REVERSAL",         # Gaps up but fills the gap by noon
    "ORB_BREAKDOWN",             # Breaks Opening Range Low, sellers pile in
    "UNKNOWN",                   # Could not classify
]


@dataclass
class TechnicalSnapshot:
    """Technicals computed from the intraday chart at the moment of strongest move."""
    ema9: float = 0.0
    ema20: float = 0.0
    ema_alignment: str = ""        # "9 > 20 (bullish)" or "9 < 20 (bearish)"
    rsi_at_trigger: float = 50.0
    macd_crossover_before_move: bool = False
    vwap: float = 0.0
    above_vwap_at_trigger: bool = False
    orb_high: float = 0.0
    orb_low: float = 0.0
    orb_breakout: bool = False
    orb_breakdown: bool = False
    volume_spike_ratio: float = 0.0
    adx: float = 0.0
    trigger_time: str = ""
    trigger_candle_idx: int = -1


@dataclass
class MoverAnalysis:
    """Structured analysis of a single top mover."""
    date: str
    symbol: str
    direction: str           # "GAINER" or "LOSER"
    pct_change: float
    open_price: float = 0.0
    close_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    volume: int = 0
    # Technical analysis
    technicals: Optional[dict] = None
    # News catalyst
    catalyst: str = ""
    news_headlines: list = None
    # AI-generated fields
    pattern_classification: str = "UNKNOWN"
    lesson: str = ""
    gemini_analysis: str = ""

    def __post_init__(self):
        if self.news_headlines is None:
            self.news_headlines = []


# ── Technical Analysis Engine ─────────────────────────────────────────────

def _compute_technicals(bars_df: pd.DataFrame) -> TechnicalSnapshot:
    """Compute all technicals from intraday 5-min bars."""
    snap = TechnicalSnapshot()

    if bars_df is None or bars_df.empty or len(bars_df) < 10:
        return snap

    close = bars_df["close"]
    high = bars_df["high"]
    low = bars_df["low"]
    volume = bars_df["volume"]

    # EMA 9/20
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    snap.ema9 = round(float(ema9.iloc[-1]), 2)
    snap.ema20 = round(float(ema20.iloc[-1]), 2)
    snap.ema_alignment = "9 > 20 (bullish)" if snap.ema9 > snap.ema20 else "9 < 20 (bearish)"

    # RSI (14)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line

    # VWAP
    tp = (high + low + close) / 3.0
    cum_tp_vol = (tp * volume).cumsum()
    cum_vol = volume.cumsum()
    vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
    snap.vwap = round(float(vwap.iloc[-1]), 2)

    # Opening Range (first 3 bars of 5-min = first 15 min)
    or_bars = min(3, len(bars_df))
    snap.orb_high = round(float(high.iloc[:or_bars].max()), 2)
    snap.orb_low = round(float(low.iloc[:or_bars].min()), 2)

    last_close = float(close.iloc[-1])
    snap.orb_breakout = last_close > snap.orb_high
    snap.orb_breakdown = last_close < snap.orb_low

    # ADX (simplified)
    if len(bars_df) >= 14:
        up = high.diff()
        dn = -low.diff()
        plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
        minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/14, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_s = dx.ewm(alpha=1/14, adjust=False).mean()
        snap.adx = round(float(adx_s.iloc[-1]), 1) if not pd.isna(adx_s.iloc[-1]) else 0.0

    # ── Find Trigger Candle ───────────────────────────────────────────────
    # The trigger = the bar where volume spikes 2x+ AND directional candle
    avg_vol = float(volume.mean())
    best_idx = -1
    best_vol_ratio = 0.0

    for i in range(3, len(bars_df)):  # skip first 3 (ORB period)
        bar_vol = float(volume.iloc[i])
        bar_change = float(close.iloc[i] - close.iloc[i - 1])
        vol_ratio = bar_vol / avg_vol if avg_vol > 0 else 0

        if vol_ratio >= 1.5 and abs(bar_change) > 0:
            if vol_ratio > best_vol_ratio:
                best_vol_ratio = vol_ratio
                best_idx = i

    if best_idx >= 0:
        snap.trigger_candle_idx = best_idx
        snap.volume_spike_ratio = round(best_vol_ratio, 2)

        # Get the timestamp of the trigger candle
        if "date" in bars_df.columns:
            snap.trigger_time = str(bars_df["date"].iloc[best_idx])
        else:
            snap.trigger_time = f"bar_{best_idx}"

        # RSI at trigger
        rsi_val = rsi.iloc[best_idx]
        snap.rsi_at_trigger = round(float(rsi_val), 1) if not pd.isna(rsi_val) else 50.0

        # VWAP position at trigger
        snap.above_vwap_at_trigger = float(close.iloc[best_idx]) > float(vwap.iloc[best_idx])

        # MACD crossover before trigger
        for j in range(max(1, best_idx - 5), best_idx):
            if float(macd_hist.iloc[j - 1]) < 0 and float(macd_hist.iloc[j]) > 0:
                snap.macd_crossover_before_move = True
                break

    return snap


# ── Gemini Analysis ────────────────────────────────────────────────────────

AUTOPSY_PROMPT = """You are a senior Indian stock market analyst with 20 years of experience performing daily post-market autopsy.

Analyze this stock that was a top {direction} today and provide a structured analysis.

## Stock Data
- Symbol: {symbol}
- Direction: {direction} ({pct_change:+.2f}%)
- Open: ₹{open_price}, Close: ₹{close_price}, High: ₹{high_price}, Low: ₹{low_price}
- Volume: {volume:,}

## Technical Indicators at Key Moment
- EMA Alignment: {ema_alignment}
- RSI at trigger: {rsi_at_trigger}
- MACD Crossover before move: {macd_crossover}
- Above VWAP at trigger: {above_vwap}
- ORB Breakout: {orb_breakout} | ORB Breakdown: {orb_breakdown}
- Volume Spike Ratio: {vol_spike}x
- ADX: {adx}
- Trigger Time: {trigger_time}

## News Headlines (delayed, from previous session)
{news_headlines}

## Instructions
1. Explain WHY this stock moved {pct_change:+.2f}% today in 1-2 sentences.
2. Classify the pattern as ONE of: {pattern_types}
3. What technical indicator(s) would have predicted this move BEFORE it happened?
4. One actionable lesson for tomorrow.

Return ONLY valid JSON:
{{
  "catalyst": "One sentence explaining the move",
  "pattern_classification": "PATTERN_NAME",
  "predictive_indicators": "Which indicators would have caught this early",
  "lesson": "One actionable lesson"
}}"""


def _generate_gemini_analysis(analysis: MoverAnalysis) -> dict:
    """Use Gemini to generate structured pattern analysis."""
    try:
        import google.generativeai as genai
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return {}

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")

        tech = analysis.technicals or {}
        news_str = "\n".join(f"- {h}" for h in (analysis.news_headlines or [])[:5])
        if not news_str:
            news_str = "(No news headlines available)"

        prompt = AUTOPSY_PROMPT.format(
            direction=analysis.direction,
            symbol=analysis.symbol,
            pct_change=analysis.pct_change,
            open_price=analysis.open_price,
            close_price=analysis.close_price,
            high_price=analysis.high_price,
            low_price=analysis.low_price,
            volume=analysis.volume,
            ema_alignment=tech.get("ema_alignment", "N/A"),
            rsi_at_trigger=tech.get("rsi_at_trigger", "N/A"),
            macd_crossover=tech.get("macd_crossover_before_move", "N/A"),
            above_vwap=tech.get("above_vwap_at_trigger", "N/A"),
            orb_breakout=tech.get("orb_breakout", "N/A"),
            orb_breakdown=tech.get("orb_breakdown", "N/A"),
            vol_spike=tech.get("volume_spike_ratio", "N/A"),
            adx=tech.get("adx", "N/A"),
            trigger_time=tech.get("trigger_time", "N/A"),
            news_headlines=news_str,
            pattern_types=", ".join(PATTERN_TYPES),
        )

        response = model.generate_content(prompt)
        raw = response.text.strip()

        # Parse JSON from response
        json_str = raw
        if "```json" in raw:
            json_str = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            json_str = raw.split("```")[1].split("```")[0].strip()

        return json.loads(json_str)

    except Exception as e:
        logger.error(f"Gemini autopsy analysis failed for {analysis.symbol}: {e}")
        return {}


# ── Pattern Database ──────────────────────────────────────────────────────

PATTERN_DB_PATH = "data/pattern_db.json"


def _load_pattern_db() -> dict:
    if os.path.exists(PATTERN_DB_PATH):
        try:
            with open(PATTERN_DB_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"entries": [], "stats": {}}


def _save_pattern_db(db: dict):
    os.makedirs("data", exist_ok=True)
    with open(PATTERN_DB_PATH, "w") as f:
        json.dump(db, f, indent=2, default=str)


def _update_pattern_stats(db: dict):
    """Compute aggregate stats from the pattern database."""
    entries = db.get("entries", [])
    if not entries:
        return

    # Count by pattern type
    pattern_counts = {}
    indicator_hits = {}

    for entry in entries:
        pat = entry.get("pattern_classification", "UNKNOWN")
        pattern_counts[pat] = pattern_counts.get(pat, 0) + 1

        tech = entry.get("technicals", {})
        if tech.get("macd_crossover_before_move"):
            indicator_hits["MACD_crossover"] = indicator_hits.get("MACD_crossover", 0) + 1
        if tech.get("above_vwap_at_trigger"):
            indicator_hits["above_VWAP"] = indicator_hits.get("above_VWAP", 0) + 1
        if tech.get("orb_breakout"):
            indicator_hits["ORB_breakout"] = indicator_hits.get("ORB_breakout", 0) + 1
        if tech.get("volume_spike_ratio", 0) >= 2.0:
            indicator_hits["volume_spike_2x"] = indicator_hits.get("volume_spike_2x", 0) + 1

    gainer_entries = [e for e in entries if e.get("direction") == "GAINER"]
    loser_entries = [e for e in entries if e.get("direction") == "LOSER"]

    db["stats"] = {
        "total_entries": len(entries),
        "total_gainers": len(gainer_entries),
        "total_losers": len(loser_entries),
        "pattern_distribution": pattern_counts,
        "indicator_frequency": indicator_hits,
        "last_updated": str(datetime.now().date()),
    }


# ── Report Generator ──────────────────────────────────────────────────────

def _generate_report(analyses: list, today: date) -> str:
    """Generate a markdown autopsy report."""
    lines = [f"# VoltEdge EOD Market Autopsy — {today}\n"]

    gainers = [a for a in analyses if a.direction == "GAINER"]
    losers = [a for a in analyses if a.direction == "LOSER"]

    if gainers:
        lines.append("## 🟢 Top Gainers\n")
        lines.append("| # | Symbol | Change | Pattern | Catalyst |")
        lines.append("|---|--------|--------|---------|----------|")
        for i, a in enumerate(gainers, 1):
            lines.append(f"| {i} | {a.symbol} | {a.pct_change:+.2f}% | {a.pattern_classification} | {a.catalyst[:60]}... |")
        lines.append("")

        for a in gainers:
            lines.append(f"### {a.symbol} ({a.pct_change:+.2f}%)")
            lines.append(f"- **Pattern:** {a.pattern_classification}")
            lines.append(f"- **Catalyst:** {a.catalyst}")
            lines.append(f"- **Lesson:** {a.lesson}")
            tech = a.technicals or {}
            lines.append(f"- **Technicals:** EMA={tech.get('ema_alignment', 'N/A')}, RSI={tech.get('rsi_at_trigger', 'N/A')}, VWAP={tech.get('above_vwap_at_trigger', 'N/A')}, Vol={tech.get('volume_spike_ratio', 'N/A')}x")
            lines.append("")

    if losers:
        lines.append("## 🔴 Top Losers\n")
        lines.append("| # | Symbol | Change | Pattern | Catalyst |")
        lines.append("|---|--------|--------|---------|----------|")
        for i, a in enumerate(losers, 1):
            lines.append(f"| {i} | {a.symbol} | {a.pct_change:+.2f}% | {a.pattern_classification} | {a.catalyst[:60]}... |")
        lines.append("")

        for a in losers:
            lines.append(f"### {a.symbol} ({a.pct_change:+.2f}%)")
            lines.append(f"- **Pattern:** {a.pattern_classification}")
            lines.append(f"- **Catalyst:** {a.catalyst}")
            lines.append(f"- **Lesson:** {a.lesson}")
            lines.append("")

    # Pattern stats
    db = _load_pattern_db()
    stats = db.get("stats", {})
    if stats.get("pattern_distribution"):
        lines.append("## 📊 All-Time Pattern Stats\n")
        lines.append("| Pattern | Count |")
        lines.append("|---------|-------|")
        for pat, cnt in sorted(stats["pattern_distribution"].items(), key=lambda x: x[1], reverse=True):
            lines.append(f"| {pat} | {cnt} |")
        lines.append("")

    return "\n".join(lines)


# ── Main Orchestrator ─────────────────────────────────────────────────────

def run_eod_autopsy(kite=None, traded_symbols: set = None, viper_symbols: list = None):
    """
    Main orchestrator. Runs at 16:00 IST.

    Args:
        kite:            KiteConnect instance (for fetching historical data).
                         If None, tries to create one from env.
        traded_symbols:  Set of symbols actually traded today (from runner).
        viper_symbols:   VIPER watchlist symbols (from runner).
    """
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv()

    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).date()

    # Duplicate guard: skip if today's autopsy already exists
    existing_paths = [
        os.path.join("logs", "daily_reports", f"{today}_autopsy.md"),
        os.path.join("logs", "daily_reports", f"voltedge_{today}", f"{today}_autopsy.md"),
    ]
    for ep in existing_paths:
        if os.path.exists(ep):
            print(f"[VoltEdge] Autopsy already exists at {ep} — skipping duplicate generation.")
            return

    logger.info(f"Starting EOD Autopsy for {today}")
    print(f"\n{'='*60}")
    print(f"[16:00] EOD MARKET AUTOPSY — {today}")
    print(f"{'='*60}")

    # ── Step 0: Get Kite client ───────────────────────────────────────────
    if kite is None:
        try:
            from kiteconnect import KiteConnect
            api_key = os.getenv("ZERODHA_API_KEY")
            access_token = os.getenv("ZERODHA_ACCESS_TOKEN")
            if api_key and access_token:
                kite = KiteConnect(api_key=api_key)
                kite.set_access_token(access_token)
            else:
                logger.error("Kite credentials not set — cannot run autopsy.")
                return
        except Exception as e:
            logger.error(f"Failed to initialize Kite client: {e}")
            return

    # ── Step 0b: Build token map ONCE from cached CSV (avoids 20x kite.instruments() calls) ──
    _token_map: dict = {}
    try:
        from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
        _token_map = build_symbol_token_map(load_instruments_csv())
        logger.info(f"Token map loaded: {len(_token_map)} symbols")
    except Exception as e:
        logger.warning(f"Could not load token map from CSV: {e} — will attempt live lookup")

    # ── Step 1: Fetch top movers ──────────────────────────────────────────
    try:
        from src.sniper.momentum_scanner import fetch_top_movers
        movers = fetch_top_movers()
        gainers = movers.get("gainers", [])[:10]
        losers = movers.get("losers", [])[:10]
        print(f"  📈 Gainers: {[c.symbol for c in gainers]}")
        print(f"  📉 Losers:  {[c.symbol for c in losers]}")
    except Exception as e:
        logger.error(f"Failed to fetch top movers: {e}")
        gainers, losers = [], []

    # ── Step 2: Analyze each mover ────────────────────────────────────────
    analyses = []
    news_client = None
    try:
        from src.data_ingestion.news_context import NewsClient
        news_client = NewsClient()
    except Exception:
        pass

    all_movers = [(c, "GAINER") for c in gainers] + [(c, "LOSER") for c in losers]

    for candidate, direction in all_movers:
        sym = candidate.symbol
        try:
            import time
            time.sleep(0.4) # Rate limit protection for Kite API
            print(f"\n  🔬 Analyzing {sym} ({direction})...")

            # A) Fetch intraday data from Kite (5-min candles, full day)
            # Uses pre-built token map — no per-symbol kite.instruments() call
            bars_df = None
            try:
                token = _token_map.get(sym) or _resolve_token(kite, sym, _token_map)
                if token:
                    from_dt = datetime.combine(today, datetime.min.time())
                    to_dt = datetime.combine(today, datetime.max.time())
                    historical = kite.historical_data(
                        instrument_token=token,
                        from_date=from_dt,
                        to_date=to_dt,
                        interval="5minute",
                    )
                    if historical:
                        bars_df = pd.DataFrame(historical)
                else:
                    logger.warning(f"No token found for {sym} — skipping historical fetch")
            except Exception as e:
                logger.warning(f"Could not fetch Kite historical for {sym}: {e}")

            # B) Compute technicals
            tech_snap = _compute_technicals(bars_df)
            tech_dict = asdict(tech_snap)

            # C) Fetch news catalyst (1 NewsData credit)
            headlines = []
            if news_client and direction == "GAINER":  # only for gainers to save credits
                try:
                    news = news_client.fetch_stock_eod_news(sym)
                    headlines = [n.headline for n in news[:5]]
                except Exception:
                    pass

            # D) Build analysis object
            analysis = MoverAnalysis(
                date=str(today),
                symbol=sym,
                direction=direction,
                pct_change=candidate.pct_change,
                open_price=getattr(candidate, 'open_price', 0.0) or 0.0,
                close_price=candidate.last_price,
                high_price=getattr(candidate, 'high_price', 0.0) or 0.0,
                low_price=getattr(candidate, 'low_price', 0.0) or 0.0,
                volume=candidate.volume,
                technicals=tech_dict,
                news_headlines=headlines,
            )

            # E) Gemini AI analysis
            gemini_result = _generate_gemini_analysis(analysis)
            if gemini_result:
                analysis.catalyst = gemini_result.get("catalyst", "")
                analysis.pattern_classification = gemini_result.get("pattern_classification", "UNKNOWN")
                analysis.lesson = gemini_result.get("lesson", "")
                analysis.gemini_analysis = json.dumps(gemini_result)

            analyses.append(analysis)
            print(f"    ✅ {sym}: {analysis.pattern_classification} — {analysis.catalyst[:80]}")

        except Exception as e:
            logger.error(f"Autopsy failed for {sym}: {e}")
            print(f"    ❌ {sym}: Error: {e}")

    # ── Step 3: Save to Pattern Database ──────────────────────────────────
    db = _load_pattern_db()
    for a in analyses:
        entry = asdict(a)
        db["entries"].append(entry)
    # Keep only last 60 days (~1200 entries max)
    db["entries"] = db["entries"][-1200:]
    _update_pattern_stats(db)
    _save_pattern_db(db)
    print(f"\n  💾 Pattern DB updated: {db['stats'].get('total_entries', 0)} total entries")

    # ── Step 4: Generate and save report ──────────────────────────────────
    report_md = _generate_report(analyses, today)
    os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
    report_path = os.path.join("logs", "daily_reports", f"{today}_autopsy.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"  📝 Autopsy report saved: {report_path}")

    # ── Step 5: Email (reuse existing infrastructure) ─────────────────────
    try:
        from src.reports.market_chronicle import _send_email
        _send_email(
            subject=f"VoltEdge EOD Autopsy — {today}",
            report_md=report_md,
            report_path=report_path,
        )
    except Exception as e:
        logger.warning(f"Autopsy email failed: {e}")

    print(f"\n{'='*60}")
    print(f"[16:00] AUTOPSY COMPLETE — {len(analyses)} stocks analyzed")
    print(f"{'='*60}\n")


def _resolve_token(kite, symbol: str, cached_map: dict = None) -> int:
    """
    Resolve NSE instrument token for a symbol.
    Prefers the pre-built cached_map to avoid live API calls.
    """
    # 1. Try pre-built map first (fast, no API call)
    if cached_map:
        token = cached_map.get(symbol, 0)
        if token:
            return token
    # 2. Fallback: CSV (still no live API call)
    try:
        from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
        df = load_instruments_csv()
        token_map = build_symbol_token_map(df)
        token = token_map.get(symbol, 0)
        if token:
            return token
    except Exception:
        pass
    # 3. Last resort: live API (slow — only if CSV is missing/stale)
    try:
        instruments = kite.instruments("NSE")
        for inst in instruments:
            if inst["tradingsymbol"] == symbol:
                return inst["instrument_token"]
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_eod_autopsy()
