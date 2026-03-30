"""
coil_reporter.py — COIL Dry-Run Performance Reporter
------------------------------------------------------
Generates comprehensive reports on COIL (mean reversion) dry-run signals.
Tracks hypothetical P&L, win rate, and identifies improvement areas.

Reports:
  - Daily: JSON log with each signal's outcome
  - Weekly: Aggregated performance summary with learnings
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from glob import glob

logger = logging.getLogger(__name__)

COIL_LOG_DIR = os.path.join("logs", "viper_coil")


def update_coil_outcomes(
    date_str: str,
    price_data: Dict[str, Dict],
) -> None:
    """
    Update a COIL report with actual market outcomes.
    Called at EOD to fill in what actually happened.

    Args:
        date_str: "2026-03-28"
        price_data: {symbol: {max_price, min_price, close_price, vwap}}
    """
    report_path = os.path.join(COIL_LOG_DIR, f"{date_str}_coil_report.json")
    if not os.path.exists(report_path):
        logger.info(f"[COILReporter] No report for {date_str}")
        return

    with open(report_path, "r") as f:
        report = json.load(f)

    signals = report.get("coil_signals", [])
    wins = 0
    losses = 0

    for signal in signals:
        symbol = signal["symbol"]
        if symbol not in price_data:
            continue

        prices = price_data[symbol]
        entry_price = float(signal["entry_price"])
        sl = float(signal["hypothetical_sl"])
        target = float(signal["hypothetical_target"])
        direction = signal["direction"]

        max_price = prices.get("max_price", entry_price)
        min_price = prices.get("min_price", entry_price)
        close_price = prices.get("close_price", entry_price)

        if direction == "SHORT":
            max_favorable = entry_price - min_price
            max_adverse = max_price - entry_price
            would_hit_target = min_price <= target
            would_hit_sl = max_price >= sl
            pnl_pct = (entry_price - close_price) / entry_price * 100
        else:  # BUY (fading oversold)
            max_favorable = max_price - entry_price
            max_adverse = entry_price - min_price
            would_hit_target = max_price >= target
            would_hit_sl = min_price <= sl
            pnl_pct = (close_price - entry_price) / entry_price * 100

        # Determine outcome
        if would_hit_sl and would_hit_target:
            # Both hit — check which was hit first (we don't have tick data, assume SL first if adverse > favorable)
            won = max_favorable > max_adverse
        elif would_hit_target:
            won = True
        elif would_hit_sl:
            won = False
        else:
            won = pnl_pct > 0

        if won:
            wins += 1
        else:
            losses += 1

        signal["actual_outcome"] = {
            "max_price": round(max_price, 2),
            "min_price": round(min_price, 2),
            "close_price": round(close_price, 2),
            "max_favorable_move": round(max_favorable, 2),
            "max_adverse_move": round(max_adverse, 2),
            "would_have_hit_target": would_hit_target,
            "would_have_hit_sl": would_hit_sl,
            "won": won,
            "pnl_pct": round(pnl_pct, 2),
        }

    # Update summary
    total = wins + losses
    report["summary"]["wins"] = wins
    report["summary"]["losses"] = losses
    report["summary"]["win_rate"] = round(wins / total * 100, 1) if total > 0 else 0
    report["summary"]["outcomes_filled"] = True

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info(
        f"[COILReporter] Updated {date_str}: {wins}W/{losses}L "
        f"({report['summary']['win_rate']}% win rate)"
    )


def generate_weekly_summary() -> Optional[str]:
    """
    Aggregate the last 7 days of COIL reports into a weekly summary.

    Returns:
        Path to the weekly summary file, or None if no data.
    """
    os.makedirs(COIL_LOG_DIR, exist_ok=True)
    reports = sorted(glob(os.path.join(COIL_LOG_DIR, "*_coil_report.json")))

    if not reports:
        return None

    # Take last 7 reports
    recent = reports[-7:]
    all_signals = []
    dates = []

    for rp in recent:
        try:
            with open(rp, "r") as f:
                data = json.load(f)
            dates.append(data.get("date", ""))
            for sig in data.get("coil_signals", []):
                if "actual_outcome" in sig:
                    all_signals.append(sig)
        except Exception:
            continue

    if not all_signals:
        return None

    # Compute aggregates
    total = len(all_signals)
    wins = sum(1 for s in all_signals if s.get("actual_outcome", {}).get("won", False))
    losses = total - wins

    avg_conviction = sum(s.get("conviction_score", 0) for s in all_signals) / total
    avg_pnl = sum(s.get("actual_outcome", {}).get("pnl_pct", 0) for s in all_signals) / total

    # Best and worst trades
    by_pnl = sorted(all_signals, key=lambda s: s.get("actual_outcome", {}).get("pnl_pct", 0))
    worst = by_pnl[0] if by_pnl else None
    best = by_pnl[-1] if by_pnl else None

    # Move type breakdown
    move_types: Dict[str, Dict] = {}
    for sig in all_signals:
        mt = sig.get("move_type", "UNKNOWN")
        if mt not in move_types:
            move_types[mt] = {"total": 0, "wins": 0, "avg_pnl": 0, "pnls": []}
        move_types[mt]["total"] += 1
        pnl = sig.get("actual_outcome", {}).get("pnl_pct", 0)
        move_types[mt]["pnls"].append(pnl)
        if sig.get("actual_outcome", {}).get("won", False):
            move_types[mt]["wins"] += 1

    for mt_data in move_types.values():
        if mt_data["pnls"]:
            mt_data["avg_pnl"] = round(sum(mt_data["pnls"]) / len(mt_data["pnls"]), 2)
        del mt_data["pnls"]

    # Conviction vs outcome correlation
    high_conv_signals = [s for s in all_signals if s.get("conviction_score", 0) >= 80]
    high_conv_wins = sum(1 for s in high_conv_signals if s.get("actual_outcome", {}).get("won", False))

    summary = {
        "period": f"{dates[0] if dates else '?'} to {dates[-1] if dates else '?'}",
        "total_signals": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        "avg_conviction": round(avg_conviction, 1),
        "avg_pnl_pct": round(avg_pnl, 2),
        "high_conviction_accuracy": (
            f"{high_conv_wins}/{len(high_conv_signals)} "
            f"({round(high_conv_wins / len(high_conv_signals) * 100, 1) if high_conv_signals else 0}%)"
        ),
        "best_trade": {
            "symbol": best.get("symbol", ""),
            "pnl_pct": best.get("actual_outcome", {}).get("pnl_pct", 0),
        } if best else None,
        "worst_trade": {
            "symbol": worst.get("symbol", ""),
            "pnl_pct": worst.get("actual_outcome", {}).get("pnl_pct", 0),
        } if worst else None,
        "move_type_breakdown": move_types,
        "recommendation": _generate_recommendation(wins, losses, avg_pnl, move_types),
    }

    summary_path = os.path.join(COIL_LOG_DIR, f"weekly_summary_{dates[-1]}.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    logger.info(f"[COILReporter] Weekly summary: {summary_path}")
    return summary_path


def _generate_recommendation(
    wins: int, losses: int, avg_pnl: float, move_types: dict
) -> str:
    """Generate a human-readable recommendation based on COIL performance."""
    total = wins + losses
    if total < 5:
        return "Insufficient data (need at least 5 signals for assessment)"

    win_rate = wins / total * 100

    if win_rate >= 65 and avg_pnl > 0.5:
        return (
            f"STRONG PERFORMER: {win_rate:.0f}% win rate, avg P&L {avg_pnl:+.2f}%. "
            f"Consider enabling COIL for live trading."
        )
    elif win_rate >= 50 and avg_pnl > 0:
        best_type = max(move_types.items(), key=lambda x: x[1].get("avg_pnl", 0))[0] if move_types else "?"
        return (
            f"MODERATE: {win_rate:.0f}% win rate, avg P&L {avg_pnl:+.2f}%. "
            f"Best performance on {best_type} patterns. Consider selective activation."
        )
    else:
        worst_type = min(move_types.items(), key=lambda x: x[1].get("avg_pnl", 0))[0] if move_types else "?"
        return (
            f"NEEDS WORK: {win_rate:.0f}% win rate, avg P&L {avg_pnl:+.2f}%. "
            f"Worst on {worst_type}. Keep in dry-run mode and refine entry timing."
        )
