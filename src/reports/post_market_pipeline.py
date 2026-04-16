"""
post_market_pipeline.py — Post-Market Report v2 Pipeline
---------------------------------------------------------
5-stage pipeline following brief_pipeline.py architecture:

  Stage 1: Data Collection (parallel, ThreadPoolExecutor)
  Stage 2: LLM Analysis (3 parallel Groq calls)
  Stage 3: Section Assembly (9 sections, deterministic)
  Stage 4: Report Assembly
  Stage 5: Persist + Email

Fallback chain: Groq → Gemini → mechanical (never empty).
Every section generates content every day — zero empty sections.
"""
import os
import csv
import json
import logging
import re
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ── Dataclasses ──────────────────────────────────────────────────────────

@dataclass
class PostMarketData:
    """All raw data collected in Stage 1."""
    conviction_signals: List[dict] = field(default_factory=list)
    dawn_rows: List[dict] = field(default_factory=list)
    dawn_summary: Optional[dict] = None
    predictions: List[dict] = field(default_factory=list)
    closing_prices: Dict[str, dict] = field(default_factory=dict)  # sym -> {close, prev_close}
    nse_movers: Optional[dict] = None
    yesterday_learnings: List[str] = field(default_factory=list)
    pattern_db_stats: dict = field(default_factory=dict)
    db_trades: dict = field(default_factory=dict)
    kite_ok: bool = False
    pre_market_ran: bool = True
    viper_health: str = ""
    api_failures: List[str] = field(default_factory=list)
    grok_call_count: int = 0
    phase_str: str = "UNKNOWN"


@dataclass
class PostMarketAnalysis:
    """Structured analysis from Stage 2 LLM calls."""
    day_summary: dict = field(default_factory=dict)
    movers_analysis: dict = field(default_factory=dict)
    tomorrow_setup: dict = field(default_factory=dict)


# ── NSE Holidays (Addition 3) ───────────────────────────────────────────

NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 10),   # Holi
    date(2026, 3, 30),   # Id-ul-Fitr
    date(2026, 4, 2),    # Ram Navami
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 25),   # Buddha Purnima
    date(2026, 6, 5),    # Eid-ul-Adha (Bakri Id)
    date(2026, 7, 6),    # Muharram
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 17),   # Parsi New Year
    date(2026, 9, 4),    # Milad-un-Nabi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 10, 21),  # Dussehra
    date(2026, 11, 9),   # Diwali (Laxmi Pujan)
    date(2026, 11, 10),  # Diwali (Balipratipada)
    date(2026, 11, 27),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}


def _is_trading_day(d: date) -> bool:
    """Check if a date is a trading day (not weekend, not holiday)."""
    return d.weekday() < 5 and d not in NSE_HOLIDAYS_2026


# ═══════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

