"""
post_market_report.py (v2) — 16:00 Unified Post-Market Debrief
---------------------------------------------------------------
Complete daily audit integrating conviction engine data, phase
transitions, signal lifecycles, and trade execution context.

Sections:
  0. System Health (ALWAYS populated)
  1. Pre-Market Plan vs Reality
  2. Conviction Engine Audit
  3. Market Phase Timeline
  4. Trades Executed
  5. Market Context & Top Movers
  6. Tomorrow's Setup
"""

import os
import json
import logging
from datetime import datetime, date
from typing import Optional, Dict, List

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


# ── Data Extraction Helpers ──────────────────────────────────────────────────

def _read_file_safe(path: str, max_chars: int = 3000) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[:max_chars]
    except Exception:
        return ""


def _read_morning_brief(today: date) -> str:
    candidates = [
        os.path.join("logs", "daily_reports", f"{today}_morning_brief.md"),
        os.path.join("logs", "daily_reports", f"voltedge_{today}", f"{today}_morning_brief.md"),
    ]
    for path in candidates:
        content = _read_file_safe(path, 3000)
        if content:
            return content
    return ""


def _load_prediction_log_today(today: date) -> list:
    """Load today's predictions from prediction_log.json."""
    path = "data/prediction_log.json"
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            log = json.load(f)
        return [p for p in log.get("predictions", []) if p.get("date") == str(today)]
    except Exception:
        return []


def _fetch_db_trades(today: date) -> dict:
    """Fetch today's trades and stats from DB."""
    try:
        from src.db import SessionLocal, TradeRecord, init_db
        init_db()
        with SessionLocal() as session:
            trades = session.query(TradeRecord).filter(
                TradeRecord.exit_time >= datetime.combine(today, datetime.min.time()),
                TradeRecord.exit_time <= datetime.combine(today, datetime.max.time()),
            ).order_by(TradeRecord.exit_time).all()

        trade_list = []
        for t in trades:
            trade_list.append({
                "symbol": getattr(t, "symbol", "?"),
                "direction": getattr(t, "direction", "?"),
                "qty": getattr(t, "qty", 0),
                "entry_price": getattr(t, "entry_price", 0.0),
                "exit_price": getattr(t, "exit_price", 0.0),
                "pnl": round(getattr(t, "pnl", 0.0) or 0.0, 2),
                "entry_time": t.entry_time.strftime("%H:%M") if getattr(t, "entry_time", None) else "?",
                "exit_time": t.exit_time.strftime("%H:%M") if getattr(t, "exit_time", None) else "?",
                "strategy": getattr(t, "strategy", "?"),
                "exit_reason": getattr(t, "exit_reason", "?"),
            })

        day_pnl = round(sum(t.get("pnl", 0) for t in trade_list), 2)
        win_count = sum(1 for t in trade_list if t.get("pnl", 0) > 0)
        win_rate = round(win_count / len(trade_list) * 100, 1) if trade_list else 0.0

        return {
            "stats": {"num_trades": len(trade_list), "day_pnl": day_pnl, "win_rate_pct": win_rate},
            "trades": trade_list,
        }
    except Exception as e:
        logger.warning(f"DB fetch failed: {e}")
        return {"stats": {"num_trades": 0, "day_pnl": 0, "win_rate_pct": 0}, "trades": []}


def _read_runner_log_tail(n_lines: int = 200) -> str:
    for path in ["/tmp/voltedge_logs/runner.log", "logs/runner.log"]:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                return "".join(lines[-n_lines:])
            except Exception:
                continue
    return ""


def _extract_dragon_events(log_tail: str) -> str:
    import re
    patterns = [
        r"HYDRA", r"VIPER", r"CONFLUENCE", r"EXIT", r"SL_HIT", r"TP_HIT",
        r"TRAILING", r"COIL", r"BUY \d+x", r"SHORT \d+x", r"EXECUTE",
        r"Regime=", r"Grok", r"\[ConvEng\]", r"\[Phase\]",
    ]
    combined = "|".join(patterns)
    relevant = []
    for line in log_tail.splitlines():
        if re.search(combined, line, re.IGNORECASE):
            relevant.append(line.strip())
    return "\n".join(relevant[-100:]) if relevant else "(No system events found in log)"


