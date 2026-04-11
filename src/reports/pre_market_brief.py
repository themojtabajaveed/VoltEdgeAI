"""
pre_market_brief.py — 6:00 AM Global Intelligence Brief
---------------------------------------------------------
Pulls last 12 hours of global news via Finnhub + Gemini Search,
assesses Indian market impact, generates 5 specific stock predictions,
saves regime JSON and persists predictions to prediction_log.json
for the evening feedback loop to score.
"""
import os
import re
import json
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def _load_prediction_log() -> dict:
    path = "data/prediction_log.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"predictions": [], "system_lessons": []}


def _save_prediction_log(log: dict) -> None:
    os.makedirs("data", exist_ok=True)
    with open("data/prediction_log.json", "w") as f:
        json.dump(log, f, indent=2)


def _build_lessons_context(log: dict) -> str:
    """Return last 5 lessons and last 5 scored predictions for the Gemini prompt."""
    lessons = log.get("system_lessons", [])[-5:]
    recent_preds = [
        p for p in log.get("predictions", [])
        if p.get("score") is not None
    ][-5:]

    out = []
    if recent_preds:
        out.append("**Recent Prediction Accuracy (last 5 scored calls):**")
        for p in recent_preds:
            score_label = {1: "✅ CORRECT", 0: "➖ FLAT", -1: "❌ WRONG"}.get(p.get("score"), "?")
            out.append(
                f"- {p['date']} | {p['symbol']} | Predicted: {(p.get('predicted_direction') or p.get('direction', '?')).upper()} | "
                f"Actual: {p.get('actual_change_pct', '?')}% | {score_label}"
            )

    if lessons:
        out.append("\n**System Lessons (applied to today's analysis):**")
        for l in lessons:
            out.append(f"- {l}")

    return "\n".join(out) if out else "No prior predictions scored yet — this is the first run."


def generate_pre_market_brief():
    """
    Morning Brief v2 entry point — delegates to brief_pipeline.
    Kept for backward compatibility with runner.py scheduling.
    """
    try:
        from src.reports.brief_pipeline import generate_morning_brief
        generate_morning_brief()
    except Exception as e:
        logger.error(f"[Brief] Morning Brief v2 pipeline failed: {e}")
        # Last-resort fallback: generate minimal data-only brief
        _generate_emergency_fallback(e)


def _generate_emergency_fallback(error: Exception) -> None:
    """Last-resort fallback if the v2 pipeline itself crashes."""
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).date()

    report_md = (
        f"# VoltEdge Morning Brief — {today} (EMERGENCY FALLBACK)\n\n"
        f"> **Pipeline error:** {error}\n\n"
        f"The morning brief v2 pipeline encountered an error. "
        f"Check logs for details. Trading engine will use default regime (sideways/0.5).\n"
    )

    try:
        os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
        report_path = os.path.join("logs", "daily_reports", f"{today}_morning_brief.md")
        with open(report_path, "w") as f:
            f.write(report_md)
        _send_email(f"[EMERGENCY] VoltEdge Morning Brief — {today}", report_md, report_path)
    except Exception:
        pass


def _send_email(subject: str, report_md: str, report_path: str) -> bool:
    try:
        from src.reports.email_sender import send_report_email
        return send_report_email(
            subject=subject,
            body_md=report_md,
            attachment_path=report_path if report_path else None,
        )
    except Exception as e:
        logger.error(f"[EMAIL] _send_email raised exception: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_pre_market_brief()
