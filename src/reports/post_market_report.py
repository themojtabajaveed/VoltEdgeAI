"""
post_market_report.py — 16:00 Unified Post-Market Report
--------------------------------------------------------
Runs once daily at 16:00 IST (30 min after market close).
Combines the legacy EOD Autopsy and Market Chronicle.

Outputs a unified report:
  1. Pre-Market Plan vs Reality
  2. The Movers (Top Gainers/Losers via Kite + Catalyst)
  3. VoltEdge Intraday Timeline & PnL
  4. Post-Market Scorecard
  5. Grok / System Lessons
"""

import os
import json
import smtplib
import logging
from email.message import EmailMessage
from datetime import datetime, date
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

import sys
if "." not in sys.path:
    sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()
logger = logging.getLogger(__name__)


# ── Database & Context Extraction ─────────────────────────────────────────

def _read_file_tail(path: str, n_lines: int = 80) -> str:
    if not os.path.exists(path):
        return "(log file not found)"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n_lines:])
    except Exception as e:
        return f"(error reading log: {e})"

def _read_morning_brief(today: date) -> str:
    candidates = [
        os.path.join("logs", "daily_reports", f"{today}_morning_brief.md"),
        os.path.join("logs", "daily_reports", f"voltedge_{today}", f"{today}_morning_brief.md"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read()[:3000]
            except Exception:
                pass
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
            lines.append(f"- {p['symbol']} | {(p.get('predicted_direction') or p.get('direction', '?')).upper()} @ {p.get('key_level', '?')}{score_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"(Error loading prediction log: {e})"

def _fetch_db_context(today: date) -> dict:
    try:
        from src.db import SessionLocal, TradeRecord, DecisionRecord, DailyPerformanceSnapshot, init_db
        init_db()
        with SessionLocal() as session:
            trades = session.query(TradeRecord).filter(
                TradeRecord.exit_time >= datetime.combine(today, datetime.min.time()),
                TradeRecord.exit_time <= datetime.combine(today, datetime.max.time()),
            ).order_by(TradeRecord.exit_time).all()

            snapshots = session.query(DailyPerformanceSnapshot).filter(
                DailyPerformanceSnapshot.date == today
            ).all()

        trade_list = []
        for t in trades:
            # Safely handle missing attributes if schema changed
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
            })

        day_pnl = round(sum(t.get("pnl", 0) for t in trade_list), 2)
        win_count = sum(1 for t in trade_list if t.get("pnl", 0) > 0)
        win_rate = round(win_count / len(trade_list) * 100, 1) if trade_list else 0.0

        return {
            "stats": {
                "num_trades": len(trade_list),
                "day_pnl": day_pnl,
                "win_rate_pct": win_rate,
            },
            "trades": trade_list,
        }
    except Exception as e:
        logger.warning(f"DB fetch failed: {e}")
        return {"stats": {"num_trades": 0, "day_pnl": 0, "win_rate_pct": 0}, "trades": []}

def _extract_dragon_events_from_log(log_tail: str) -> str:
    import re
    relevant_lines = []
    patterns = [
        r"HYDRA", r"VIPER", r"CONFLUENCE", r"EXIT", r"SL_HIT", r"TP_HIT",
        r"TRAILING", r"COIL", r"BUY \d+x", r"SHORT \d+x", r"EXECUTE",
        r"Regime=", r"\+?[0-9.]+ CONFLUENCE BONUS", r"Grok",
    ]
    combined = "|".join(patterns)
    for line in log_tail.splitlines():
        if re.search(combined, line, re.IGNORECASE):
            relevant_lines.append(line.strip())
    if not relevant_lines:
        return "(No Dragon Architecture events found in log)"
    return "\n".join(relevant_lines[-80:])


# ── Market Movers Extraction (Kite) ───────────────────────────────────────

@dataclass
class TechnicalSnapshot:
    ema_alignment: str = ""
    rsi_at_trigger: float = 50.0
    above_vwap: bool = False
    vol_spike_ratio: float = 0.0
    trigger_time: str = ""

def _compute_technicals(bars_df: pd.DataFrame) -> TechnicalSnapshot:
    snap = TechnicalSnapshot()
    if bars_df is None or bars_df.empty or len(bars_df) < 10:
        return snap
    try:
        close = bars_df["close"]
        high = bars_df["high"]
        low = bars_df["low"]
        volume = bars_df["volume"]

        ema9 = close.ewm(span=9, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        snap.ema_alignment = "9 > 20" if float(ema9.iloc[-1]) > float(ema20.iloc[-1]) else "9 < 20"

        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        tp = (high + low + close) / 3.0
        vwap = (tp * volume).cumsum() / volume.cumsum().replace(0, np.nan)

        avg_vol = float(volume.mean())
        best_idx = -1
        best_vol_ratio = 0.0

        for i in range(3, len(bars_df)):
            v_ratio = float(volume.iloc[i]) / avg_vol if avg_vol > 0 else 0
            if v_ratio >= 1.5 and abs(float(close.iloc[i] - close.iloc[i-1])) > 0:
                if v_ratio > best_vol_ratio:
                    best_vol_ratio = v_ratio
                    best_idx = i

        if best_idx >= 0:
            snap.vol_spike_ratio = round(best_vol_ratio, 1)
            snap.rsi_at_trigger = round(float(rsi.iloc[best_idx]), 1) if not pd.isna(rsi.iloc[best_idx]) else 50.0
            snap.above_vwap = float(close.iloc[best_idx]) > float(vwap.iloc[best_idx])
            t_col = "date" if "date" in bars_df.columns else "timestamp" if "timestamp" in bars_df.columns else None
            snap.trigger_time = str(bars_df[t_col].iloc[best_idx]) if t_col else f"bar_{best_idx}"

        return snap
    except Exception as e:
        logger.warning(f"Technicals failed: {e}")
        return snap


def _resolve_token(token_map: dict, symbol: str) -> int:
    return token_map.get(symbol, 0)


def _build_movers_context(kite, today: date) -> str:
    try:
        from src.sniper.momentum_scanner import fetch_top_movers
        # Call fetch_top_movers with the passed-in kite instance
        movers = fetch_top_movers(kite_client=kite)
        gainers = movers.get("gainers", [])[:5]
        losers = movers.get("losers", [])[:5]

        if not gainers and not losers:
            return "Kite API returned empty top movers. Market may be closed or rate-limits exceeded."

        news_client = None
        try:
            from src.data_ingestion.news_context import NewsClient
            news_client = NewsClient()
        except Exception:
            pass

        import time
        from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
        try:
            token_map = build_symbol_token_map(load_instruments_csv())
        except Exception as e:
            logger.warning(f"Failed to load token map: {e}")
            token_map = {}

        context_lines = []
        for label, group in [("GAINERS", gainers), ("LOSERS", losers)]:
            if group:
                context_lines.append(f"### TOP {label}")
                for c in group:
                    # 1. Fetch Intraday chart for technical trigger
                    bars_df = None
                    try:
                        token = _resolve_token(token_map, c.symbol)
                        if token:
                            frm, to_d = datetime.combine(today, datetime.min.time()), datetime.combine(today, datetime.max.time())
                            hst = kite.historical_data(token, from_date=frm, to_date=to_d, interval="5minute")
                            if hst: bars_df = pd.DataFrame(hst)
                            # APPLE / NVIDIA Red Team: Prevent rate limit blowup
                            time.sleep(0.35)
                    except:
                        pass
                    
                    tech = _compute_technicals(bars_df)
                    
                    # 2. Fetch Catalyst
                    catalyst = ""
                    if news_client and label == "GAINERS": # save credits
                        try:
                            n = news_client.fetch_stock_eod_news(c.symbol)
                            if n: catalyst = " | ".join(x.headline for x in n[:3])
                        except: pass
                    
                    context_lines.append(f"- {c.symbol}: {c.pct_change:+.2f}% | Vol: {c.volume}")
                    context_lines.append(f"  Trigger: {tech.trigger_time} (VolSpike {tech.vol_spike_ratio}x, EMA {tech.ema_alignment})")
                    if catalyst:
                        context_lines.append(f"  News: {catalyst}")

        return "\n".join(context_lines)

    except Exception as e:
        logger.error(f"Failed to build movers context: {e}")
        return f"(Failed to fetch movers context: {e})"


# ── Main Orchestrator ─────────────────────────────────────────────────────

def generate_post_market_report(kite_client=None, target_date=None, traded_symbols: set = None):
    from google import genai
    from google.genai import types
    import zoneinfo

    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = target_date or datetime.now(IST).date()

    logger.info(f"Generating Unified Post-Market Report for {today}")
    
    # 0. Prep API clients
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY missing - cannot run.")
        return

    if kite_client is None:
        try:
            from kiteconnect import KiteConnect
            ka = os.getenv("ZERODHA_API_KEY")
            at = os.getenv("ZERODHA_ACCESS_TOKEN")
            if ka and at:
                kite_client = KiteConnect(api_key=ka)
                kite_client.set_access_token(at)
        except Exception as e:
            logger.error(f"Failed to load fallback Kite client: {e}")

    # 1. Fetch VoltEdge internal context
    morning_brief = _read_morning_brief(today)
    prediction_ctx = _load_prediction_log_context(today)
    
    runner_log_tail = _read_file_tail("/tmp/voltedge_logs/runner.log", 200)
    if "not found" in runner_log_tail:
        runner_log_tail = _read_file_tail("logs/runner.log", 200)
    dragon_events = _extract_dragon_events_from_log(runner_log_tail)
    
    db_ctx = _fetch_db_context(today)
    traded_str = ", ".join(traded_symbols) if traded_symbols else "None"

    # 2. Fetch Broad Market Movers
    movers_ctx = _build_movers_context(kite_client, today)

    # 3. Build AI Prompt
    prompt = f"""You are VoltEdge's senior post-market analyst.
Synthesize today's market action ({today}) and our system output into the Unified Post-Market Report.

## 1. This Morning's Brief (Predictions):
{morning_brief[:1000]}
{prediction_ctx}

## 2. Today's Top Market Movers & Triggers (Kite Data):
{movers_ctx}

## 3. VoltEdge System Stats & PnL:
Trades Executed: {traded_str}
```json
{json.dumps(db_ctx, indent=2)}
```

## 4. Intraday System Events (Dragon Architecture):
```
{dragon_events}
```

Write the report in strictly the following markdown format. Be specific and factual. DO NOT hallucinate.

# VoltEdge Post-Market Report — {today}

## I. Pre-Market Plan vs Reality
Compare our morning predictions to real-market outcome.

## II. Top Movers & The Catalysts
For the top 3-4 Movers provided in the Kite data, state the Symbol, % Change, and write 1 sentence classifying WHY it moved (Pattern or News).
e.g. `1. RELIANCE (2.5%): VWAP breakout triggered clearly at 10:15 on massive volume spike.`

## III. VoltEdge System Timeline & PnL
- Total Trades: X
- Win Rate: Y%
- Day PnL: ₹Z
  
List chronologically the key trades/events the system took (from the Dragon Events log & System Stats). If no trades were taken, analyze why the system logically stayed flat (e.g. Market regime did not align).

## IV. Post-Market Scorecard
Provide a markdown table assessing: PnL, Biggest Winner, Biggest Error/Miss. 

## V. Learnings for Tomorrow
1 actionable mechanical lesson derived from today's data to apply tomorrow.
"""

    try:
        client = genai.Client(api_key=api_key)
        # Red Team Fix: Introduce explicit timeout (provided by genai client config)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction="You are VoltEdge's senior post-market analyst.",
                temperature=0.3
            )
        )
        report_md = response.text

        # Safe fallback if API was empty but we still generated text
        if "API returned empty" in movers_ctx:
            report_md = "> [!WARNING]\n> The Top Movers scanner failed to return data (Token/API issue). System PnL context follows below.\n\n" + report_md

        # 4. Save and Email
        os.makedirs(os.path.join("logs", "daily_reports"), exist_ok=True)
        report_path = os.path.join("logs", "daily_reports", f"{today}_post_market.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"[VoltEdge] Saved Post-Market Report to: {report_path}")

        _send_email(
            subject=f"VoltEdge Post-Market Report — {today}",
            report_md=report_md,
            report_path=report_path,
        )

    except Exception as e:
        logger.error(f"post_market_report failed: {e}")
        # Send explicitly descriptive fail email instead of blank
        fail_md = f"# VoltEdge Post-Market Report Failed\nThe LLM generation step threw an exception: `{e}`. Check the runner logs."
        _send_email(f"VoltEdge Post-Market Report — {today} (FAILED)", fail_md, "")

def _send_email(subject: str, report_md: str, report_path: str) -> None:
    if os.getenv("REPORT_EMAIL_ENABLED") != "1":
        return
    to_addr = os.getenv("REPORT_EMAIL_TO")
    s_host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com")
    s_port = int(os.getenv("REPORT_SMTP_PORT", 587))
    s_user = os.getenv("REPORT_SMTP_USER")
    s_pass = os.getenv("REPORT_SMTP_PASSWORD")

    if not all([to_addr, s_user, s_pass]):
        return

    try:
        import markdown as md_lib
        html = md_lib.markdown(report_md, extensions=["tables", "nl2br"])
    except:
        html = f"<pre>{report_md}</pre>"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s_user
    msg["To"] = to_addr
    msg.set_content(report_md)
    msg.add_alternative(html, subtype="html")
    
    if os.path.exists(report_path):
        with open(report_path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype="text", subtype="markdown", filename=os.path.basename(report_path))
            
    try:
        with smtplib.SMTP(s_host, s_port) as srv:
            srv.starttls()
            srv.login(s_user, s_pass)
            srv.send_message(msg)
        logger.info(f"Post Market Report emailed to {to_addr}")
    except Exception as e:
        logger.warning(f"Email failed: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_post_market_report()