# ── Section Builders (machine-generated, always populated) ───────────────────

def _build_section_0_health(
    kite_ok: bool,
    pre_market_ran: bool,
    viper_health: str,
    api_failures: list,
) -> str:
    """Section 0: System Health — ALWAYS populated."""
    from src.reports.email_sender import validate_email_config
    email_status = validate_email_config()

    lines = [
        "## 0. System Health\n",
        f"| Component | Status |",
        f"|-----------|--------|",
        f"| Email | {email_status.split('Email: ')[1] if 'Email: ' in email_status else email_status} |",
        f"| Kite Token | {'Valid' if kite_ok else 'EXPIRED / MISSING'} |",
        f"| Pre-Market Brief | {'Ran successfully' if pre_market_ran else 'FAILED or skipped'} |",
        f"| VIPER Scan Health | {viper_health or 'N/A'} |",
    ]
    if api_failures:
        lines.append(f"| API Failures | {'; '.join(api_failures[:5])} |")
    else:
        lines.append(f"| API Failures | None |")
    return "\n".join(lines)


def _build_section_1_predictions(predictions: list) -> str:
    """Section 1: Pre-Market Plan vs Reality."""
    if not predictions:
        return (
            "## 1. Pre-Market Plan vs Reality\n\n"
            "No morning predictions were saved today. "
            "Either the pre-market brief failed to run or the Gemini response "
            "did not contain parseable predictions."
        )
    lines = [
        "## 1. Pre-Market Plan vs Reality\n",
        "| Symbol | Predicted Dir | Key Level | Actual % | Score |",
        "|--------|---------------|-----------|----------|-------|",
    ]
    for p in predictions:
        direction = (p.get("predicted_direction") or p.get("direction", "?")).upper()
        key_level = p.get("key_level", "?")
        actual = p.get("actual_change_pct")
        score = p.get("score")
        actual_str = f"{actual:+.2f}%" if actual is not None else "N/A"
        if score == 1:
            score_str = "HIT"
        elif score == -1:
            score_str = "MISS"
        elif score == 0:
            score_str = "FLAT"
        else:
            score_str = "Unscored"
        lines.append(f"| {p.get('symbol', '?')} | {direction} | {key_level} | {actual_str} | {score_str} |")
    return "\n".join(lines)


def _build_section_2_conviction(conviction_data: dict) -> str:
    """Section 2: Conviction Engine Audit — signal lifecycles."""
    signals = conviction_data.get("signals", [])
    phase_data = conviction_data.get("phase", "UNKNOWN")
    if not signals:
        return (
            "## 2. Conviction Engine Audit\n\n"
            f"No signals entered the watchboard today. Current phase: {phase_data}.\n"
            "This means either no events were detected by HYDRA, no movers were found "
            "by VIPER, or the system was offline during market hours."
        )
    lines = [
        "## 2. Conviction Engine Audit\n",
        f"Final market phase: **{phase_data}**\n",
        "| Symbol | Dir | Strategy | Entry Conv | Peak Conv | Final Conv | Status | Cycles |",
        "|--------|-----|----------|-----------|-----------|------------|--------|--------|",
    ]
    for s in signals:
        history = s.get("history", [])
        convictions = [h[1] for h in history] if history else [s.get("last_conviction", 0)]
        entry_conv = convictions[0] if convictions else 0
        peak_conv = max(convictions) if convictions else 0
        final_conv = convictions[-1] if convictions else 0
        lines.append(
            f"| {s['symbol']} | {s['direction']} | {s['strategy']} | "
            f"{entry_conv:.0f} | {peak_conv:.0f} | {final_conv:.0f} | "
            f"{s['status']} | {len(history)} |"
        )

    # Show conviction evolution for signals that reached >50
    high_conv = [s for s in signals if any(h[1] > 50 for h in s.get("history", []))]
    if high_conv:
        lines.append("\n**Conviction Evolution (signals > 50):**\n")
        for s in high_conv:
            history = s.get("history", [])
            timeline = " → ".join(f"{h[0]}:{h[1]:.0f}({h[2]})" for h in history[-8:])
            lines.append(f"- **{s['symbol']}** ({s['direction']}): {timeline}")

    return "\n".join(lines)


