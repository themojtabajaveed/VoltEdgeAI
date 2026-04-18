"""
router_tuner.py — read-only analysis of router performance history.

Aggregates the last N days of data/counterfactual_*.json files and suggests
whether the DAWN/HYDRA routing threshold should move. NEVER writes to
config.yaml — suggestion output only. Apply changes manually.

CLI usage: `python -m src.analysis.router_tuner`
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from src.config_loader import get_router_dawn_confidence_min

logger = logging.getLogger(__name__)

_DATA_DIR = Path("data")
_STEP = 0.05


def load_cf_history(days: int = 5) -> List[Dict[str, Any]]:
    """
    Load the most recent N counterfactual_*.json files from data/.

    Returns results sorted by date ascending. Missing dates are skipped silently.
    Uses file discovery (glob) so we don't rely on calendar math hitting trading days.
    """
    if not _DATA_DIR.exists():
        return []
    files = sorted(_DATA_DIR.glob("counterfactual_*.json"))
    results: List[Dict[str, Any]] = []
    for fp in files[-days:] if days > 0 else []:
        try:
            with open(fp) as f:
                results.append(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[RouterTuner] Skipping unreadable {fp.name}: {e}")
            continue
    results.sort(key=lambda r: str(r.get("date", "")))
    return results


def compute_tuning_suggestion(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Aggregate a run of counterfactual summaries and emit a tuning suggestion.

    Returns:
      { action, current_value, suggested_value, reason, days_analyzed,
        avg_router_accuracy, total_missed, total_correct }
    """
    current = get_router_dawn_confidence_min()
    days_analyzed = len(history)

    if days_analyzed == 0:
        return {
            "action": "NO_CHANGE",
            "current_value": current,
            "suggested_value": current,
            "reason": "No counterfactual history available",
            "days_analyzed": 0,
            "avg_router_accuracy": 0.0,
            "total_missed": 0,
            "total_correct": 0,
        }

    accuracies = [float(h.get("router_accuracy", 0.0) or 0.0) for h in history]
    avg_acc = sum(accuracies) / len(accuracies)
    total_missed = sum(int(h.get("missed_opportunities", 0) or 0) for h in history)
    total_correct = sum(int(h.get("correct_routes", 0) or 0) for h in history)

    if avg_acc >= 0.70:
        action = "NO_CHANGE"
        suggested = current
        reason = "Router performing well"
    elif total_missed > total_correct:
        action = "LOWER_DAWN_CONFIDENCE"
        suggested = round(max(0.0, current - _STEP), 4)
        reason = f"Missed {total_missed} opportunities in {days_analyzed} days"
    else:
        action = "RAISE_DAWN_CONFIDENCE"
        suggested = round(min(1.0, current + _STEP), 4)
        reason = f"Over-routing to DAWN — {total_correct} incorrect DAWN routes"

    return {
        "action": action,
        "current_value": current,
        "suggested_value": suggested,
        "reason": reason,
        "days_analyzed": days_analyzed,
        "avg_router_accuracy": round(avg_acc, 4),
        "total_missed": total_missed,
        "total_correct": total_correct,
    }


def print_tuning_report(days: int = 5) -> None:
    """Print a readable tuning report to stdout. Intended for CLI use."""
    history = load_cf_history(days=days)
    suggestion = compute_tuning_suggestion(history)

    print("=" * 64)
    print(f"VoltEdgeAI — Router Tuning Report (last {days} days)")
    print("=" * 64)

    if not history:
        print("No counterfactual history found in data/.")
        print(f"Current router.dawn_confidence_min = {suggestion['current_value']}")
        print("Action: NO_CHANGE (nothing to analyze)")
        return

    print(f"{'Date':<12} {'Accuracy':>10} {'Missed':>8} {'Correct':>8} {'Total':>8}")
    print("-" * 64)
    for h in history:
        print(
            f"{str(h.get('date','?')):<12} "
            f"{float(h.get('router_accuracy',0.0)):>9.1%} "
            f"{int(h.get('missed_opportunities',0)):>8d} "
            f"{int(h.get('correct_routes',0)):>8d} "
            f"{int(h.get('total_shadows',0)):>8d}"
        )
    print("-" * 64)
    print(f"Avg router accuracy : {suggestion['avg_router_accuracy']:.1%}")
    print(f"Total missed        : {suggestion['total_missed']}")
    print(f"Total correct       : {suggestion['total_correct']}")
    print()
    print(f"Current dawn_confidence_min : {suggestion['current_value']}")
    print(f"Suggested action            : {suggestion['action']}")
    if suggestion["action"] != "NO_CHANGE":
        print(f"Suggested value             : {suggestion['suggested_value']}")
    print(f"Reason                      : {suggestion['reason']}")
    print()
    print("To apply: edit config.yaml → router.dawn_confidence_min → restart service.")
    print("This tool NEVER modifies config.yaml automatically.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print_tuning_report(days=5)