def run_post_market_pipeline(
    kite_client=None,
    target_date: Optional[date] = None,
    grok_call_count: int = 0,
    pre_market_ran: bool = True,
    viper_health: str = "",
    conviction_engine=None,
    traded_symbols: Optional[set] = None,
) -> str:
    """
    Run the complete post-market report pipeline.
    Returns the final report markdown string.
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = target_date or datetime.now(IST).date()

    print(f"[VoltEdge] 📊 Post-Market Pipeline v2 starting for {today}")

    # Compute phase from live market snapshot (nifty pct + A/D breadth)
    # Same logic as mid_session_pulse — avoids stale CHOPPY on strong-trending days.
    phase_str = "UNKNOWN"
    try:
        from src.reports.mid_session_pulse import _compute_market_phase
        from src.trading.market_phase import fetch_market_snapshot
        snap = fetch_market_snapshot(kite_client) if kite_client else None
        if snap is not None:
            phase_str = _compute_market_phase(snap.nifty_pct, snap.ad_ratio)
    except Exception as e:
        logger.warning(f"[PostMarket] Phase computation failed: {e}")
        if conviction_engine:
            try:
                phase_str = conviction_engine.phase.value if conviction_engine.phase else "UNKNOWN"
            except Exception:
                pass

    # ── Stage 1: Data Collection ─────────────────────────────────────
    print(f"[VoltEdge] Stage 1: Collecting data...")
    data = _collect_data(kite_client, today, grok_call_count, pre_market_ran, viper_health, conviction_engine, phase_str)

    # ── Stage 2: LLM Analysis ────────────────────────────────────────
    print(f"[VoltEdge] Stage 2: Running parallel LLM analysis...")
    analysis = _run_analysis(data, today)

    # ── Stage 3 + 4: Section Assembly + Report Assembly ──────────────
    print(f"[VoltEdge] Stage 3-4: Assembling sections...")
    sections, updated_predictions, learnings = _build_all_sections(data, analysis, today)
    report_md = _assemble_report(today, sections)

    # ── Stage 5: Persist + Email ─────────────────────────────────────
    print(f"[VoltEdge] Stage 5: Persisting and emailing...")
    _persist_and_email(today, report_md, updated_predictions, learnings)

    print(f"[VoltEdge] ✅ Post-Market Pipeline v2 complete for {today}")
    return report_md


# ═══════════════════════════════════════════════════════════════════════
# STAGE 1: DATA COLLECTION (parallel)
# ═══════════════════════════════════════════════════════════════════════

def _collect_data(
    kite_client,
    today: date,
    grok_call_count: int,
    pre_market_ran: bool,
    viper_health: str,
    conviction_engine,
    phase_str: str,
) -> PostMarketData:
    """Fetch all data sources in parallel."""
    data = PostMarketData(
        pre_market_ran=pre_market_ran,
        viper_health=viper_health,
        grok_call_count=grok_call_count,
        phase_str=phase_str,
    )

    # Determine kite health
    if kite_client:
        try:
            kite_client.ltp("NSE:NIFTY 50")
            data.kite_ok = True
        except Exception as e:
            data.api_failures.append(f"Kite LTP: {e}")
    else:
        data.api_failures.append("Kite client not available")

    def _fetch_conviction_signals():
        path = os.path.join("logs", "conviction_signals", f"{today}_signals.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[PostMarket/S1] Conviction signals load failed: {e}")
            return []

    def _fetch_dawn_data():
        csv_path = os.path.join("logs", "dawn_dryrun", f"{today}.csv")
        if not os.path.exists(csv_path):
            return [], None
        try:
            rows = []
            summary = None
            with open(csv_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("symbol", "").upper() == "SUMMARY":
                        summary = row
                    else:
                        rows.append(row)
            return rows, summary
        except Exception as e:
            logger.warning(f"[PostMarket/S1] DAWN CSV load failed: {e}")
            return [], None

    def _fetch_predictions_and_prices():
        """Load predictions AND fetch closing prices for ALL symbols."""
        predictions = []
        closing_prices = {}

        # Load predictions
        pred_path = "data/prediction_log.json"
        if os.path.exists(pred_path):
            try:
                with open(pred_path) as f:
                    log = json.load(f)
                predictions = [
                    p for p in log.get("predictions", [])
                    if p.get("date") == str(today)
                ]
            except Exception as e:
                logger.warning(f"[PostMarket/S1] Prediction load failed: {e}")

        return predictions, closing_prices

    def _fetch_nse_movers():
        try:
            from src.data_ingestion.nse_scraper import get_nse_top_gainers_losers
            return get_nse_top_gainers_losers(count=5)
        except Exception as e:
            logger.warning(f"[PostMarket/S1] NSE movers failed: {e}")
            return None

    def _fetch_yesterday_learnings():
        path = "data/system_learnings.json"
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                all_learnings = json.load(f)
            yesterday = today - timedelta(days=1)
            # Look back up to 3 days for last trading day
            for i in range(3):
                d = today - timedelta(days=1 + i)
                key = str(d)
                if key in all_learnings:
                    return all_learnings[key]
            return []
        except Exception as e:
            logger.warning(f"[PostMarket/S1] Learnings load failed: {e}")
            return []

    def _fetch_pattern_db_stats():
        path = "data/pattern_db.json"
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                records = json.load(f)
            if not isinstance(records, list):
                return {}
            total = len(records)
            wins = sum(1 for r in records if r.get("outcome") == "WIN")
            losses = sum(1 for r in records if r.get("outcome") == "LOSS")
            correct_nt = sum(1 for r in records if r.get("outcome") == "CORRECT_NO_TRADE")
            wrong_nt = sum(1 for r in records if r.get("outcome") == "WRONG_NO_TRADE")
            return {
                "total": total, "wins": wins, "losses": losses,
                "correct_no_trade": correct_nt, "wrong_no_trade": wrong_nt,
            }
        except Exception as e:
            logger.warning(f"[PostMarket/S1] Pattern DB load failed: {e}")
            return {}

    def _fetch_db_trades():
        try:
            from src.reports.post_market_report import _fetch_db_trades
            return _fetch_db_trades(today)
        except Exception as e:
            logger.warning(f"[PostMarket/S1] DB trades fetch failed: {e}")
            return {"stats": {"num_trades": 0, "day_pnl": 0, "win_rate_pct": 0}, "trades": []}

    # Run all fetches in parallel
    with ThreadPoolExecutor(max_workers=7, thread_name_prefix="PostMarketS1") as pool:
        futures = {
            pool.submit(_fetch_conviction_signals): "signals",
            pool.submit(_fetch_dawn_data): "dawn",
            pool.submit(_fetch_predictions_and_prices): "predictions",
            pool.submit(_fetch_nse_movers): "movers",
            pool.submit(_fetch_yesterday_learnings): "learnings",
            pool.submit(_fetch_pattern_db_stats): "pattern_db",
            pool.submit(_fetch_db_trades): "db_trades",
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                if name == "signals":
                    data.conviction_signals = result or []
                elif name == "dawn":
                    data.dawn_rows, data.dawn_summary = result
                elif name == "predictions":
                    data.predictions, data.closing_prices = result
                elif name == "movers":
                    data.nse_movers = result
                elif name == "learnings":
                    data.yesterday_learnings = result or []
                elif name == "pattern_db":
                    data.pattern_db_stats = result or {}
                elif name == "db_trades":
                    data.db_trades = result or {"stats": {}, "trades": []}
            except Exception as e:
                logger.warning(f"[PostMarket/S1] {name} future failed: {e}")

    # Now fetch closing prices via Kite (needs symbols from predictions + signals)
    # Addition 2: use kite.ohlc() for official closing prices
    all_symbols = set()
    for p in data.predictions:
        sym = p.get("symbol", "")
        if sym:
            all_symbols.add(sym)
    for s in data.conviction_signals:
        sym = s.get("symbol", "")
        if sym:
            all_symbols.add(sym)
    # Also add dawn symbols
    for d in data.dawn_rows:
        sym = d.get("symbol", "")
        if sym:
            all_symbols.add(sym)

    if all_symbols and kite_client and data.kite_ok:
        nse_keys = [f"NSE:{sym}" for sym in all_symbols]
        ohlc_count = 0
        ltp_count = 0
        unavailable_count = 0

        try:
            # Primary: kite.ohlc() for official exchange closing price
            ohlc_data = kite_client.ohlc(nse_keys)
            for sym in all_symbols:
                key = f"NSE:{sym}"
                if key in ohlc_data:
                    item = ohlc_data[key]
                    ohlc = item.get("ohlc", {})
                    data.closing_prices[sym] = {
                        "close": ohlc.get("close", 0),
                        "prev_close": ohlc.get("open", 0),  # Use open as proxy if prev_close unavailable
                        "last_price": item.get("last_price", 0),
                    }
                    # Prefer last_price if close is 0 (intraday call)
                    if data.closing_prices[sym]["close"] == 0:
                        data.closing_prices[sym]["close"] = item.get("last_price", 0)
                    ohlc_count += 1
                else:
                    unavailable_count += 1
            logger.info(
                f"[PostMarket] Closing prices: {ohlc_count}/{len(all_symbols)} symbols via ohlc, "
                f"{ltp_count} via ltp_cache, {unavailable_count} unavailable"
            )
        except Exception as e:
            logger.warning(f"[PostMarket] ohlc() failed ({e}), trying ltp() fallback")
            # Fallback: ltp
            try:
                ltp_data = kite_client.ltp(nse_keys)
                for sym in all_symbols:
                    key = f"NSE:{sym}"
                    if key in ltp_data and sym not in data.closing_prices:
                        data.closing_prices[sym] = {
                            "close": ltp_data[key].get("last_price", 0),
                            "prev_close": 0,
                            "last_price": ltp_data[key].get("last_price", 0),
                        }
                        ltp_count += 1
                    elif sym not in data.closing_prices:
                        unavailable_count += 1
                logger.info(
                    f"[PostMarket] Closing prices: {ohlc_count}/{len(all_symbols)} symbols via ohlc, "
                    f"{ltp_count} via ltp_cache, {unavailable_count} unavailable"
                )
            except Exception as e2:
                logger.error(f"[PostMarket] Both ohlc() and ltp() failed: {e2}")

    logger.info(
        f"[PostMarket/S1] Data collected: signals={len(data.conviction_signals)}, "
        f"dawn={len(data.dawn_rows)}, predictions={len(data.predictions)}, "
        f"prices={len(data.closing_prices)}, movers={data.nse_movers is not None}"
    )
    return data


# ═══════════════════════════════════════════════════════════════════════
# STAGE 2: LLM ANALYSIS (3 parallel calls)
# ═══════════════════════════════════════════════════════════════════════

def _run_analysis(data: PostMarketData, today: date) -> PostMarketAnalysis:
    """Run 3 parallel LLM analysis calls with Groq→Gemini→mechanical fallback."""
    from src.llm.brief_analyzer import _call_groq, _call_gemini

    analysis = PostMarketAnalysis()

    # Build context summaries
    signal_count = len(data.conviction_signals)
    triggered = sum(1 for s in data.conviction_signals if s.get("status") == "TRIGGERED")
    expired = sum(1 for s in data.conviction_signals if s.get("status") == "EXPIRED")
    trade_stats = data.db_trades.get("stats", {})
    dawn_count = len(data.dawn_rows)
    dawn_wins = sum(1 for r in data.dawn_rows if float(r.get("pnl_pct", "0").replace("+", "")) > 0)
    dawn_losses = sum(1 for r in data.dawn_rows if float(r.get("pnl_pct", "0").replace("+", "")) < 0)
    pred_count = len(data.predictions)

    # ── Call 1: Day Summary ──────────────────────────────────────────
    def _call_day_summary():
        context = (
            f"Date: {today}\n"
            f"Market phase: {data.phase_str}\n"
            f"Conviction signals: {signal_count} total, {triggered} triggered, {expired} expired\n"
            f"Trades executed: {trade_stats.get('num_trades', 0)}, "
            f"P&L: {trade_stats.get('day_pnl', 0):+.2f}, "
            f"Win rate: {trade_stats.get('win_rate_pct', 0):.1f}%\n"
            f"DAWN signals: {dawn_count} ({dawn_wins}W {dawn_losses}L)\n"
            f"Morning predictions: {pred_count}\n"
        )
        if data.yesterday_learnings:
            context += f"Yesterday's learnings: {'; '.join(data.yesterday_learnings[:3])}\n"

        prompt = f"""You are VoltEdge's post-market analyst. Summarize today's trading session.