def _build_section_3_phases(phase_transitions: list) -> str:
    """Section 3: Market Phase Timeline."""
    if not phase_transitions:
        return (
            "## 3. Market Phase Timeline\n\n"
            "No phase transitions recorded. System may have been offline "
            "or market was in a single phase all day."
        )
    lines = [
        "## 3. Market Phase Timeline\n",
    ]
    for t in phase_transitions:
        lines.append(f"- {t}")
    return "\n".join(lines)


def _build_section_4_trades(db_ctx: dict) -> str:
    """Section 4: Trades Executed."""
    stats = db_ctx.get("stats", {})
    trades = db_ctx.get("trades", [])

    lines = [
        "## 4. Trades Executed\n",
        f"- **Total Trades**: {stats.get('num_trades', 0)}",
        f"- **Win Rate**: {stats.get('win_rate_pct', 0):.1f}%",
        f"- **Day PnL**: {stats.get('day_pnl', 0):+.2f}\n",
    ]

    if not trades:
        lines.append(
            "No trades were executed today. Possible reasons:\n"
            "- Conviction threshold (70) was never reached\n"
            "- Market phase did not align with signal direction\n"
            "- Risk gates (slot manager, daily loss cap, time gate) blocked entry\n"
            "- No signals were generated by HYDRA or VIPER"
        )
    else:
        lines.append("| Symbol | Dir | Qty | Entry | Exit | PnL | Strategy | Exit Reason |")
        lines.append("|--------|-----|-----|-------|------|-----|----------|-------------|")
        for t in trades:
            lines.append(
                f"| {t['symbol']} | {t['direction']} | {t['qty']} | "
                f"{t['entry_price']:.2f} ({t['entry_time']}) | "
                f"{t['exit_price']:.2f} ({t['exit_time']}) | "
                f"{t['pnl']:+.2f} | {t['strategy']} | {t.get('exit_reason', '?')} |"
            )
    return "\n".join(lines)


def _build_movers_context_nse_fallback() -> str:
    """Fetch top movers from NSE pre-open data (no Kite token required)."""
    try:
        from nsepython import nse_preopen_movers
        data = nse_preopen_movers("NIFTY")
        if not data:
            return "(NSE pre-open movers unavailable)"
        lines = []
        # data is typically a list or dict with gainers/losers
        if isinstance(data, list) and len(data) > 0:
            lines.append("NSE Pre-Open Movers:")
            for item in data[:10]:
                if isinstance(item, dict):
                    sym = item.get("symbol", "?")
                    chg = item.get("pChange", item.get("change", "?"))
                    lines.append(f"- {sym}: {chg}%")
        return "\n".join(lines) if lines else "(NSE movers parse failed)"
    except Exception as e:
        return f"(NSE movers fallback failed: {e})"


def _load_persisted_signals(today: date) -> list:
    """Load today's conviction signals from the daily JSON snapshot."""
    path = os.path.join("logs", "conviction_signals", f"{today}_signals.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load persisted signals: {e}")
        return []


