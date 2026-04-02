"""
mid_session_pulse.py — 12:00 IST Midday Checkpoint
----------------------------------------------------
Pure template report — NO LLM call. Runs in <5 seconds.

Sections:
  1. Market Phase: current phase, transitions, Nifty/VIX/A/D
  2. Conviction Watchboard: all WATCHING signals with Layer breakdown
  3. Morning Plan Check: predictions vs current price
  4. Position Status: open trades, unrealized PnL, slots
  5. Flags: warnings and alerts
"""
import os
import json
import logging
from datetime import datetime, date
from typing import Optional

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
    Generate the mid-session pulse report. Pure template, no LLM.

    All args are live objects from runner.py. Returns the report markdown.
    """
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)
    today = now.date()

    lines = [f"# VoltEdge Mid-Session Pulse — {today} {now.strftime('%H:%M')} IST\n"]

    # ── Section 1: Market Phase ──────────────────────────────────────────
    lines.append("## 1. Market Phase\n")
    if conviction_engine and market_snapshot:
        phase = conviction_engine.phase.value.upper()
        lines.append(f"**Current Phase**: {phase}\n")
        lines.append(
            f"| Metric | Value |\n"
            f"|--------|-------|\n"
            f"| Nifty % | {market_snapshot.nifty_pct:+.2f}% |\n"
            f"| VIX | {market_snapshot.vix:.1f} |\n"
            f"| A/D Ratio | {market_snapshot.ad_ratio:.2f} |\n"
            f"| Nifty Direction (5m) | {market_snapshot.nifty_direction_5m} |\n"
        )
        # Phase transitions
        transitions = conviction_engine.phase_state.transitions
        if transitions:
            lines.append("**Phase Transitions:**")
            for t in transitions:
                lines.append(f"- {t}")
            lines.append("")
        else:
            lines.append(f"No phase transitions — {phase} since market open.\n")

        # Sector performance
        if market_snapshot.sector_changes:
            lines.append("**Sector Performance:**\n")
            lines.append("| Sector | Change |")
            lines.append("|--------|--------|")
            for sector, chg in sorted(market_snapshot.sector_changes.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"| {sector} | {chg:+.2f}% |")
            lines.append("")
    else:
        lines.append("Market data unavailable (conviction engine or snapshot not provided).\n")

    # ── Section 2: Conviction Watchboard ─────────────────────────────────
    lines.append("## 2. Conviction Watchboard\n")
    if conviction_engine:
        active = conviction_engine.get_active_signals()
        if active:
            lines.append("| Symbol | Dir | Strategy | Conv | A | B | C | D | E | Trend | Event |")
            lines.append("|--------|-----|----------|------|---|---|---|---|---|-------|-------|")
            for sig in sorted(active, key=lambda s: s.last_conviction, reverse=True):
                history = sig.conviction_history
                if len(history) >= 2:
                    delta = history[-1][1] - history[-2][1]
                    trend = "^" if delta > 2 else ("v" if delta < -2 else "=")
                else:
                    trend = "~"
                # Extract last layer values from log (approximate from history)
                event_short = sig.event_summary[:40] if sig.event_summary else ""
                lines.append(
                    f"| {sig.symbol} | {sig.direction} | {sig.strategy} | "
                    f"{sig.last_conviction:.0f} | - | - | {sig.layer_c_score:.0f} | - | {sig.layer_e_score:.0f} | "
                    f"{trend} | {event_short} |"
                )
            lines.append(f"\n**Total**: {len(active)} signals watching, "
                        f"threshold = {conviction_engine._threshold:.0f}")
        else:
            lines.append("Watchboard is empty — no signals being tracked.\n")

        # Also show triggered/expired counts
        all_signals = list(conviction_engine._watchboard.values())
        triggered = [s for s in all_signals if s.status == "TRIGGERED"]
        expired = [s for s in all_signals if s.status == "EXPIRED"]
        if triggered or expired:
            lines.append(f"\nTriggered today: {len(triggered)} | Expired: {len(expired)}")
    else:
        lines.append("Conviction engine not available.\n")

    # ── Section 3: Morning Plan Check ────────────────────────────────────
    lines.append("\n## 3. Morning Plan Check\n")
    predictions = _load_today_predictions(today)
    if predictions and live_client:
        lines.append("| Symbol | Predicted | Current % | Tracking |")
        lines.append("|--------|-----------|-----------|----------|")
        for p in predictions:
            sym = p.get("symbol", "?")
            predicted_dir = (p.get("predicted_direction") or p.get("direction", "?")).upper()
            # Try to get current price
            current_pct = _get_current_pct(sym, live_client)
            if current_pct is not None:
                if predicted_dir == "BULLISH" and current_pct > 0.3:
                    tracking = "ON TRACK"
                elif predicted_dir == "BEARISH" and current_pct < -0.3:
                    tracking = "ON TRACK"
                elif abs(current_pct) < 0.3:
                    tracking = "FLAT"
                else:
                    tracking = "DIVERGING"
                lines.append(f"| {sym} | {predicted_dir} | {current_pct:+.2f}% | {tracking} |")
            else:
                lines.append(f"| {sym} | {predicted_dir} | N/A | No data |")
    elif predictions:
        lines.append("Predictions loaded but live data unavailable for checking.\n")
    else:
        lines.append("No morning predictions to check.\n")

    # ── Section 4: Position Status ───────────────────────────────────────
    lines.append("\n## 4. Position Status\n")
    if positions and slot_manager and risk_state:
        open_pos = positions.get_open_positions()
        slots_used = slot_manager.trades_today if hasattr(slot_manager, 'trades_today') else 0
        slots_remaining = slot_manager.remaining if hasattr(slot_manager, 'remaining') else "?"
        daily_pnl = float(risk_state.realized_pnl) if hasattr(risk_state, 'realized_pnl') else 0
        max_loss = risk_cfg.max_daily_loss_rupees if risk_cfg else "?"

        lines.append(f"- **Open Positions**: {len(open_pos)}")
        lines.append(f"- **Slots Used / Remaining**: {slots_used} / {slots_remaining}")
        lines.append(f"- **Realized PnL**: {daily_pnl:+.2f}")
        lines.append(f"- **Daily Loss Cap**: {max_loss}")
        lines.append(f"- **Trades Taken**: {risk_state.trades_taken if hasattr(risk_state, 'trades_taken') else '?'}\n")

        if open_pos:
            lines.append("| Symbol | Side | Qty | Entry | Current | Unrealized PnL |")
            lines.append("|--------|------|-----|-------|---------|----------------|")
            for pos in open_pos:
                current_price = pos.avg_price  # default
                if live_client:
                    tick = live_client.get_last_tick(pos.symbol)
                    if tick:
                        current_price = tick.ltp
                if pos.side == "LONG":
                    unrealized = (current_price - pos.avg_price) * pos.total_qty
                else:
                    unrealized = (pos.avg_price - current_price) * pos.total_qty
                lines.append(
                    f"| {pos.symbol} | {pos.side} | {pos.total_qty} | "
                    f"{pos.avg_price:.2f} | {current_price:.2f} | {unrealized:+.2f} |"
                )
        else:
            lines.append("No open positions.\n")
    else:
        lines.append("Position data not available.\n")

    # ── Section 5: Flags ─────────────────────────────────────────────────
    lines.append("\n## 5. Flags\n")
    flags = []
    if risk_state and risk_cfg:
        daily_pnl = float(risk_state.realized_pnl) if hasattr(risk_state, 'realized_pnl') else 0
        if daily_pnl < 0 and abs(daily_pnl) > risk_cfg.max_daily_loss_rupees * 0.7:
            flags.append(f"APPROACHING LOSS CAP: PnL={daily_pnl:+.2f} vs cap={risk_cfg.max_daily_loss_rupees}")
    if market_snapshot:
        if market_snapshot.vix > 22:
            flags.append(f"HIGH VIX: {market_snapshot.vix:.1f} — elevated volatility regime")
        if market_snapshot.ad_ratio < 0.3:
            flags.append(f"WEAK BREADTH: A/D={market_snapshot.ad_ratio:.2f} — broad selling")
        elif market_snapshot.ad_ratio > 0.7:
            flags.append(f"STRONG BREADTH: A/D={market_snapshot.ad_ratio:.2f} — broad buying")
    if conviction_engine:
        phase = conviction_engine.phase
        if hasattr(conviction_engine, '_phase_state'):
            transitions = conviction_engine.phase_state.transitions
            if len(transitions) >= 3:
                flags.append(f"UNSTABLE MARKET: {len(transitions)} phase transitions today")

    if flags:
        for f in flags:
            lines.append(f"- {f}")
    else:
        lines.append("No flags. System operating normally.")

    report_md = "\n".join(lines)

    # ── Save report ──────────────────────────────────────────────────────
    os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
    report_path = os.path.join("logs", "daily_reports", f"{today}_mid_session.md")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"[VoltEdge] Saved Mid-Session Pulse to: {report_path}")
    except Exception as e:
        logger.error(f"Failed to save mid-session report: {e}")

    # ── Email ────────────────────────────────────────────────────────────
    from src.reports.email_sender import send_report_email
    send_report_email(
        subject=f"VoltEdge Mid-Session Pulse — {today} {now.strftime('%H:%M')}",
        body_md=report_md,
        attachment_path=report_path,
    )

    return report_md


def _load_today_predictions(today: date) -> list:
    path = "data/prediction_log.json"
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            log = json.load(f)
        return [p for p in log.get("predictions", []) if p.get("date") == str(today)]
    except Exception:
        return []


def _get_current_pct(symbol: str, live_client) -> Optional[float]:
    """Try to get the current day % change for a symbol from the live client."""
    try:
        kite = getattr(live_client, '_kite', None)
        if kite:
            ohlc = kite.ohlc(f"NSE:{symbol}")
            if f"NSE:{symbol}" in ohlc:
                d = ohlc[f"NSE:{symbol}"]
                ltp = d.get("last_price", 0)
                prev_c = d.get("ohlc", {}).get("close", 0)
                if prev_c > 0 and ltp > 0:
                    return (ltp - prev_c) / prev_c * 100
    except Exception:
        pass
    return None
