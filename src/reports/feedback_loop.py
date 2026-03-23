"""
feedback_loop.py — 6:01 PM Prediction Scorer
----------------------------------------------
Runs after market close. For each stock prediction saved this morning,
looks up its actual pct_change in DailyPerformanceSnapshot and scores:
  +1  — predicted direction matches actual move > 0.3%
   0  — stock moved < 0.3% either way (flat)
  -1  — predicted direction is opposite of actual move

Also appends Gemini-generated lessons to the prediction log so tomorrow's
morning brief can reference them.
"""
import os
import json
import logging
from datetime import datetime, date

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

PREDICTION_LOG_PATH = "data/prediction_log.json"
FLAT_THRESHOLD_PCT  = 0.30   # moves smaller than this are scored as flat (0)


def _load_log() -> dict:
    if os.path.exists(PREDICTION_LOG_PATH):
        try:
            with open(PREDICTION_LOG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"predictions": [], "system_lessons": []}


def _save_log(log: dict) -> None:
    os.makedirs("data", exist_ok=True)
    with open(PREDICTION_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def _score_direction(predicted: str, actual_pct: float) -> int:
    predicted = (predicted or "").lower()
    if abs(actual_pct) < FLAT_THRESHOLD_PCT:
        return 0
    actual_dir = "bullish" if actual_pct > 0 else "bearish"
    if predicted == "sideways":
        return 0
    return 1 if predicted == actual_dir else -1


def _fetch_actual_changes(today: date) -> dict:
    """Return {symbol: pct_change} from DailyPerformanceSnapshot for today."""
    try:
        from src.db import SessionLocal, DailyPerformanceSnapshot, init_db
        init_db()
        with SessionLocal() as session:
            rows = session.query(DailyPerformanceSnapshot).filter(
                DailyPerformanceSnapshot.date == today
            ).all()
        return {r.symbol: r.pct_change for r in rows if r.pct_change is not None}
    except Exception as e:
        logger.warning(f"Could not fetch DailyPerformanceSnapshot: {e}")
        return {}


def _generate_lessons(scored_today: list) -> list[str]:
    """Ask Gemini to distil lessons from today's scored predictions."""
    if not scored_today:
        return []
    try:
        from google import genai
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return []
        client = genai.Client(api_key=api_key)

        summary = "\n".join(
            f"- {p['symbol']} | Predicted: {p['predicted_direction']} | "
            f"Actual: {p.get('actual_change_pct', '?')}% | "
            f"Score: {'+1 CORRECT' if p['score']==1 else ('❌ WRONG' if p['score']==-1 else 'FLAT')}"
            for p in scored_today
        )

        prompt = f"""Today's VoltEdge prediction scorecard:
{summary}

As a professional trading risk manager, provide exactly 3 concise, actionable lessons (1 sentence each)
that VoltEdge should apply tomorrow. Focus on WHY predictions were wrong and what signals to watch.
Format: one lesson per line, no bullet points, no preamble.
"""
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        lines = [l.strip() for l in resp.text.strip().split("\n") if l.strip()]
        today_str = str(datetime.now().date())
        return [f"{today_str}: {l}" for l in lines[:3]]
    except Exception as e:
        logger.warning(f"Gemini lesson generation failed: {e}")
        return []


def run_feedback_loop():
    today = datetime.now().date()
    log = _load_log()

    # Get today's unscored predictions
    today_preds = [
        p for p in log["predictions"]
        if p.get("date") == str(today) and p.get("score") is None
    ]

    if not today_preds:
        logger.info("No unscored predictions for today — feedback loop has nothing to do.")
        return

    actuals = _fetch_actual_changes(today)
    scored_today = []

    for pred in today_preds:
        symbol = pred["symbol"]
        actual_pct = actuals.get(symbol)
        if actual_pct is not None:
            pred["actual_change_pct"] = round(actual_pct, 2)
            pred["score"] = _score_direction(pred.get("predicted_direction", ""), actual_pct)
        else:
            # No data for this symbol today — leave as None
            pred["score"] = None
            logger.warning(f"No actual data for {symbol} — score left as None")
            continue

        # Update the prediction in the log
        for i, p in enumerate(log["predictions"]):
            if p["date"] == str(today) and p["symbol"] == symbol:
                log["predictions"][i] = pred
                scored_today.append(pred)
                break

    if scored_today:
        correct  = sum(1 for p in scored_today if p["score"] == 1)
        wrong    = sum(1 for p in scored_today if p["score"] == -1)
        total    = len(scored_today)
        accuracy = (correct / total * 100) if total > 0 else 0

        print(f"\n[VoltEdge Feedback] Today's prediction accuracy: {correct}/{total} correct ({accuracy:.0f}%)")
        for p in scored_today:
            icon = "✅" if p["score"] == 1 else ("❌" if p["score"] == -1 else "➖")
            print(f"  {icon} {p['symbol']}: predicted {p['predicted_direction']}, actual {p['actual_change_pct']}%")

        # Generate and store lessons
        lessons = _generate_lessons(scored_today)
        if lessons:
            log.setdefault("system_lessons", []).extend(lessons)
            # Keep only last 20 lessons
            log["system_lessons"] = log["system_lessons"][-20:]
            print(f"\n[VoltEdge Feedback] Lessons for tomorrow:")
            for l in lessons:
                print(f"  • {l}")

    _save_log(log)
    logger.info("Feedback loop complete — prediction_log.json updated.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_feedback_loop()