def _build_section_7_deep_analysis(mover_analysis: list) -> str:
    """Section 7: Deep Root Cause Analysis for top movers (Grok-powered)."""
    if not mover_analysis:
        return (
            "## 7. Deep Root Cause Analysis\n\n"
            "> Grok deep analysis not available — check Grok budget or API connectivity.\n"
        )
    lines = [
        "## 7. Deep Root Cause Analysis\n",
        "| Symbol | Move% | Root Cause | TA at Open | Volume | Detectable? | Gap to Close |",
        "|--------|-------|------------|------------|--------|-------------|--------------|",
    ]
    for m in mover_analysis:
        sym = m.get("symbol", "?")
        move = m.get("move_pct", 0.0)
        lines.append(
            f"| {sym} | {move:+.2f}% | {m.get('root_cause','?')[:60]} | "
            f"{m.get('ta_at_open','?')[:50]} | {m.get('volume_analysis','?')[:40]} | "
            f"{m.get('detectable_by_system','?')[:40]} | {m.get('gap_to_close','?')[:50]} |"
        )
    lines.append("")
    for m in mover_analysis:
        sym = m.get("symbol", "?")
        lines.append(f"**{sym}** — Timing: {m.get('timing_map', 'N/A')}")
        lines.append(f"  Pre-market signals: {m.get('pre_market_signals', 'N/A')}")
        lines.append(f"  Could have predicted: _{m.get('what_we_could_have_predicted', 'N/A')}_")
        lines.append("")
    return "\n".join(lines)


def _build_section_8_conviction_postmortem(postmortem: list) -> str:
    """Section 8: Conviction Post-Mortem — signal lifecycle analysis."""
    if not postmortem:
        return (
            "## 8. Conviction Post-Mortem\n\n"
            "> No post-mortem data available.\n"
        )
    lines = [
        "## 8. Conviction Post-Mortem\n",
        "| Symbol | Dir | Detected | Type | What Happened | Window (min) | Dry-Run? | Verdict | Weight Adj |",
        "|--------|-----|----------|------|---------------|-------------|----------|---------|------------|",
    ]
    for p in postmortem:
        lines.append(
            f"| {p.get('symbol','?')} | {p.get('direction','?')} | "
            f"{str(p.get('discovery_time','?'))[:16]} | {p.get('signal_type','?')} | "
            f"{p.get('what_happened_after','?')[:50]} | {p.get('time_window_minutes','?')} | "
            f"{'YES' if p.get('dry_run_triggered') else 'NO'} | "
            f"{p.get('verdict','?')} | {p.get('weight_adjustment','?')[:50]} |"
        )
    return "\n".join(lines)


def _build_section_9_key_learnings(learnings: list, today: date) -> str:
    """Section 9: Key Learnings — system-writable carry-forward bullets."""
    if not learnings:
        return (
            "## 9. Key Learnings\n\n"
            "> No learnings generated today.\n"
        )
    lines = ["## 9. Key Learnings\n"]
    for item in learnings:
        lines.append(f"- {item}")
    # Persist to prediction_log system_lessons
    try:
        log_path = "data/prediction_log.json"
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as f:
                log_data = json.load(f)
        else:
            log_data = {"predictions": [], "system_lessons": []}
        existing_lessons = log_data.get("system_lessons", [])
        dated_lessons = [f"[{today}] {l}" for l in learnings]
        log_data["system_lessons"] = (existing_lessons + dated_lessons)[-20:]
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, indent=2)
        logger.info(f"[PostMarket] Persisted {len(dated_lessons)} learnings to prediction_log.json")
    except Exception as e:
        logger.warning(f"[PostMarket] Failed to persist learnings: {e}")
    return "\n".join(lines)


def _build_movers_context(kite_client, today: date) -> str:
    """Fetch top movers — Kite primary, NSE fallback."""
    try:
        from src.sniper.momentum_scanner import fetch_top_movers
        movers = fetch_top_movers(kite_client=kite_client)
        gainers = movers.get("gainers", [])[:5]
        losers = movers.get("losers", [])[:5]

        if not gainers and not losers:
            logger.warning("Kite movers returned empty — trying NSE fallback")
            return _build_movers_context_nse_fallback()

        lines = []
        for label, group in [("TOP GAINERS", gainers), ("TOP LOSERS", losers)]:
            if group:
                lines.append(f"### {label}")
                for c in group:
                    lines.append(f"- {c.symbol}: {c.pct_change:+.2f}% | Vol: {c.volume}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Kite movers failed ({e}) — trying NSE fallback")
        return _build_movers_context_nse_fallback()


