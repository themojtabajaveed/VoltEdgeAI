"""
mid_session_pulse.py — 12:22 IST Mid-Session Report v2
-------------------------------------------------------
READ-ONLY observational report. Does NOT affect trading decisions.

7 Sections:
  0. Header: Phase, Nifty, VIX, A:D — machine data
  1. Morning Narrative: LLM-generated market summary
  2. Market Scoreboard: Top gainers/losers with reasons (Brave Search)
  3. What System Did: Trades, PnL, positions, signals — machine data
  4. Morning Brief Accuracy Check: Predictions vs actual prices
  5. Signals Monitored: Active signals with age + move-since-detection
  6. Second Half Outlook: LLM-generated afternoon outlook

LLM: Groq primary → Gemini fallback → mechanical last resort.
Guarantee: Report ALWAYS generates, even with zero LLM availability.
"""
import os
import json
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


def generate_mid_session_pulse(
    conviction_engine=None,
    market_snapshot=None,
    positions=None,
    slot_manager=None,
    risk_state=None,
    risk_cfg=None,
    live_client=None,
) -> str:
    """
    Generate the mid-session pulse report v2.

    All args are live objects from runner.py. Returns the report markdown.
    Same function signature as v1 — no runner.py changes needed.
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)
    today = now.date()

    logger.info("[MidSession] Starting mid-session pulse v2 generation...")

    # ── Stage 1: Collect data in parallel ────────────────────────────────
    movers = {}
    mover_reasons: Dict[str, str] = {}
    predictions = _load_today_predictions(today)
    predictions_status = []

    try:
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="MidSess") as pool:
            futures = {}

            # Fetch NSE top gainers/losers
            futures["movers"] = pool.submit(_fetch_movers)

            # Fetch current prices for predictions
            if predictions and live_client:
                futures["pred_prices"] = pool.submit(
                    _check_predictions, predictions, live_client
                )

            # Wait for movers first, then kick off reason search
            for key, future in futures.items():
                try:
                    if key == "movers":
                        movers = future.result(timeout=15)
                    elif key == "pred_prices":
                        predictions_status = future.result(timeout=15)
                except Exception as e:
                    logger.warning(f"[MidSession] {key} fetch failed: {e}")

        # Fetch mover reasons via Brave (after movers are available), then
        # apply strict symbol-match filter to prevent market-wide headline bleed.
        if movers:
            try:
                from src.llm.brief_analyzer import fetch_mover_reasons
                raw_reasons = fetch_mover_reasons(
                    movers.get("gainers", []),
                    movers.get("losers", []),
                    max_per_side=3,
                )
                all_symbols = (
                    [g.get("symbol", "") for g in movers.get("gainers", [])] +
                    [l.get("symbol", "") for l in movers.get("losers", [])]
                )
                mover_reasons = _filter_mover_reasons(raw_reasons, all_symbols)
            except Exception as e:
                logger.warning(f"[MidSession] Mover reasons failed: {e}")

    except Exception as e:
        logger.error(f"[MidSession] Data collection failed: {e}")

    # ── Stage 2: LLM analysis (2 parallel Groq calls) ───────────────────
    market_data = _build_market_data(market_snapshot, conviction_engine, movers)
    narrative_result = {}
    outlook_result = {}

    try:
        from src.llm.brief_analyzer import analyze_mid_session, analyze_second_half

        # Build signals summary for outlook
        signals_summary = _build_signals_summary(conviction_engine)
        risk_flags = _compute_risk_flags(market_snapshot, conviction_engine, risk_state, risk_cfg)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="MidLLM") as pool:
            f_narrative = pool.submit(
                analyze_mid_session, market_data, mover_reasons, predictions_status
            )
            f_outlook = pool.submit(
                analyze_second_half, market_data, signals_summary, risk_flags
            )

            try:
                narrative_result = f_narrative.result(timeout=30)
            except Exception as e:
                logger.warning(f"[MidSession] Narrative LLM failed: {e}")

            try:
                outlook_result = f_outlook.result(timeout=30)
            except Exception as e:
                logger.warning(f"[MidSession] Outlook LLM failed: {e}")

    except Exception as e:
        logger.error(f"[MidSession] LLM analysis stage failed: {e}")

    # ── Stage 3: Assemble report ─────────────────────────────────────────
    report_md = _assemble_report(
        now=now,
        today=today,
        market_snapshot=market_snapshot,
        conviction_engine=conviction_engine,
        positions=positions,
        slot_manager=slot_manager,
        risk_state=risk_state,
        risk_cfg=risk_cfg,
        live_client=live_client,
        movers=movers,
        mover_reasons=mover_reasons,
        predictions=predictions,
        predictions_status=predictions_status,
        narrative_result=narrative_result,
        outlook_result=outlook_result,
    )

    # ── Stage 4: Persist + Email ─────────────────────────────────────────
    os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
    report_path = os.path.join("logs", "daily_reports", f"{today}_mid_session.md")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"[VoltEdge] Saved Mid-Session Pulse v2 to: {report_path}")
    except Exception as e:
        logger.error(f"Failed to save mid-session report: {e}")

    try:
        from src.reports.email_sender import send_report_email
        send_report_email(
            subject=f"VoltEdge Mid-Session Pulse — {today} {now.strftime('%H:%M')}",
            body_md=report_md,
            attachment_path=report_path,
        )
    except Exception as e:
        logger.error(f"[MidSession] Email failed: {e}")

    logger.info("[MidSession] Mid-session pulse v2 complete")
    return report_md


# ── Assembly ────────────────────────────────────────────────────────────────


def _assemble_report(
    now, today, market_snapshot, conviction_engine, positions,
    slot_manager, risk_state, risk_cfg, live_client,
    movers, mover_reasons, predictions, predictions_status,
    narrative_result, outlook_result,
) -> str:
    """Deterministic template assembly. LLM content fills slots."""
    lines = []

    # ── Section 0: Header ────────────────────────────────────────────────
    lines.append(f"# VoltEdge Mid-Session Pulse — {today} {now.strftime('%H:%M')} IST\n")
    if market_snapshot and conviction_engine:
        phase = _compute_market_phase(market_snapshot.nifty_pct, market_snapshot.ad_ratio)
        lines.append(
            f"**Market Phase: {phase}** | "
            f"Nifty: {market_snapshot.nifty_pct:+.2f}% | "
            f"VIX: {market_snapshot.vix:.1f} | "
            f"A:D: {market_snapshot.ad_ratio:.2f}\n"
        )
    else:
        lines.append("**Market data unavailable** — report generated with limited data.\n")

    lines.append("---\n")

    # ── Section 1: Morning Narrative (LLM) ───────────────────────────────
    lines.append("## 1. Morning Narrative\n")
    narrative = narrative_result.get("narrative", "")
    if narrative:
        lines.append(f"{narrative}\n")
    else:
        if market_snapshot:
            direction = "up" if market_snapshot.nifty_pct > 0 else "down" if market_snapshot.nifty_pct < 0 else "flat"
            lines.append(
                f"Market opened and is currently {direction} {abs(market_snapshot.nifty_pct):.2f}%. "
                f"VIX at {market_snapshot.vix:.1f}. A/D ratio: {market_snapshot.ad_ratio:.2f}.\n"
            )
        else:
            lines.append("Market narrative unavailable — no market data.\n")

    lines.append("---\n")

    # ── Section 2: Market Scoreboard with Reasons ────────────────────────
    lines.append("## 2. Market Scoreboard\n")

    gainers = movers.get("gainers", []) if movers else []
    losers = movers.get("losers", []) if movers else []

    if gainers:
        lines.append("### Top Gainers\n")
        lines.append("| Symbol | Chg% | Price | Reason |")
        lines.append("|--------|------|-------|--------|")
        for g in gainers[:5]:
            sym = g.get("symbol", "?")
            reason = mover_reasons.get(sym, "—")
            lines.append(
                f"| {sym} | {g.get('pct_change', 0):+.1f}% | "
                f"{g.get('last_price', '—')} | {reason} |"
            )
        lines.append("")

    if losers:
        lines.append("### Top Losers\n")
        lines.append("| Symbol | Chg% | Price | Reason |")
        lines.append("|--------|------|-------|--------|")
        for l in losers[:5]:
            sym = l.get("symbol", "?")
            reason = mover_reasons.get(sym, "—")
            lines.append(
                f"| {sym} | {l.get('pct_change', 0):+.1f}% | "
                f"{l.get('last_price', '—')} | {reason} |"
            )
        lines.append("")

    mover_commentary = narrative_result.get("mover_commentary", "")
    if mover_commentary:
        lines.append(f"{mover_commentary}\n")

    if not gainers and not losers:
        lines.append("Market scoreboard unavailable — NSE data fetch failed.\n")

    lines.append("---\n")

    # ── Section 3: What the System Did ───────────────────────────────────
    lines.append("## 3. What the System Did This Morning\n")

    trades_taken = 0
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    slots_used = 0
    slots_max = 5
    watching_count = 0
    phase_transitions = []

    if risk_state:
        trades_taken = getattr(risk_state, "trades_taken", 0)
        realized_pnl = float(getattr(risk_state, "realized_pnl", 0))
        unrealized_pnl = float(getattr(risk_state, "unrealized_pnl", 0))

    if slot_manager:
        slots_used = getattr(slot_manager, "trades_today", 0)
        slots_max = getattr(slot_manager, "max_trades", 5)

    if conviction_engine:
        active = conviction_engine.get_active_signals()
        watching_count = len(active) if active else 0
        if hasattr(conviction_engine, "phase_state"):
            phase_transitions = conviction_engine.phase_state.transitions or []

    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Trades Executed | {trades_taken} |")
    lines.append(f"| Realized PnL | ₹{realized_pnl:+.2f} |")
    lines.append(f"| Unrealized PnL | ₹{unrealized_pnl:+.2f} |")
    lines.append(f"| Active Positions | {slots_used} / {slots_max} |")
    lines.append(f"| Signals Watching | {watching_count} |")
    lines.append(f"| Phase Transitions | {len(phase_transitions)} |")
    lines.append("")

    if phase_transitions:
        lines.append("**Phase Transitions:**")
        for t in phase_transitions:
            lines.append(f"- {t}")
        lines.append("")

    # Active positions table
    if positions and slot_manager:
        open_pos = []
        try:
            open_pos = positions.get_open_positions()
        except Exception:
            pass

        if open_pos:
            lines.append("### Active Positions\n")
            lines.append("| Symbol | Direction | Entry | Current | PnL% | Strategy |")
            lines.append("|--------|-----------|-------|---------|------|----------|")
            for pos in open_pos:
                current_price = pos.avg_price
                if live_client:
                    try:
                        tick = live_client.get_last_tick(pos.symbol)
                        if tick:
                            current_price = tick.ltp
                    except Exception:
                        pass
                if pos.avg_price > 0:
                    pnl_pct = ((current_price - pos.avg_price) / pos.avg_price * 100
                               if pos.side == "LONG"
                               else (pos.avg_price - current_price) / pos.avg_price * 100)
                else:
                    pnl_pct = 0.0
                strategy = getattr(pos, "strategy", "—")
                lines.append(
                    f"| {pos.symbol} | {pos.side} | {pos.avg_price:.2f} | "
                    f"{current_price:.2f} | {pnl_pct:+.1f}% | {strategy} |"
                )
            lines.append("")
        else:
            if trades_taken == 0:
                lines.append("No trades executed — system in observation mode.\n")
            else:
                lines.append("All positions closed.\n")
    elif trades_taken == 0:
        lines.append("No trades executed — system in observation mode.\n")

    lines.append("---\n")

    # ── Section 4: Morning Brief Accuracy Check ──────────────────────────
    lines.append("## 4. Morning Brief Accuracy Check\n")

    if predictions_status:
        correct = sum(1 for p in predictions_status if p.get("status") == "CORRECT")
        wrong = sum(1 for p in predictions_status if p.get("status") == "WRONG")
        flat = sum(1 for p in predictions_status if p.get("status") == "FLAT")
        no_data = sum(1 for p in predictions_status if p.get("status") == "NO_DATA")
        total_scored = correct + wrong + flat

        if total_scored > 0:
            accuracy = correct / total_scored * 100
            lines.append(f"**Score: {correct}/{total_scored} correct ({accuracy:.0f}%)**\n")

        lines.append("| Symbol | Predicted | Key Level | Current Price | Actual Chg% | Status |")
        lines.append("|--------|-----------|-----------|---------------|-------------|--------|")
        for p in predictions_status:
            status_emoji = {
                "CORRECT": "✅ Correct",
                "WRONG": "❌ Wrong",
                "FLAT": "➖ Flat",
                "NO_DATA": "⚪ No data",
            }.get(p.get("status", ""), p.get("status", "?"))
            actual_pct = f"{p['actual_pct']:+.1f}%" if p.get("actual_pct") is not None else "N/A"
            lines.append(
                f"| {p.get('symbol', '?')} | {p.get('predicted', '?')} | "
                f"{p.get('key_level', '—')} | {p.get('current_price', 'N/A')} | "
                f"{actual_pct} | {status_emoji} |"
            )
        lines.append("")

        pred_commentary = narrative_result.get("prediction_commentary", "")
        if pred_commentary:
            lines.append(f"{pred_commentary}\n")
    elif predictions:
        lines.append("Morning predictions loaded but live data unavailable for checking.\n")
    else:
        lines.append("No morning predictions to check.\n")

    lines.append("---\n")

    # ── Section 5: Signals Monitored ─────────────────────────────────────
    lines.append("## 5. Signals Monitored\n")

    if conviction_engine:
        active = conviction_engine.get_active_signals()
        if active:
            lines.append("| Symbol | Dir | Strategy | Age | Det. Price | Current | Move% | Conv | Status |")
            lines.append("|--------|-----|----------|-----|-----------|---------|-------|------|--------|")
            for sig in sorted(active, key=lambda s: s.last_conviction, reverse=True):
                # Age category
                age_str = "?"
                if getattr(sig, "created_at", None):
                    age_minutes = (now - sig.created_at).total_seconds() / 60
                    if age_minutes < 60:
                        age_str = f"Fresh ({int(age_minutes)}m)"
                    elif age_minutes < 180:
                        age_str = f"Maturing ({age_minutes/60:.1f}h)"
                    else:
                        age_str = f"Stale ({age_minutes/60:.1f}h)"

                # Detection price and move
                det_price = sig.metadata.get("detection_price")
                current_price = None
                move_pct_str = "—"
                det_price_str = "—"

                if det_price:
                    det_price_str = f"{det_price:.2f}"
                    # Try to get current price
                    current_price = _get_current_price(sig.symbol, live_client)
                    if current_price and det_price > 0:
                        move_pct = (current_price - det_price) / det_price * 100
                        move_pct_str = f"{move_pct:+.1f}%"

                current_str = f"{current_price:.2f}" if current_price else "—"
                dry_tag = " [DRY]" if getattr(sig, "is_dry_run", False) else ""
                win_status = getattr(sig, "window_status", "ACTIVE")

                lines.append(
                    f"| {sig.symbol}{dry_tag} | {sig.direction} | {sig.strategy} | "
                    f"{age_str} | {det_price_str} | {current_str} | {move_pct_str} | "
                    f"{sig.last_conviction:.0f} | {win_status} |"
                )

            lines.append(f"\n**Total**: {len(active)} signals watching, "
                        f"threshold = {conviction_engine._threshold:.0f}")
            lines.append("")

            # Triggered/expired counts
            all_signals = list(conviction_engine._watchboard.values())
            triggered = [s for s in all_signals if s.status == "TRIGGERED"]
            expired = [s for s in all_signals if s.status == "EXPIRED"]
            if triggered or expired:
                lines.append(f"Triggered today: {len(triggered)} | Expired: {len(expired)}\n")
        else:
            lines.append("Watchboard is empty — no signals being tracked.\n")
    else:
        lines.append("Conviction engine not available.\n")

    lines.append("---\n")

    # ── Section 6: Second Half Outlook (LLM) ─────────────────────────────
    lines.append("## 6. Second Half Outlook\n")

    outlook = outlook_result.get("outlook", "")
    if outlook:
        lines.append(f"{outlook}\n")
    else:
        if market_snapshot and conviction_engine:
            phase = _compute_market_phase(market_snapshot.nifty_pct, market_snapshot.ad_ratio)
            watching = len(conviction_engine.get_active_signals()) if conviction_engine.get_active_signals() else 0
            lines.append(
                f"Phase: {phase}. Nifty {market_snapshot.nifty_pct:+.2f}% at midday. "
                f"{watching} signals active. Continue monitoring.\n"
            )
        else:
            lines.append("Outlook unavailable — insufficient data.\n")

    risk_flags = outlook_result.get("risk_flags", [])
    if risk_flags and risk_flags != ["None"]:
        lines.append("**Risk Flags:**")
        for flag in risk_flags:
            lines.append(f"- {flag}")
        lines.append("")

    highlight = outlook_result.get("watchlist_highlight", "")
    if highlight:
        lines.append(f"**Watchlist Highlight:** {highlight}\n")

    return "\n".join(lines)


# ── Data Helpers ────────────────────────────────────────────────────────────


def _fetch_movers() -> dict:
    """Fetch NSE top gainers/losers."""
    try:
        from src.data_ingestion.nse_scraper import get_nse_top_gainers_losers
        return get_nse_top_gainers_losers(count=5)
    except Exception as e:
        logger.warning(f"[MidSession] NSE movers fetch failed: {e}")
        return {}


def _filter_mover_reasons(
    raw_reasons: Dict[str, str],
    all_symbols: List[str],
) -> Dict[str, str]:
    """
    Post-filter Brave-fetched mover reasons to eliminate market-wide headline bleed.

    Rules applied per symbol:
      1. Exact ticker match required — reason must contain the symbol as a
         whole word (case-insensitive). Partial-word matches (e.g. "OIL" in
         "BOIL") are rejected.
      2. Negative filter — reject if headline contains any market-wide signal
         phrase WITHOUT also containing the exact ticker symbol:
           "Nifty", "Sensex", "market up", "investors gain", "lakh crore",
           "stock market", "equity market", "share market", "benchmark"
      3. Truncate clean matches to 100 chars.
      4. Prepend confidence label:
           [News] — exact ticker found in headline
           [News (name match)] — already annotated by upstream caller
           [No catalyst identified] — no clean match
    """
    import re

    # Market-wide phrases that signal a generic headline (not stock-specific)
    _MARKET_WIDE = re.compile(
        r"\b(nifty|sensex|market up|investors gain|lakh crore|"
        r"stock market|equity market|share market|benchmark)\b",
        re.IGNORECASE,
    )

    filtered: Dict[str, str] = {}
    for sym in all_symbols:
        if not sym:
            continue
        raw = (raw_reasons.get(sym) or "").strip()
        if not raw or raw == "—":
            filtered[sym] = "[No catalyst identified]"
            continue

        sym_upper = sym.strip().upper()
        # Whole-word pattern for the ticker (handles 3-10 char symbols)
        sym_pattern = re.compile(r"\b" + re.escape(sym_upper) + r"\b", re.IGNORECASE)
        ticker_present = bool(sym_pattern.search(raw))

        # Check for market-wide contamination
        market_wide_hit = bool(_MARKET_WIDE.search(raw))

        if market_wide_hit and not ticker_present:
            # Market-wide headline with NO mention of this ticker → reject
            logger.warning(
                f"[MidSession] Rejected market-wide headline for {sym}: {raw[:60]!r}"
            )
            filtered[sym] = "[No catalyst identified]"
            continue

        if not ticker_present:
            # Headline doesn't mention the ticker at all → not a clean match
            logger.warning(
                f"[MidSession] No ticker match for {sym} in headline: {raw[:60]!r}"
            )
            filtered[sym] = "[No catalyst identified]"
            continue

        # Clean match — truncate and label
        snippet = raw[:100].rstrip()
        if len(raw) > 100:
            snippet += "…"
        filtered[sym] = f"[News] {snippet}"

    return filtered


def _compute_market_phase(nifty_pct: float, ad_ratio: float) -> str:
    """
    Derive market phase from index move magnitude + breadth (A/D ratio).

    Matrix:
      index ≥1.5% + broad  → TRENDING        (strong move, wide participation)
      index ≥1.5% + narrow → LARGE-CAP LED   (strong index, thin breadth)
      index ≥0.5% + broad  → STEADY          (moderate move, decent breadth)
      index ≥0.5% + narrow → SELECTIVE       (moderate move, few stocks moving)
      index <0.5% + broad  → SIDEWAYS-BROAD  (flat index but breadth present)
      flat + narrow        → CHOPPY           (genuinely undirected market)

    Fixes the regression where Nifty +1.59% / A/D 0.50 returned CHOPPY.
    """
    index_move = abs(nifty_pct)
    if index_move >= 1.5 and ad_ratio >= 0.55:
        return "TRENDING"
    elif index_move >= 1.5 and ad_ratio < 0.55:
        return "LARGE-CAP LED"
    elif index_move >= 0.5 and ad_ratio >= 0.55:
        return "STEADY"
    elif index_move >= 0.5 and ad_ratio < 0.55:
        return "SELECTIVE"
    elif index_move < 0.5 and ad_ratio >= 0.55:
        return "SIDEWAYS-BROAD"
    return "CHOPPY"


def _build_market_data(market_snapshot, conviction_engine, movers) -> dict:
    """Build market_data dict for LLM calls."""
    data = {
        "nifty_pct": 0.0,
        "vix": 0.0,
        "ad_ratio": 0.0,
        "phase": "UNKNOWN",
        "sector_changes": {},
        "top_gainers": [],
        "top_losers": [],
    }
    if market_snapshot:
        data["nifty_pct"] = market_snapshot.nifty_pct
        data["vix"] = market_snapshot.vix
        data["ad_ratio"] = market_snapshot.ad_ratio
        data["sector_changes"] = market_snapshot.sector_changes or {}
    if conviction_engine and market_snapshot:
        data["phase"] = _compute_market_phase(market_snapshot.nifty_pct, market_snapshot.ad_ratio)
    elif conviction_engine:
        data["phase"] = conviction_engine.phase.value.upper()
    if movers:
        data["top_gainers"] = movers.get("gainers", [])
        data["top_losers"] = movers.get("losers", [])
    return data


def _build_signals_summary(conviction_engine) -> dict:
    """Build signals summary for outlook LLM call."""
    summary = {"total": 0, "by_strategy": {}, "above_threshold": 0}
    if not conviction_engine:
        return summary

    active = conviction_engine.get_active_signals()
    if not active:
        return summary

    summary["total"] = len(active)
    for sig in active:
        strat = sig.strategy
        summary["by_strategy"][strat] = summary["by_strategy"].get(strat, 0) + 1
        if sig.last_conviction >= conviction_engine._threshold:
            summary["above_threshold"] += 1

    return summary


def _compute_risk_flags(market_snapshot, conviction_engine, risk_state, risk_cfg) -> List[str]:
    """Compute risk flags from machine data."""
    flags = []
    if market_snapshot:
        if market_snapshot.vix > 22:
            flags.append(f"HIGH VIX: {market_snapshot.vix:.1f} — elevated volatility regime")
        if market_snapshot.ad_ratio < 0.3:
            flags.append(f"WEAK BREADTH: A/D={market_snapshot.ad_ratio:.2f} — broad selling")
        elif market_snapshot.ad_ratio > 0.7:
            flags.append(f"STRONG BREADTH: A/D={market_snapshot.ad_ratio:.2f} — broad buying")

    if conviction_engine and hasattr(conviction_engine, "phase_state"):
        transitions = conviction_engine.phase_state.transitions
        if transitions and len(transitions) >= 3:
            flags.append(f"UNSTABLE MARKET: {len(transitions)} phase transitions today")

    if risk_state and risk_cfg:
        try:
            realized = float(getattr(risk_state, "realized_pnl", 0))
            loss_cap = getattr(risk_cfg, "daily_loss_cap", None)
            if loss_cap and realized < 0 and abs(realized) > abs(float(loss_cap)) * 0.7:
                flags.append(f"APPROACHING LOSS CAP: ₹{realized:+.0f} (cap: ₹{loss_cap})")
        except Exception:
            pass

    return flags


def _load_today_predictions(today: date) -> list:
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


def _check_predictions(predictions: list, live_client) -> List[dict]:
    """Check morning predictions against current prices."""
    results = []
    for p in predictions:
        sym = p.get("symbol", "?")
        predicted_dir = (p.get("predicted_direction") or p.get("direction", "?")).upper()
        key_level = p.get("key_level", "—")

        current_pct = _get_current_pct(sym, live_client)
        current_price = _get_current_price_value(sym, live_client)

        if current_pct is not None:
            if predicted_dir == "BULLISH" and current_pct > 0.5:
                status = "CORRECT"
            elif predicted_dir == "BEARISH" and current_pct < -0.5:
                status = "CORRECT"
            elif abs(current_pct) <= 0.5:
                status = "FLAT"
            else:
                status = "WRONG"
        else:
            status = "NO_DATA"

        results.append({
            "symbol": sym,
            "predicted": predicted_dir,
            "key_level": key_level,
            "current_price": f"{current_price:.2f}" if current_price else "N/A",
            "actual_pct": current_pct,
            "status": status,
        })

    return results


def _get_current_pct(symbol: str, live_client) -> Optional[float]:
    """Get current day % change for a symbol."""
    try:
        kite = getattr(live_client, '_kite', None)
        if kite:
            ohlc = kite.ohlc(f"NSE:{symbol}")
            if f"NSE:{symbol}" in ohlc:
                d = ohlc[f"NSE:{symbol}"]
                ltp = d.get("last_price", 0)
                prev_c = d.get("ohlc", {}).get("close", 0)
                if prev_c > 0 and ltp > 0:
                    try:
                        from cache.data_cache import CacheManager
                        CacheManager().write_ltp(symbol, ltp)
                    except Exception:
                        pass
                    return (ltp - prev_c) / prev_c * 100
    except Exception as e:
        try:
            from kiteconnect import exceptions as _ke
            if isinstance(e, _ke.TokenException):
                from cache.data_cache import handle_token_expiry
                handle_token_expiry(f"mid_session._get_current_pct for {symbol}")
        except Exception:
            pass
    return None


def _get_current_price_value(symbol: str, live_client) -> Optional[float]:
    """Get the current LTP for a symbol."""
    try:
        kite = getattr(live_client, '_kite', None)
        if kite:
            ohlc = kite.ohlc(f"NSE:{symbol}")
            if f"NSE:{symbol}" in ohlc:
                return ohlc[f"NSE:{symbol}"].get("last_price")
    except Exception:
        pass

    # Fallback to cache
    try:
        from cache.data_cache import CacheManager
        ltp = CacheManager().read_ltp(symbol, max_age_seconds=300)
        if ltp:
            return float(ltp)
    except Exception:
        pass

    return None


def _get_current_price(symbol: str, live_client) -> Optional[float]:
    """Get current price for a signal — tries live client, then cache."""
    # Try live client
    try:
        if live_client:
            tick = live_client.get_last_tick(symbol)
            if tick and hasattr(tick, "ltp") and tick.ltp > 0:
                return tick.ltp
    except Exception:
        pass

    # Try kite OHLC
    try:
        kite = getattr(live_client, '_kite', None) if live_client else None
        if kite:
            ohlc = kite.ohlc(f"NSE:{symbol}")
            if f"NSE:{symbol}" in ohlc:
                ltp = ohlc[f"NSE:{symbol}"].get("last_price")
                if ltp and ltp > 0:
                    return float(ltp)
    except Exception:
        pass

    # Fallback to cache
    try:
        from cache.data_cache import CacheManager
        ltp = CacheManager().read_ltp(symbol, max_age_seconds=300)
        if ltp:
            return float(ltp)
    except Exception:
        pass

    return None
