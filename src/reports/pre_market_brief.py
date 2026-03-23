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
import smtplib
import logging
from email.message import EmailMessage
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
                f"- {p['date']} | {p['symbol']} | Predicted: {p['predicted_direction'].upper()} | "
                f"Actual: {p.get('actual_change_pct', '?')}% | {score_label}"
            )

    if lessons:
        out.append("\n**System Lessons (applied to today's analysis):**")
        for l in lessons:
            out.append(f"- {l}")

    return "\n".join(out) if out else "No prior predictions scored yet — this is the first run."


def generate_pre_market_brief():
    from google import genai
    from google.genai import types

    try:
        from src.data_ingestion.finnhub_client import fetch_global_sentiment
    except Exception as e:
        logger.warning(f"Could not import finnhub_client: {e}")
        fetch_global_sentiment = lambda: {}

    today = datetime.now().date()
    log = _load_prediction_log()
    lessons_context = _build_lessons_context(log)

    try:
        finnhub_data = fetch_global_sentiment()
    except Exception as e:
        logger.warning(f"Finnhub fetch failed: {e}")
        finnhub_data = {}

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not set — cannot generate brief.")
        return

    client = genai.Client(api_key=api_key)

    prompt = f"""You are VoltEdge's senior pre-market analyst with 20+ years of experience in Indian equity markets.

Today is {today}. Your task is to deliver a structured, factual, actionable morning intelligence brief.
Focus ONLY on events from the last 12 hours (since 6 PM yesterday). Do NOT repeat stale information.

## Feedback Loop Context (Learn from past predictions):
{lessons_context}

## Finnhub News Data (last 12 hours):
```json
{json.dumps(finnhub_data, indent=2)[:4000]}
```

Generate a markdown report with EXACTLY this structure:

---

# VoltEdge Global Intelligence Brief — {today}

## 1. Overnight Global Events
Summarize US market close, key earnings releases, Fed/macro events, commodity moves (oil, gold, DXY).
Include Asian market morning action and GIFT Nifty futures indication.
Be specific — include actual numbers (Nasdaq +X%, Oil at $X, etc.)

## 2. Indian Market Impact Assessment
For each major global event above, state the DIRECT impact on Indian sectors:
| Event | Affected Indian Sector/Stock | Expected Impact | Reasoning |
|-------|------------------------------|-----------------|-----------|

## 3. Today's 5 Stock Predictions
Provide exactly 5 specific NSE stock calls. For each:
| Symbol | Direction (Bullish/Bearish) | Key Level to Watch | Reason (1 sentence) |
|--------|----------------------------|--------------------|----------------------|

## 4. VoltEdge Tactical Directives
Provide exactly 3 concrete rules for today's trading engine:
1. [Directive 1]
2. [Directive 2]
3. [Directive 3]

## 5. Risk Regime Assessment
State overall market bias for today.

---

At the very end, output this JSON block (used by the trading engine):
```json
{{
  "trend": "bullish|bearish|sideways",
  "strength": 0.0_to_1.0,
  "top_sectors_long": ["SECTOR1", "SECTOR2"],
  "top_sectors_short": ["SECTOR3"],
  "predictions": [
    {{"symbol": "RELIANCE", "direction": "bullish", "key_level": 1450.0, "reason": "one sentence"}},
    {{"symbol": "INFY", "direction": "bearish", "key_level": 1750.0, "reason": "one sentence"}},
    {{"symbol": "HDFCBANK", "direction": "bullish", "key_level": 1900.0, "reason": "one sentence"}},
    {{"symbol": "BHARTIARTL", "direction": "bullish", "key_level": 1620.0, "reason": "one sentence"}},
    {{"symbol": "AXISBANK", "direction": "sideways", "key_level": 1080.0, "reason": "one sentence"}}
  ]
}}
```
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(tools=[{"google_search": {}}]),
        )
        report_md = response.text

        # ── Parse and save regime JSON ──────────────────────────────────────
        match = re.search(r"```json\s*(\{.*?\})\s*```", report_md, re.DOTALL)
        if match:
            try:
                regime_data = json.loads(match.group(1))

                os.makedirs("data", exist_ok=True)
                regime_path = "data/daily_regime.json"
                with open(regime_path, "w") as jf:
                    json.dump(
                        {"trend": regime_data.get("trend", "sideways"),
                         "strength": regime_data.get("strength", 0.5)},
                        jf, indent=2
                    )
                logger.info(f"Regime saved: {regime_data.get('trend')} / {regime_data.get('strength')}")

                # ── Persist today's predictions to the feedback log ──────────
                new_predictions = regime_data.get("predictions", [])
                for pred in new_predictions:
                    pred["date"] = str(today)
                    pred["score"] = None   # will be filled by feedback_loop.py tonight
                    pred["actual_change_pct"] = None
                    # Remove duplicates for same symbol+date
                    log["predictions"] = [
                        p for p in log["predictions"]
                        if not (p["date"] == str(today) and p["symbol"] == pred["symbol"])
                    ]
                    log["predictions"].append(pred)

                _save_prediction_log(log)
                logger.info(f"Saved {len(new_predictions)} predictions to prediction_log.json")

            except Exception as parse_err:
                logger.warning(f"Failed to parse regime JSON from report: {parse_err}")

        # ── Save Markdown report ────────────────────────────────────────────
        os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
        report_path = os.path.join("logs", "daily_reports", f"{today}_morning_brief.md")
        with open(report_path, "w") as f:
            f.write(report_md)
        print(f"[VoltEdge] Morning brief saved to: {report_path}")

        # ── Email dispatch ─────────────────────────────────────────────────
        _send_email(
            subject=f"VoltEdge Morning Brief — {today}",
            report_md=report_md,
            report_path=report_path,
        )

    except Exception as e:
        logger.error(f"pre_market_brief failed: {e}")
        raise


def _send_email(subject: str, report_md: str, report_path: str) -> None:
    if os.getenv("REPORT_EMAIL_ENABLED") != "1":
        return
    to_addr   = os.getenv("REPORT_EMAIL_TO")
    smtp_host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("REPORT_SMTP_PORT", 587))
    smtp_user = os.getenv("REPORT_SMTP_USER")
    smtp_pass = os.getenv("REPORT_SMTP_PASSWORD")

    if not all([to_addr, smtp_user, smtp_pass]):
        logger.warning("SMTP credentials missing — skipping email.")
        return
    try:
        import markdown as md_lib
        html = md_lib.markdown(report_md, extensions=["tables", "nl2br"])
    except ImportError:
        html = f"<pre>{report_md}</pre>"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.set_content(report_md)
    msg.add_alternative(html, subtype="html")
    with open(report_path, "rb") as fh:
        msg.add_attachment(fh.read(), maintype="text", subtype="markdown",
                           filename=os.path.basename(report_path))
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)
        logger.info(f"Morning brief emailed to {to_addr}")
    except Exception as e:
        logger.warning(f"Email failed: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_pre_market_brief()