{context}

Write 3-4 paragraphs covering:
1. Overall market behavior and system performance
2. Signal quality and conviction engine effectiveness
3. DAWN strategy performance (if signals exist)
4. Key takeaways

Return ONLY valid JSON:
{{"narrative": "3-4 paragraphs", "market_phase": "{data.phase_str}"}}"""

        result = _call_groq(prompt, max_tokens=800)
        if result and "narrative" in result:
            return result
        result = _call_gemini(prompt)
        if result and "narrative" in result:
            return result
        # Mechanical fallback
        num_trades = trade_stats.get('num_trades', 0)
        day_pnl = trade_stats.get('day_pnl', 0)
        trades_line = "No trades were executed." if num_trades == 0 else f"{num_trades} trades executed with day P&L of {day_pnl:+.2f}."
        dawn_line = f"DAWN generated {dawn_count} signals ({dawn_wins}W {dawn_losses}L)." if dawn_count else "DAWN did not generate signals today."
        return {
            "narrative": (
                f"Market was in **{data.phase_str}** phase today. "
                f"The conviction engine detected {signal_count} signals — "
                f"{triggered} triggered, {expired} expired. "
                f"{trades_line} {dawn_line}"
            ),
            "market_phase": data.phase_str,
        }

    # ── Call 2: Top Movers Deep Analysis ─────────────────────────────
    def _call_movers_analysis():
        if not data.nse_movers:
            return {"analyses": []}

        gainers = data.nse_movers.get("gainers", [])[:5]
        losers = data.nse_movers.get("losers", [])[:5]

        # Cross-reference movers with conviction signals
        signal_lookup = {}
        for s in data.conviction_signals:
            sym = s.get("symbol", "")
            history = s.get("conviction_history", [])
            peak = max((h[1] for h in history), default=s.get("last_conviction", 0))
            signal_lookup[sym] = {
                "direction": s.get("direction", "?"),
                "strategy": s.get("strategy", "?"),
                "peak_conv": peak,
                "created_at": str(s.get("created_at", "?"))[:16],
            }

        # Check if Grok budget allows deep analysis
        from src.llm.grok_client import GROK_DAILY_BUDGET
        use_grok = data.grok_call_count < GROK_DAILY_BUDGET

        if use_grok:
            try:
                from src.llm.grok_client import grok_deep_analysis
                grok_result = grok_deep_analysis(
                    top_gainers=gainers,
                    top_losers=losers,
                    watchboard_signals=data.conviction_signals,
                    market_summary=f"Phase: {data.phase_str}",
                    current_call_count=data.grok_call_count,
                )
                if grok_result and grok_result.get("mover_analysis"):
                    analyses = []
                    for m in grok_result["mover_analysis"]:
                        sym = m.get("symbol", "")
                        sig = signal_lookup.get(sym)
                        analyses.append({
                            "symbol": sym,
                            "pct_change": m.get("move_pct", 0),
                            "reason": m.get("root_cause", "Unknown"),
                            "system_caught": bool(sig),
                            "caught_by": f"{sig['strategy']} at {sig['created_at']}, conv {sig['peak_conv']:.0f}" if sig else "Not detected",
                            "in_dawn": any(d.get("symbol") == sym for d in data.dawn_rows),
                            "what_was_missed": m.get("gap_to_close", "N/A") if not sig else "Correct behavior",
                        })
                    return {"analyses": analyses}
            except Exception as e:
                logger.warning(f"[PostMarket/S2] Grok movers analysis failed: {e}")

        # Groq fallback with Brave Search reasons
        try:
            from src.llm.brief_analyzer import fetch_mover_reasons
            reasons = fetch_mover_reasons(gainers, losers, max_per_side=3)
        except Exception:
            reasons = {}

        analyses = []
        for mover_list, default_pct in [(gainers, 1.0), (losers, -1.0)]:
            for m in mover_list:
                sym = m.get("symbol", "")
                sig = signal_lookup.get(sym)
                analyses.append({
                    "symbol": sym,
                    "pct_change": m.get("pct_change", default_pct),
                    "reason": reasons.get(sym, "No catalyst identified"),
                    "system_caught": bool(sig),
                    "caught_by": f"{sig['strategy']} at {sig['created_at']}, conv {sig['peak_conv']:.0f}" if sig else "Not detected",
                    "in_dawn": any(d.get("symbol") == sym for d in data.dawn_rows),
                    "what_was_missed": "N/A" if sig else "Not on watchboard — catalyst missed or below threshold",
                })
        return {"analyses": analyses}

    # ── Call 3: Tomorrow's Setup ─────────────────────────────────────
    def _call_tomorrow_setup():
        # Structured context: gainers/losers with catalysts
        gainers_ctx = []
        losers_ctx = []
        if data.nse_movers:
            for g in data.nse_movers.get("gainers", [])[:5]:
                gainers_ctx.append(f"{g.get('symbol', '?')} ({g.get('pct_change', 0):+.2f}%)")
            for l_ in data.nse_movers.get("losers", [])[:5]:
                losers_ctx.append(f"{l_.get('symbol', '?')} ({l_.get('pct_change', 0):+.2f}%)")

        # Expired signals with peak conviction (would-have-been tomorrow setups)
        expired_peaks = []
        for s in data.conviction_signals:
            if s.get("status") == "EXPIRED":
                history = s.get("conviction_history", [])
                peak = max((h[1] for h in history), default=s.get("last_conviction", 0))
                expired_peaks.append(
                    f"{s.get('symbol', '?')} {s.get('direction', '?')} peaked at {peak:.0f}"
                )
        expired_peaks = expired_peaks[:5]

        # High-urgency filings received today (preview of tomorrow's potential)
        filings_ctx = []
        try:
            from src.data_ingestion.exchange_filings import fetch_exchange_filings
            filings = fetch_exchange_filings(hours_back=24) or []
            high = [f for f in filings if getattr(f, "urgency", 0) >= 7][:5]
            for f in high:
                filings_ctx.append(
                    f"{getattr(f, 'symbol', '?')} — {getattr(f, 'category', '?')} "
                    f"(urgency {getattr(f, 'urgency', 0):.0f})"
                )
        except Exception as _fe:
            logger.warning(f"[PostMarket/S2] Filings fetch for tomorrow setup failed: {_fe}")

        # Dynamic conviction threshold
        try:
            from src.config.risk import load_risk_config
            threshold = load_risk_config().dry_run_conviction_threshold
        except Exception:
            threshold = 55.0

        context = (
            f"Today's final phase: {data.phase_str}\n"
            f"Pattern DB: {data.pattern_db_stats.get('wins', 0)}W "
            f"{data.pattern_db_stats.get('losses', 0)}L / "
            f"{data.pattern_db_stats.get('total', 0)} total\n"
            f"DAWN: {dawn_count} signals, {dawn_wins}W {dawn_losses}L\n"
            f"Conviction signals: {signal_count}, {triggered} triggered (threshold={threshold:.0f})\n"
        )
        if gainers_ctx:
            context += f"Top gainers today: {', '.join(gainers_ctx)}\n"
        if losers_ctx:
            context += f"Top losers today: {', '.join(losers_ctx)}\n"
        if expired_peaks:
            context += f"Expired signals today (peak conviction): {'; '.join(expired_peaks)}\n"
        if filings_ctx:
            context += f"High-urgency filings today (potential tomorrow catalysts): {'; '.join(filings_ctx)}\n"
        if data.yesterday_learnings:
            context += f"Yesterday's learnings: {'; '.join(data.yesterday_learnings[:3])}\n"

        prompt = f"""You are VoltEdge's market strategist. Based on today's data, set up for tomorrow.