# ── Main Orchestrator ────────────────────────────────────────────────────────

def generate_post_market_report(
    kite_client=None,
    target_date=None,
    traded_symbols: set = None,
    conviction_engine=None,
    viper_health: str = "",
    pre_market_ran: bool = True,
):
    """
    Generate the unified post-market debrief.

    Args:
        kite_client: Kite Connect client for market data
        target_date: Date for the report (defaults to today)
        traded_symbols: Set of symbols traded today
        conviction_engine: ConvictionEngine instance with today's signal history
        viper_health: VIPER scan health summary string
        pre_market_ran: Whether the pre-market brief ran successfully
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = target_date or datetime.now(IST).date()

    logger.info(f"Generating Post-Market Report v2 for {today}")

    # ── Determine system health ──────────────────────────────────────────
    kite_ok = False
    api_failures = []
    if kite_client:
        try:
            kite_client.ltp("NSE:NIFTY 50")
            kite_ok = True
        except Exception as e:
            api_failures.append(f"Kite LTP: {e}")
    else:
        api_failures.append("Kite client not available")

    # ── Section 0: System Health ─────────────────────────────────────────
    section_0 = _build_section_0_health(
        kite_ok=kite_ok,
        pre_market_ran=pre_market_ran,
        viper_health=viper_health,
        api_failures=api_failures,
    )

    # ── Section 1: Predictions ───────────────────────────────────────────
    predictions = _load_prediction_log_today(today)
    section_1 = _build_section_1_predictions(predictions)

    # ── Section 2: Conviction Engine ─────────────────────────────────────
    conviction_data = {"signals": [], "phase": "UNKNOWN"}
    if conviction_engine:
        try:
            all_signals = []
            for key, sig in conviction_engine._watchboard.items():
                all_signals.append({
                    "symbol": sig.symbol,
                    "direction": sig.direction,
                    "strategy": sig.strategy,
                    "status": sig.status,
                    "last_conviction": sig.last_conviction,
                    "layer_c_score": sig.layer_c_score,
                    "history": list(sig.conviction_history),
                    "event_summary": sig.event_summary,
                    "created_at": sig.created_at.strftime("%H:%M") if sig.created_at else "?",
                })
            conviction_data = {
                "signals": all_signals,
                "phase": conviction_engine.phase.value if conviction_engine.phase else "UNKNOWN",
            }
        except Exception as e:
            logger.warning(f"Conviction engine data extraction failed: {e}")
    section_2 = _build_section_2_conviction(conviction_data)

    # ── Section 3: Phase Timeline ────────────────────────────────────────
    phase_transitions = []
    if conviction_engine:
        try:
            phase_transitions = list(conviction_engine.phase_state.transitions)
        except Exception:
            pass
    section_3 = _build_section_3_phases(phase_transitions)

    # ── Section 4: Trades ────────────────────────────────────────────────
    db_ctx = _fetch_db_trades(today)
    section_4 = _build_section_4_trades(db_ctx)

    # ── Section 5: Market Context ────────────────────────────────────────
    movers_ctx = _build_movers_context(kite_client, today)

    # ── Section 6: Dragon Events (from log) ──────────────────────────────
    runner_log = _read_runner_log_tail(200)
    dragon_events = _extract_dragon_events(runner_log)

    # ── Sections 7-9: Grok Deep Analysis ─────────────────────────────────
    persisted_signals = _load_persisted_signals(today)
    grok_analysis = None
    grok_call_count = 0
    try:
        from src.llm.grok_client import grok_deep_analysis, GROK_DAILY_BUDGET
        # Parse mover lists from movers_ctx for the Grok prompt
        raw_gainers = []
        raw_losers = []
        try:
            import re as _re
            for line in movers_ctx.splitlines():
                m = _re.match(r"- (\w+): ([+-]?\d+\.\d+)%.*Vol: (\d+)", line)
                if m:
                    entry = {"symbol": m.group(1), "pct_change": float(m.group(2)), "volume": int(m.group(3))}
                    if float(m.group(2)) > 0:
                        raw_gainers.append(entry)
                    else:
                        raw_losers.append(entry)
        except Exception:
            pass
        mkt_summary = f"Phase: choppy | Nifty: N/A | VIX: N/A"
        if conviction_engine:
            try:
                mkt_summary = f"Phase: {conviction_engine.phase.value}"
            except Exception:
                pass
        grok_analysis = grok_deep_analysis(
            top_gainers=raw_gainers[:5],
            top_losers=raw_losers[:5],
            watchboard_signals=persisted_signals,
            market_summary=mkt_summary,
            current_call_count=grok_call_count,
        )
    except Exception as grok_e:
        logger.error(f"[PostMarket] Grok deep analysis failed: {grok_e}")

    section_7 = _build_section_7_deep_analysis(
        grok_analysis.get("mover_analysis", []) if grok_analysis else []
    )
    section_8 = _build_section_8_conviction_postmortem(
        grok_analysis.get("conviction_postmortem", []) if grok_analysis else []
    )
    section_9 = _build_section_9_key_learnings(
        grok_analysis.get("key_learnings", []) if grok_analysis else [], today
    )

    # ── Assemble machine-generated sections ──────────────────────────────
    machine_report = f"""# VoltEdge Post-Market Report — {today}

