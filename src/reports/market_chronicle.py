"""
market_chronicle.py — 6:00 PM Market Chronicle
------------------------------------------------
Runs once daily at 18:00. Generates a structured post-market report covering:
  - What was predicted this morning vs what actually happened
  - Intraday agent decision timeline (timestamped)
  - Trade-by-trade analysis with entry setup, exit reason, and PnL
  - Post-market assessment: what went right, what went wrong, and why
  - Generates lessons for tomorrow's morning brief

Replaces the old daily_summary.py (which had hallucinated filler text).
"""
import os
import re
import json
import smtplib
import logging
from email.message import EmailMessage
from datetime import datetime, date

import sys
if "." not in sys.path:
    sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()
load_dotenv()
logger = logging.getLogger(__name__)


def _read_file_tail(path: str, n_lines: int = 80) -> str:
    """Read the last N lines of a log file safely."""
    if not os.path.exists(path):
        return "(log file not found)"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n_lines:])
    except Exception as e:
        return f"(error reading log: {e})"


def _read_morning_brief(today: date) -> str:
    """Search for morning brief in all known save locations."""
    candidates = [
        os.path.join("logs", "daily_reports", f"{today}_morning_brief.md"),
        os.path.join("logs", "daily_reports", f"voltedge_{today}", f"{today}_morning_brief.md"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()[:3000]
    return "(No morning brief found for today)"


def _load_prediction_log_context(today: date) -> str:
    path = "data/prediction_log.json"
    if not os.path.exists(path):
        return "(No prediction log found)"
    try:
        with open(path) as f:
            log = json.load(f)
        today_preds = [p for p in log.get("predictions", []) if p.get("date") == str(today)]
        if not today_preds:
            return "(No predictions were saved for today)"
        lines = ["**Today's morning predictions:**"]
        for p in today_preds:
            score_str = ""
            if p.get("score") is not None:
                icon = {1: "✅", 0: "➖", -1: "❌"}.get(p["score"], "?")
                score_str = f" → {icon} actual {p.get('actual_change_pct', '?')}%"
            lines.append(
                f"- {p['symbol']} | {p['predicted_direction'].upper()} @ {p.get('key_level', '?')}{score_str}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"(Error loading prediction log: {e})"


def _fetch_db_context(today: date) -> dict:
    """Pull trades, decisions and snapshots from SQLite for today."""
    try:
        from src.db import SessionLocal, TradeRecord, DecisionRecord, DailyPerformanceSnapshot, init_db
        init_db()
        with SessionLocal() as session:
            trades = session.query(TradeRecord).filter(
                TradeRecord.exit_time >= datetime.combine(today, datetime.min.time()),
                TradeRecord.exit_time <= datetime.combine(today, datetime.max.time()),
            ).order_by(TradeRecord.exit_time).all()

            decisions = session.query(DecisionRecord).filter(
                DecisionRecord.created_at >= datetime.combine(today, datetime.min.time()),
                DecisionRecord.created_at <= datetime.combine(today, datetime.max.time()),
            ).order_by(DecisionRecord.created_at).all()

            snapshots = session.query(DailyPerformanceSnapshot).filter(
                DailyPerformanceSnapshot.date == today
            ).all()

        trade_list = [
            {
                "symbol":      t.symbol,
                "direction":   t.direction,
                "qty":         t.qty,
                "entry_price": t.entry_price,
                "exit_price":  t.exit_price,
                "pnl":         round(t.pnl, 2),
                "entry_time":  t.entry_time.strftime("%H:%M") if t.entry_time else "?",
                "exit_time":   t.exit_time.strftime("%H:%M") if t.exit_time else "?",
                "mode":        t.mode,
                "strategy":    t.strategy,
                "exit_reason": t.exit_reason,
            }
            for t in trades
        ]

        decision_timeline = [
            {
                "time":       d.created_at.strftime("%H:%M:%S") if d.created_at else "?",
                "symbol":     d.symbol,
                "status":     d.status,
                "reason":     d.reason,
                "confidence": d.juror_confidence,
            }
            for d in decisions
        ]

        day_pnl   = round(sum(t.pnl for t in trades), 2)
        win_count = sum(1 for t in trades if t.pnl > 0)
        win_rate  = round(win_count / len(trades) * 100, 1) if trades else 0.0

        gainers = sorted(
            [s for s in snapshots if s.side == "gainer" and s.pct_change is not None],
            key=lambda x: x.pct_change, reverse=True
        )[:5]
        losers = sorted(
            [s for s in snapshots if s.side == "loser" and s.pct_change is not None],
            key=lambda x: x.pct_change
        )[:5]

        return {
            "stats": {
                "num_signals": len(decisions),
                "num_trades": len(trades),
                "day_pnl": day_pnl,
                "win_rate_pct": win_rate,
            },
            "trades": trade_list,
            "decision_timeline": decision_timeline,
            "top_gainers": [{"symbol": g.symbol, "pct": round(g.pct_change, 2)} for g in gainers],
            "top_losers":  [{"symbol": l.symbol, "pct": round(l.pct_change, 2)} for l in losers],
        }
    except Exception as e:
        logger.warning(f"DB fetch failed: {e}")
        return {
            "stats": {"num_signals": 0, "num_trades": 0, "day_pnl": 0, "win_rate_pct": 0},
            "trades": [],
            "decision_timeline": [],
            "top_gainers": [],
            "top_losers": [],
        }


def _extract_dragon_events_from_log(log_tail: str) -> str:
    """
    Pull HYDRA/VIPER/EXIT/CONFLUENCE events from the runner log tail.
    These are the events the Dragon Architecture produced today that never
    made it into the DB's DecisionRecord table.
    """
    import re
    relevant_lines = []
    patterns = [
        r"HYDRA", r"VIPER", r"CONFLUENCE", r"EXIT", r"SL_HIT", r"TP_HIT",
        r"TRAILING", r"COIL", r"BUY \d+x", r"SHORT \d+x", r"EXECUTE",
        r"Regime=", r"\[RSI DIV\]", r"MACD_DISTRIBUTION", r"PARTIAL_EXIT",
        r"Grok", r"conviction=",
    ]
    combined = "|".join(patterns)
    for line in log_tail.splitlines():
        if re.search(combined, line, re.IGNORECASE):
            relevant_lines.append(line.strip())
    if not relevant_lines:
        return "(No Dragon Architecture events found in log — may be first trading day)"
    return "\n".join(relevant_lines[-60:])  # Last 60 relevant lines


def generate_market_chronicle(
    target_date=None,
    traded_symbols: set = None,
    hydra_candidates: list = None,
    viper_candidates: list = None,
) -> None:
    from google import genai
    from google.genai import types

    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = target_date or datetime.now(IST).date()

    # Duplicate guard: skip if today's chronicle already exists
    existing_paths = [
        os.path.join("logs", "daily_reports", f"{today}_chronicle.md"),
        os.path.join("logs", "daily_reports", f"voltedge_{today}", f"{today}_chronicle.md"),
    ]
    for ep in existing_paths:
        if os.path.exists(ep):
            print(f"[VoltEdge] Chronicle already exists at {ep} — skipping duplicate generation.")
            return

    morning_brief   = _read_morning_brief(today)
    prediction_ctx  = _load_prediction_log_context(today)
    # Try both known log locations
    runner_log_tail = _read_file_tail("/tmp/voltedge_logs/runner.log", n_lines=150)
    if "not found" in runner_log_tail:
        runner_log_tail = _read_file_tail("logs/runner.log", n_lines=150)
    db_ctx          = _fetch_db_context(today)

    # Build focused Dragon Architecture event timeline from log
    dragon_events = _extract_dragon_events_from_log(runner_log_tail)

    # Build context about what the system was watching/trading today
    focus_context = ""
    if traded_symbols:
        focus_context += f"\n**Symbols actually traded today:** {', '.join(sorted(traded_symbols))}"
    if hydra_candidates:
        syms = [c.get('symbol', '?') if isinstance(c, dict) else getattr(c, 'symbol', '?') for c in hydra_candidates[:8]]
        focus_context += f"\n**HYDRA watchlist (events watched):** {', '.join(syms)}"
    if viper_candidates:
        syms = [c.get('symbol', '?') if isinstance(c, dict) else getattr(c, 'symbol', '?') for c in viper_candidates[:8]]
        focus_context += f"\n**VIPER watchlist (movers watched):** {', '.join(syms)}"

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not set — cannot generate chronicle.")
        return

    client = genai.Client(api_key=api_key)

    prompt = f"""You are VoltEdge's post-market chronicle analyst — a senior trader reviewing today's entire session.

Today: {today}
{focus_context}

## This Morning's Brief (what we predicted):
{morning_brief}

## Today's Predictions vs Reality:
{prediction_ctx}

## System Performance Data (from SQLite):
```json
{json.dumps(db_ctx, indent=2)}
```

## Dragon Architecture Events (HYDRA + VIPER signals from runner log):
```
{dragon_events}
```

## System Log — Last 150 lines:
```
{runner_log_tail}
```

Generate the Market Chronicle in EXACTLY this format. Be specific, factual, and professional.
IMPORTANT: For Section 2, use the Dragon Architecture Events log above — NOT the decision_timeline from the DB (which is populated by the old V1 pipeline, now inactive).
If traded_symbols above is empty, state "No trades executed today" rather than inventing trades.

---

# VoltEdge Market Chronicle — {today}

## 1. Pre-Market Plan vs Reality
Compare what was predicted this morning against what the market actually did.
Be honest — state which calls were right, which were wrong, and why.

## 2. Intraday Agent Timeline
Based on the Dragon Architecture Events log, list every significant event chronologically:
| Time | Symbol | Event | System Reaction |
|------|--------|---------|-----------------|
(Include: HYDRA events, VIPER strikes, stop-loss hits, trailing stop adjustments, exits, Grok orchestrator decisions)

## 3. Trade-by-Trade Analysis
For each trade in the trades list:
**{{symbol}} — {{direction}} — PnL: ₹{{pnl}}**
- Entry: {{entry_time}} @ ₹{{entry_price}} | Exit: {{exit_time}} @ ₹{{exit_price}}
- Entry Trigger: [what indicator/strategy triggered this]
- Exit Reason: {{exit_reason}}
- Assessment: [what went right or wrong with this specific trade]

## 4. Post-Market Scorecard
| Metric | Value |
|--------|-------|
| Total Trades | |
| Win Rate | |
| Day P&L | |
| Best Trade | |
| Worst Trade | |

## 5. What Went Right
3-4 specific things the system executed correctly today (with evidence from the data).

## 6. What Went Wrong
3-4 specific failures or missed opportunities (be precise — what indicator gave a false signal? what rule caused a bad exit?)

## 7. Tomorrow's Priority Actions
3 specific directives for tomorrow based on today's learnings.
These should be different from the morning brief's directives — these come from actual market experience.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(tools=[{"google_search": {}}]),
        )
        report_md = response.text

        # ── Save report ─────────────────────────────────────────────────────
        try:
            os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
            report_path = os.path.join("logs", "daily_reports", f"{today}_chronicle.md")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_md)
        except Exception as e:
            logger.warning(f"Could not save to logs/daily_reports: {e}. Falling back to data/")
            report_path = os.path.join("data", f"{today}_chronicle.md")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_md)
        
        print(f"[VoltEdge] Market Chronicle saved to: {report_path}")

        # ── Email dispatch ───────────────────────────────────────────────────
        _send_email(
            subject=f"VoltEdge Market Chronicle — {today}",
            report_md=report_md,
            report_path=report_path,
        )

    except Exception as e:
        logger.error(f"market_chronicle failed: {e}")
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
        logger.info(f"Chronicle emailed to {to_addr}")
    except Exception as e:
        logger.warning(f"Email failed: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_market_chronicle()