{context}

Instructions:
- Reference specific stocks and price levels from the data above.
- Name at least 2 specific stocks to watch tomorrow with reason.
- Do NOT use generic phrases like 'remains volatile' or 'cautious approach'.
- End regime_expectation with: Top 3 stocks to watch tomorrow: [specific names with reasons]

Return ONLY valid JSON:
{{
  "regime_expectation": "2-3 sentences grounded in today's phase + top movers + expired signals + filings. MUST end with 'Top 3 stocks to watch tomorrow: [name1 — reason, name2 — reason, name3 — reason]'",
  "sectors_to_watch": ["sector1", "sector2"],
  "events_tomorrow": "Known upcoming events, filings received today that resolve tomorrow, or 'No major events scheduled'",
  "system_readiness": "1-2 sentences on system tuning recommendations tied to today's threshold={threshold:.0f} performance"
}}"""

        result = _call_groq(prompt, max_tokens=700)
        if result and "regime_expectation" in result:
            return result
        result = _call_gemini(prompt)
        if result and "regime_expectation" in result:
            return result
        # Mechanical fallback — still grounded in actual data
        watch_list = []
        for ep in expired_peaks[:3]:
            watch_list.append(ep)
        for fc in filings_ctx[:3]:
            watch_list.append(fc)
        watch_str = "; ".join(watch_list[:3]) if watch_list else "no specific names — signals sparse"
        return {
            "regime_expectation": (
                f"Market closed in {data.phase_str} phase. "
                f"Expired signals and filings suggest watching: {watch_str}. "
                f"Top 3 stocks to watch tomorrow: {watch_str}."
            ),
            "sectors_to_watch": [],
            "events_tomorrow": (
                f"{len(filings_ctx)} high-urgency filings received today: "
                f"{'; '.join(filings_ctx[:3])}" if filings_ctx
                else "Check NSE corporate announcements for scheduled events."
            ),
            "system_readiness": (
                f"Pattern DB: {data.pattern_db_stats.get('total', 0)} records. "
                f"Conviction threshold at {threshold:.0f}. DAWN {'active' if dawn_count else 'standby'}."
            ),
        }

    # Run all 3 analysis calls in parallel
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="PostMarketS2") as pool:
        f_summary = pool.submit(_call_day_summary)
        f_movers = pool.submit(_call_movers_analysis)
        f_tomorrow = pool.submit(_call_tomorrow_setup)

        try:
            analysis.day_summary = f_summary.result() or {}
        except Exception as e:
            logger.warning(f"[PostMarket/S2] Day summary failed: {e}")
            analysis.day_summary = {
                "narrative": f"Day summary generation failed. Market phase: {data.phase_str}.",
                "market_phase": data.phase_str,
            }

        try:
            analysis.movers_analysis = f_movers.result() or {}
        except Exception as e:
            logger.warning(f"[PostMarket/S2] Movers analysis failed: {e}")

        try:
            analysis.tomorrow_setup = f_tomorrow.result() or {}
        except Exception as e:
            logger.warning(f"[PostMarket/S2] Tomorrow setup failed: {e}")
            analysis.tomorrow_setup = {
                "regime_expectation": "Analysis unavailable.",
                "sectors_to_watch": [],
                "events_tomorrow": "Check NSE for events.",
                "system_readiness": "Standard operation.",
            }

    return analysis


# ═══════════════════════════════════════════════════════════════════════
# STAGE 3: SECTION ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════

def _build_all_sections(
    data: PostMarketData,
    analysis: PostMarketAnalysis,
    today: date,
) -> Tuple[List[str], List[dict], List[str]]:
    """
    Build all 9 sections. Returns (sections_list, updated_predictions, learnings).
    """
    sections = []

    # Section 0: System Health
    sections.append(_build_section_0_health(data, today))

    # Section 1: Day Summary
    sections.append(_build_section_1_day_summary(analysis))

    # Section 2: Morning Brief Accuracy Check
    s2, updated_predictions = _build_section_2_predictions(data, today)
    sections.append(s2)

    # Section 3: Conviction Signals Post-Mortem
    sections.append(_build_section_3_conviction_postmortem(data))

    # Section 4: DAWN Post-Mortem
    sections.append(_build_section_4_dawn_postmortem(data))

    # Section 5: Top Gainers & Losers Deep Analysis
    sections.append(_build_section_5_movers(data, analysis))

    # Section 6: Trades Executed
    sections.append(_build_section_6_trades(data))

    # Section 7: Key Learnings
    learnings = _generate_learnings(data, updated_predictions, today)
    sections.append(_build_section_7_learnings(learnings))

    # Section 8: Tomorrow's Setup
    sections.append(_build_section_8_tomorrow(analysis))

    return sections, updated_predictions, learnings


# ── Section 0: System Health ─────────────────────────────────────────

def _build_section_0_health(data: PostMarketData, today: date) -> str:
    """Section 0: System Health — ALWAYS populated."""
    try:
        from src.reports.email_sender import validate_email_config
        email_status = validate_email_config()
        if "Email: " in email_status:
            email_status = email_status.split("Email: ")[1]
    except Exception:
        email_status = "Unknown"

    dawn_count = len(data.dawn_rows)
    dawn_wins = sum(1 for r in data.dawn_rows if float(r.get("pnl_pct", "0").replace("+", "")) > 0)
    dawn_losses = sum(1 for r in data.dawn_rows if float(r.get("pnl_pct", "0").replace("+", "")) < 0)

    from src.llm.grok_client import GROK_DAILY_BUDGET

    pdb = data.pattern_db_stats
    pdb_str = (
        f"{pdb.get('wins', 0)}W {pdb.get('losses', 0)}L / {pdb.get('total', 0)} total"
        if pdb else "No data"
    )

    # FIX-1: Pre-market brief health — verify file exists and has content
    brief_path = os.path.join("logs", "daily_reports", f"{today}_morning_brief.md")
    if os.path.exists(brief_path) and os.path.getsize(brief_path) > 500:
        brief_status = "✅ Generated"
    else:
        brief_status = "❌ Missing"

    # FIX-3: VIPER scan health — read from today's conviction_signals log
    signals_path = os.path.join("logs", "conviction_signals", f"{today}_signals.json")
    if os.path.exists(signals_path):
        try:
            with open(signals_path, encoding="utf-8") as _sf:
                _sig_data = json.load(_sf)
            viper_sigs = [s for s in _sig_data if str(s.get("strategy", "")).upper() == "VIPER"]
            scan_count = len(_sig_data)
            viper_health_str = f"{scan_count} signals logged ({len(viper_sigs)} VIPER)"
        except Exception as _se:
            viper_health_str = f"signals file unreadable: {_se}"
    else:
        viper_health_str = "No signals file found"

    lines = [
        "## 0. System Health\n",
        "| Component | Status |",
        "|-----------|--------|",
        f"| Email | {email_status} |",
        f"| Kite Token | {'Valid' if data.kite_ok else 'EXPIRED / MISSING'} |",
        f"| Pre-Market Brief | {brief_status} |",
        f"| VIPER Scan Health | {viper_health_str} |",
        f"| DAWN Status | {dawn_count} signals / {dawn_wins}W {dawn_losses}L |" if dawn_count else "| DAWN Status | No signals today |",
        f"| Grok Budget | {data.grok_call_count}/{GROK_DAILY_BUDGET} used |",
        f"| Pattern DB | {pdb_str} |",
    ]
    if data.api_failures:
        lines.append(f"| API Failures | {'; '.join(data.api_failures[:5])} |")
    else:
        lines.append("| API Failures | None |")

    return "\n".join(lines)


# ── Section 1: Day Summary ──────────────────────────────────────────

def _build_section_1_day_summary(analysis: PostMarketAnalysis) -> str:
    """Section 1: Day Summary — LLM narrative."""
    narrative = analysis.day_summary.get("narrative", "Day summary unavailable.")
    return f"## 1. Day Summary\n\n{narrative}"


# ── Section 2: Morning Brief Accuracy Check ─────────────────────────

def _build_section_2_predictions(
    data: PostMarketData, today: date
) -> Tuple[str, List[dict]]:
    """Section 2: Score predictions against actual closing prices."""
    predictions = data.predictions
    if not predictions:
        return (
            "## 2. Morning Brief Accuracy Check\n\n"
            "No morning predictions saved today (brief failed or no parseable predictions).",
            [],
        )

    # Score each prediction
    scored = []
    correct = 0
    wrong = 0
    flat = 0

    for p in predictions:
        sym = p.get("symbol", "")
        direction = (p.get("predicted_direction") or p.get("direction", "")).lower()
        key_level = p.get("key_level", 0)
        try:
            key_level = float(key_level) if key_level else 0
        except (ValueError, TypeError):
            key_level = 0

        price_data = data.closing_prices.get(sym, {})
        close_price = price_data.get("close", 0)
        prev_close = price_data.get("prev_close", 0)

        if close_price and prev_close and prev_close > 0:
            actual_pct = ((close_price - prev_close) / prev_close) * 100
        elif close_price and key_level and key_level > 0:
            actual_pct = ((close_price - key_level) / key_level) * 100
        else:
            actual_pct = None

        # Score
        if actual_pct is not None:
            if direction == "bullish" and actual_pct > 0.5:
                score = 1
                correct += 1
            elif direction == "bearish" and actual_pct < -0.5:
                score = 1
                correct += 1
            elif abs(actual_pct) <= 0.5:
                score = 0
                flat += 1
            else:
                score = -1
                wrong += 1
        else:
            score = None

        p_copy = dict(p)
        p_copy["actual_change_pct"] = round(actual_pct, 2) if actual_pct is not None else None
        p_copy["score"] = score
        scored.append(p_copy)

    # Build table
    lines = [
        "## 2. Morning Brief Accuracy Check\n",
        "| Symbol | Predicted | Key Level | Close | Actual% | Verdict |",
        "|--------|-----------|-----------|-------|---------|---------|",
    ]
    for p in scored:
        direction = (p.get("predicted_direction") or p.get("direction", "?")).upper()
        key_level = p.get("key_level", "?")
        close = data.closing_prices.get(p.get("symbol", ""), {}).get("close", 0)
        actual = p.get("actual_change_pct")
        score = p.get("score")

        close_str = f"{close:.2f}" if close else "N/A"
        actual_str = f"{actual:+.2f}%" if actual is not None else "N/A"
        if score == 1:
            verdict = "CORRECT ✅"
        elif score == -1:
            verdict = "WRONG ❌"
        elif score == 0:
            verdict = "FLAT ➖"
        else:
            verdict = "Unscored"

        lines.append(
            f"| {p.get('symbol', '?')} | {direction} | {key_level} | {close_str} | {actual_str} | {verdict} |"
        )

    # Today's accuracy
    total_scored = correct + wrong + flat
    today_str = f"Today: {correct}/{total_scored} correct" if total_scored else "Today: No scored predictions"

    # Running 10 trading day accuracy (Addition 3: filter by trading days only)
    running_str = _compute_running_accuracy(today)

    lines.append(f"\n**{today_str} | {running_str}**")

    return "\n".join(lines), scored


def _compute_running_accuracy(today: date) -> str:
    """Compute running accuracy over last 10 TRADING days."""
    try:
        path = "data/prediction_log.json"
        if not os.path.exists(path):
            return "Running accuracy: No historical data"

        with open(path) as f:
            log = json.load(f)

        all_preds = log.get("predictions", [])
        if not all_preds:
            return "Running accuracy: No historical data"

        # Find last 10 trading days with predictions
        trading_days = []
        d = today
        max_lookback = 30  # Look back max 30 calendar days
        for _ in range(max_lookback):
            if _is_trading_day(d):
                trading_days.append(str(d))
            if len(trading_days) >= 10:
                break
            d -= timedelta(days=1)

        # Filter predictions to those 10 trading days
        relevant = [p for p in all_preds if p.get("date") in trading_days]
        scored = [p for p in relevant if p.get("score") is not None]

        if not scored:
            return "Running accuracy (last 10 trading days): No scored data"

        correct = sum(1 for p in scored if p.get("score") == 1)
        total = len(scored)
        pct = round(correct / total * 100, 1) if total else 0

        return f"Running accuracy (last 10 trading days): {pct}% ({correct}/{total})"
    except Exception as e:
        logger.warning(f"[PostMarket] Running accuracy calc failed: {e}")
        return "Running accuracy: Calculation failed"


# ── Section 3: Conviction Signals Post-Mortem ────────────────────────

def _build_section_3_conviction_postmortem(data: PostMarketData) -> str:
    """Section 3: Conviction post-mortem with direction verdicts."""
    signals = data.conviction_signals
    if not signals:
        return (
            "## 3. Conviction Signals Post-Mortem\n\n"
            f"No signals entered the watchboard today. Phase: {data.phase_str}."
        )

    lines = [
        "## 3. Conviction Signals Post-Mortem\n",
        "| Symbol | Dir | Strategy | Detected | Peak Conv | Actual Move | Verdict |",
        "|--------|-----|----------|----------|-----------|-------------|---------|",
    ]

    correct_direction = 0
    wrong_direction = 0
    no_data = 0
    threshold_blocked = 0

    for s in signals:
        sym = s.get("symbol", "?")
        direction = s.get("direction", "?")
        strategy = s.get("strategy", "?")
        status = s.get("status", "?")
        created = str(s.get("created_at", "?"))[:16]
        history = s.get("conviction_history", [])
        peak = max((h[1] for h in history), default=s.get("last_conviction", 0))

        # Get actual move
        price_data = data.closing_prices.get(sym, {})
        close_price = price_data.get("close", 0)
        prev_close = price_data.get("prev_close", 0)

        if close_price and prev_close and prev_close > 0:
            actual_move = ((close_price - prev_close) / prev_close) * 100
            actual_str = f"{actual_move:+.2f}%"

            # Determine verdict
            direction_correct = (
                (direction == "BUY" and actual_move > 0.3) or
                (direction == "SHORT" and actual_move < -0.3)
            )

            if status == "TRIGGERED":
                if direction_correct:
                    verdict = "CORRECT-CAUGHT ✅"
                    correct_direction += 1
                else:
                    verdict = "WRONG-CAUGHT ❌"
                    wrong_direction += 1
            else:  # EXPIRED or other
                if direction_correct:
                    verdict = f"CORRECT-MISSED ⚠️"
                    correct_direction += 1
                    threshold_blocked += 1
                else:
                    verdict = "WRONG-MISSED ➖"
                    wrong_direction += 1
        else:
            actual_str = "N/A"
            verdict = "NO-DATA"
            no_data += 1

        lines.append(
            f"| {sym} | {direction} | {strategy} | {created} | {peak:.0f} | {actual_str} | {verdict} |"
        )

    # Summary
    total = len(signals)
    lines.append(
        f"\n**{correct_direction}/{total - no_data} correct direction. "
        f"Threshold blocked {threshold_blocked} signals.**"
    )

    # Conviction evolution for high-conviction signals
    high_conv = [s for s in signals if any(h[1] > 50 for h in s.get("conviction_history", []))]
    if high_conv:
        lines.append("\n**Conviction Evolution (signals > 50):**\n")
        for s in high_conv[:5]:
            history = s.get("conviction_history", [])
            timeline = " → ".join(f"{h[0]}:{h[1]:.0f}({h[2]})" for h in history[-8:])
            lines.append(f"- **{s.get('symbol', '?')}** ({s.get('direction', '?')}): {timeline}")

    return "\n".join(lines)


# ── Section 4: DAWN Post-Mortem ──────────────────────────────────────

def _build_section_4_dawn_postmortem(data: PostMarketData) -> str:
    """Section 4: DAWN strategy post-mortem from CSV data."""
    if not data.dawn_rows:
        return "## 4. DAWN Post-Mortem\n\nDAWN did not generate signals today."

    lines = ["## 4. DAWN Post-Mortem\n"]

    total = len(data.dawn_rows)
    wins = 0
    losses = 0
    total_pnl = 0.0
    best_sym = ""
    best_pnl = -999.0
    worst_sym = ""
    worst_pnl = 999.0

    for row in data.dawn_rows:
        sym = row.get("symbol", "?")
        direction = row.get("direction", "?")
        catalyst = row.get("catalyst", "?")
        dawn_score = row.get("dawn_score", "?")
        entry = row.get("entry_price", "0")
        exit_p = row.get("exit_price", "0")
        sl_type = row.get("sl_type", "?")
        pnl_str = row.get("pnl_pct", "0").replace("+", "")
        pnl_rupees = row.get("pnl_rupees", "0").replace("+", "")
        status = row.get("status", "?")
        exit_reason = row.get("exit_reason", "?")
        max_fav = row.get("max_favorable", "0").replace("+", "")
        max_adv = row.get("max_adverse", "0").replace("+", "")
        liq_warn = row.get("liquidity_warning", "False")

        try:
            pnl_val = float(pnl_str)
        except (ValueError, TypeError):
            pnl_val = 0.0

        try:
            pnl_r = float(pnl_rupees)
        except (ValueError, TypeError):
            pnl_r = 0.0

        total_pnl += pnl_r
        if pnl_val > 0:
            wins += 1
        elif pnl_val < 0:
            losses += 1

        if pnl_val > best_pnl:
            best_pnl = pnl_val
            best_sym = sym
        if pnl_val < worst_pnl:
            worst_pnl = pnl_val
            worst_sym = sym

        # Parse score components from dawn_score if it contains C:F:L:T:M format
        score_breakdown = dawn_score

        lines.append(f"### {sym} — {catalyst[:60]}    {direction} {status}")
        lines.append(f"- DAWN Score: {score_breakdown}")
        lines.append(f"- Entry: {entry} at 09:15 | Exit: {exit_p} ({exit_reason})")
        lines.append(f"- PnL: {pnl_str}% ({pnl_rupees} rupees) | Max fav: +{max_fav}% | Max adv: {max_adv}%")
        lines.append(f"- SL Journey: {sl_type}")
        lines.append(f"- Liquidity Warning: {'YES ⚠️' if liq_warn.lower() == 'true' else 'NO'}")
        lines.append("")

    # Summary
    win_rate = round(wins / total * 100, 1) if total else 0
    lines.append(
        f"**Summary: {total} signals | Win rate {win_rate}% | "
        f"Net PnL {total_pnl:+.2f} | Best: {best_sym} (+{best_pnl:.1f}%) | "
        f"Worst: {worst_sym} ({worst_pnl:+.1f}%)**"
    )

    return "\n".join(lines)


# ── Section 5: Top Gainers & Losers Deep Analysis ───────────────────

def _build_section_5_movers(data: PostMarketData, analysis: PostMarketAnalysis) -> str:
    """Section 5: Top movers with why-analysis and detection audit."""
    analyses = analysis.movers_analysis.get("analyses", [])

    if not analyses and not data.nse_movers:
        return "## 5. Top Gainers & Losers Deep Analysis\n\nNo mover data available today."

    lines = ["## 5. Top Gainers & Losers Deep Analysis\n"]

    if analyses:
        for a in analyses:
            sym = a.get("symbol", "?")
            pct = a.get("pct_change", 0)
            reason = a.get("reason", "Unknown")
            caught = a.get("system_caught", False)
            caught_by = a.get("caught_by", "Not detected")
            in_dawn = a.get("in_dawn", False)
            what_missed = a.get("what_was_missed", "N/A")

            lines.append(f"### {sym} {pct:+.2f}%")
            lines.append(f"**Why:** {reason}")
            lines.append(f"**System:** {caught_by}")
            lines.append(f"**DAWN:** {'In DAWN signals' if in_dawn else 'Not in DAWN'}")
            lines.append(f"**Gap:** {what_missed}")
            lines.append("")
    else:
        # Machine fallback from NSE movers
        if data.nse_movers:
            signal_syms = {s.get("symbol") for s in data.conviction_signals}
            dawn_syms = {d.get("symbol") for d in data.dawn_rows}

            lines.append("| Symbol | Change% | Detected? | DAWN? |")
            lines.append("|--------|---------|-----------|-------|")
            for label, group in [("gainers", data.nse_movers.get("gainers", [])),
                                 ("losers", data.nse_movers.get("losers", []))]:
                for m in group[:5]:
                    sym = m.get("symbol", "?")
                    pct = m.get("pct_change", 0)
                    detected = "YES" if sym in signal_syms else "NO"
                    dawn = "YES" if sym in dawn_syms else "NO"
                    lines.append(f"| {sym} | {pct:+.2f}% | {detected} | {dawn} |")

    # Detection rate
    if data.nse_movers:
        all_mover_syms = set()
        for g in data.nse_movers.get("gainers", [])[:5]:
            all_mover_syms.add(g.get("symbol", ""))
        for l in data.nse_movers.get("losers", [])[:5]:
            all_mover_syms.add(l.get("symbol", ""))
        signal_syms = {s.get("symbol") for s in data.conviction_signals}
        detected = all_mover_syms & signal_syms
        lines.append(
            f"\n**Detection rate: {len(detected)}/{len(all_mover_syms)} top movers on watchboard**"
        )

    return "\n".join(lines)


# ── Section 6: Trades Executed ───────────────────────────────────────

def _build_section_6_trades(data: PostMarketData) -> str:
    """Section 6: All trades — live, conviction dry-run, DAWN dry-run."""
    stats = data.db_trades.get("stats", {})
    trades = data.db_trades.get("trades", [])

    lines = [
        "## 6. Trades Executed\n",
        f"- **Total Live Trades**: {stats.get('num_trades', 0)}",
        f"- **Win Rate**: {stats.get('win_rate_pct', 0):.1f}%",
        f"- **Day PnL**: {stats.get('day_pnl', 0):+.2f}\n",
    ]

    # Sub-table 1: Live trades
    if trades:
        lines.append("### Live Trades\n")
        lines.append("| Symbol | Dir | Qty | Entry | Exit | PnL | Strategy | Exit Reason |")
        lines.append("|--------|-----|-----|-------|------|-----|----------|-------------|")
        for t in trades:
            lines.append(
                f"| {t['symbol']} | {t['direction']} | {t['qty']} | "
                f"{t['entry_price']:.2f} ({t['entry_time']}) | "
                f"{t['exit_price']:.2f} ({t['exit_time']}) | "
                f"{t['pnl']:+.2f} | {t['strategy']} | {t.get('exit_reason', '?')} |"
            )
    else:
        try:
            from src.config.risk import load_risk_config as _lrc
            _threshold = _lrc().dry_run_conviction_threshold
        except Exception:
            _threshold = 55.0
        lines.append(
            f"No live trades executed today. Conviction threshold ({_threshold:.0f}) was not reached "
            "or risk gates blocked entry."
        )

    # Sub-table 2: Conviction dry-run (triggered signals)
    triggered = [s for s in data.conviction_signals if s.get("status") == "TRIGGERED" and s.get("is_dry_run")]
    if triggered:
        lines.append("\n### Conviction Dry-Run (Triggered)\n")
        lines.append("| Symbol | Dir | Strategy | Peak Conv | Signal Type |")
        lines.append("|--------|-----|----------|-----------|-------------|")
        for s in triggered:
            history = s.get("conviction_history", [])
            peak = max((h[1] for h in history), default=s.get("last_conviction", 0))
            lines.append(
                f"| {s.get('symbol', '?')} | {s.get('direction', '?')} | "
                f"{s.get('strategy', '?')} | {peak:.0f} | {s.get('signal_type', '?')} |"
            )

    # Sub-table 3: DAWN dry-run summary
    if data.dawn_rows:
        dawn_total = len(data.dawn_rows)
        dawn_wins = sum(1 for r in data.dawn_rows if float(r.get("pnl_pct", "0").replace("+", "")) > 0)
        dawn_pnl = sum(float(r.get("pnl_rupees", "0").replace("+", "")) for r in data.dawn_rows)
        lines.append(
            f"\n### DAWN Dry-Run\n\n"
            f"DAWN: {dawn_total} signals, {dawn_wins} wins, net PnL {dawn_pnl:+.2f} rupees (dry-run)"
        )

    return "\n".join(lines)


# ── Section 7: Key Learnings ─────────────────────────────────────────

def _generate_learnings(data: PostMarketData, scored_predictions: List[dict], today: date) -> List[str]:
    """Generate 5 tagged learning bullet points from today's data."""
    learnings = []

    # Dynamic conviction threshold from config (FIX-4a)
    try:
        from src.config.risk import load_risk_config
        threshold = load_risk_config().dry_run_conviction_threshold
    except Exception:
        threshold = 55.0

    # FIX-4b: Check if DAWN actually ran today by inspecting the CSV file
    dawn_csv_path = os.path.join("logs", "dawn_dryrun", f"{today}.csv")
    dawn_ran = os.path.exists(dawn_csv_path) and os.path.getsize(dawn_csv_path) > 100

    # [LEARN-DAWN]
    if data.dawn_rows:
        dawn_total = len(data.dawn_rows)
        dawn_wins = sum(1 for r in data.dawn_rows if float(r.get("pnl_pct", "0").replace("+", "")) > 0)
        liq_warns = sum(1 for r in data.dawn_rows if r.get("liquidity_warning", "").lower() == "true")
        best_row = max(data.dawn_rows, key=lambda r: float(r.get("pnl_pct", "0").replace("+", "")), default=None)
        worst_row = min(data.dawn_rows, key=lambda r: float(r.get("pnl_pct", "0").replace("+", "")), default=None)
        best_str = f"{best_row['symbol']} ({best_row.get('pnl_pct', '0')}%)" if best_row else "N/A"
        worst_str = f"{worst_row['symbol']} ({worst_row.get('pnl_pct', '0')}%)" if worst_row else "N/A"
        liq_str = f", {liq_warns} liquidity warnings" if liq_warns else ""
        learnings.append(
            f"[LEARN-DAWN] {dawn_total} signals, {dawn_wins}W. Best: {best_str}, Worst: {worst_str}{liq_str}"
        )
    elif dawn_ran:
        learnings.append("[LEARN-DAWN] DAWN ran but produced no tradable signals today")
    else:
        learnings.append("[LEARN-DAWN] DAWN did not run today — pre-market scanner may be offline")

    # [LEARN-CONV]
    signals = data.conviction_signals
    if signals:
        total = len(signals)
        triggered = sum(1 for s in signals if s.get("status") == "TRIGGERED")
        max_peak = 0
        max_sym = "none"
        for s in signals:
            history = s.get("conviction_history", [])
            peak = max((h[1] for h in history), default=s.get("last_conviction", 0))
            if peak > max_peak:
                max_peak = peak
                max_sym = s.get("symbol", "?")
        would_meet = sum(
            1 for s in signals
            if max((h[1] for h in s.get("conviction_history", [])),
                   default=s.get("last_conviction", 0)) >= threshold
        )
        learnings.append(
            f"[LEARN-CONV] {total} signals, {triggered} triggered. "
            f"Highest: {max_sym} at {max_peak:.0f}. "
            f"Threshold={threshold:.0f} would activate {would_meet}."
        )
    else:
        learnings.append("[LEARN-CONV] No conviction signals generated today")

    # [LEARN-BRIEF]
    if scored_predictions:
        scored = [p for p in scored_predictions if p.get("score") is not None]
        correct = sum(1 for p in scored if p.get("score") == 1)
        total = len(scored)
        missed = [p for p in scored if p.get("score") == -1]
        missed_str = ", ".join(f"{p['symbol']}" for p in missed[:3]) if missed else "none"
        learnings.append(
            f"[LEARN-BRIEF] Prediction accuracy: {correct}/{total}. "
            f"Notable misses: {missed_str}."
        )
    else:
        learnings.append("[LEARN-BRIEF] No predictions scored today — brief may have failed")

    # [LEARN-REGIME]
    learnings.append(
        f"[LEARN-REGIME] Market phase: {data.phase_str}. "
        f"{'Choppy regime — consider tighter stops.' if data.phase_str.lower() in ('choppy', 'unknown') else f'{data.phase_str} regime held through session.'}"
    )

    # [LEARN-DETECT]
    if data.nse_movers:
        all_mover_syms = set()
        for g in data.nse_movers.get("gainers", [])[:5]:
            all_mover_syms.add(g.get("symbol", ""))
        for l in data.nse_movers.get("losers", [])[:5]:
            all_mover_syms.add(l.get("symbol", ""))
        signal_syms = {s.get("symbol") for s in data.conviction_signals}
        dawn_syms = {d.get("symbol") for d in data.dawn_rows}
        detected = all_mover_syms & (signal_syms | dawn_syms)
        missed = all_mover_syms - detected
        missed_str = ", ".join(list(missed)[:3]) if missed else "all detected"
        learnings.append(
            f"[LEARN-DETECT] Top mover detection: {len(detected)}/{len(all_mover_syms)}. "
            f"Missed: {missed_str}."
        )
    else:
        learnings.append("[LEARN-DETECT] NSE mover data unavailable — detection audit skipped")

    return learnings