{section_0}

{section_1}

{section_2}

{section_3}

{section_4}

## 5. Market Context

### Top Movers
{movers_ctx}

## 6. Intraday System Events
```
{dragon_events}
```

{section_7}

{section_8}

{section_9}
"""

    # ── Generate narrative via Gemini ─────────────────────────────────────
    api_key = os.getenv("GEMINI_API_KEY")
    morning_brief = _read_morning_brief(today)
    traded_str = ", ".join(traded_symbols) if traded_symbols else "None"

    report_md = machine_report  # Fallback: machine sections only

    if api_key:
        try:
            from google import genai
            from google.genai import types

            prompt = f"""You are VoltEdge's senior post-market analyst.
Today's date: {today}. Synthesize the structured data below into a narrative summary.

## Machine-Generated Sections (factual, DO NOT contradict):
{machine_report}

## Morning Brief Context:
{morning_brief[:1500] if morning_brief else "(No morning brief ran today)"}

## Traded Symbols: {traded_str}

Your task: Add these narrative sections AFTER the machine-generated content:

### Key Insights (2-3 bullets max)
The most important takeaways from today's session. Be specific and factual.

### Honest Gap Analysis
What moved in the market today that VoltEdge missed? Were any of the top movers
detectable by HYDRA or VIPER? What signals would have caught them?

### Tomorrow's Setup
One sentence on the system's stance for tomorrow based on today's data.

Be concise. Do NOT repeat the machine sections. Do NOT hallucinate data.
"""
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction="You are VoltEdge's post-market analyst. Be factual and concise.",
                    temperature=0.3,
                ),
            )
            narrative = response.text
            report_md = machine_report + "\n---\n\n" + narrative

        except Exception as e:
            logger.error(f"Gemini narrative generation failed: {e}")
            report_md = machine_report + (
                f"\n\n---\n\n> **Note:** Gemini narrative generation failed: `{e}`. "
                f"Machine-generated sections above contain all factual data."
            )
    else:
        report_md += "\n\n---\n\n> GEMINI_API_KEY not set — narrative sections skipped."

    # ── Save report ──────────────────────────────────────────────────────
    os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
    report_path = os.path.join("logs", "daily_reports", f"{today}_post_market.md")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"[VoltEdge] Saved Post-Market Report to: {report_path}")
    except Exception as e:
        logger.error(f"Failed to save report: {e}")

    # ── Email ────────────────────────────────────────────────────────────
    from src.reports.email_sender import send_report_email
    send_report_email(
        subject=f"VoltEdge Post-Market Report — {today}",
        body_md=report_md,
        attachment_path=report_path,
    )

    return report_md