def _build_section_7_learnings(learnings: List[str]) -> str:
    """Section 7: Key Learnings — always 5 tagged bullets."""
    lines = ["## 7. Key Learnings\n"]
    for l in learnings:
        lines.append(f"- {l}")
    return "\n".join(lines)


# ── Section 8: Tomorrow's Setup ──────────────────────────────────────

def _build_section_8_tomorrow(analysis: PostMarketAnalysis) -> str:
    """Section 8: Tomorrow's setup from LLM analysis."""
    setup = analysis.tomorrow_setup

    lines = ["## 8. Tomorrow's Setup\n"]
    lines.append(f"**Regime:** {setup.get('regime_expectation', 'Analysis unavailable')}")

    sectors = setup.get("sectors_to_watch", [])
    if sectors:
        lines.append(f"**Sectors to watch:** {', '.join(sectors)}")

    events = setup.get("events_tomorrow", "")
    if events:
        lines.append(f"**Events:** {events}")

    readiness = setup.get("system_readiness", "")
    if readiness:
        lines.append(f"**System readiness:** {readiness}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# STAGE 4: REPORT ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════

def _assemble_report(today: date, sections: List[str]) -> str:
    """Combine all sections into final markdown."""
    header = f"# VoltEdge Post-Market Report — {today}\n"
    return header + "\n\n---\n\n".join(sections)


# ═══════════════════════════════════════════════════════════════════════
# STAGE 5: PERSIST + EMAIL
# ═══════════════════════════════════════════════════════════════════════

def _persist_and_email(
    today: date,
    report_md: str,
    updated_predictions: List[dict],
    learnings: List[str],
) -> str:
    """Save report, update predictions, persist learnings, send email."""

    # 1. Save markdown report
    report_path = ""
    try:
        os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
        report_path = os.path.join("logs", "daily_reports", f"{today}_post_market.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"[VoltEdge] Post-Market Report saved: {report_path}")
    except Exception as e:
        logger.error(f"[PostMarket/S5] Failed to save report: {e}")

    # 2. Update prediction_log.json with scored predictions
    if updated_predictions:
        try:
            pred_path = "data/prediction_log.json"
            if os.path.exists(pred_path):
                with open(pred_path) as f:
                    log = json.load(f)
            else:
                log = {"predictions": [], "system_lessons": []}

            # Replace today's predictions with scored versions
            other_preds = [p for p in log.get("predictions", []) if p.get("date") != str(today)]
            log["predictions"] = other_preds + updated_predictions

            with open(pred_path, "w", encoding="utf-8") as f:
                json.dump(log, f, indent=2)
            logger.info(f"[PostMarket/S5] Updated {len(updated_predictions)} predictions with scores")
        except Exception as e:
            logger.warning(f"[PostMarket/S5] Prediction update failed: {e}")

    # 3. Write learnings to data/system_learnings.json
    if learnings:
        try:
            learn_path = "data/system_learnings.json"
            os.makedirs("data", exist_ok=True)
            if os.path.exists(learn_path):
                with open(learn_path) as f:
                    all_learnings = json.load(f)
            else:
                all_learnings = {}

            all_learnings[str(today)] = learnings

            # Keep last 30 days
            sorted_keys = sorted(all_learnings.keys(), reverse=True)[:30]
            trimmed = {k: all_learnings[k] for k in sorted_keys}

            with open(learn_path, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, indent=2)
            logger.info(f"[PostMarket/S5] Persisted {len(learnings)} learnings for {today}")
        except Exception as e:
            logger.warning(f"[PostMarket/S5] Learning persistence failed: {e}")

    # 4. Email
    try:
        from src.reports.email_sender import send_report_email
        email_ok = send_report_email(
            subject=f"VoltEdge Post-Market Report — {today}",
            body_md=report_md,
            attachment_path=report_path if report_path else None,
        )
        if email_ok:
            logger.info(f"[PostMarket/S5] Email sent successfully")
        else:
            logger.error(f"[PostMarket/S5] Email send FAILED")
    except Exception as e:
        logger.error(f"[PostMarket/S5] Email dispatch error: {e}")

    return report_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run_post_market_pipeline()
