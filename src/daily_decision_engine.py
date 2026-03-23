"""
daily_decision_engine.py (v2)
------------------------------
Reads recent Juror signals from the DB, filters by confidence and
Sniper technical rules. Passes actionable signals to the AntigravityWatcher.

Execution (placing actual orders) is handled exclusively by TradeExecutor
in runner.py. This module is analysis-only.
"""
import os
import logging
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime
from src.db import init_db, SessionLocal, JurorSignal
from src.sniper.core import evaluate_signal
from src.sniper.logger import log_sniper_decision

logger = logging.getLogger(__name__)

MIN_JUROR_CONFIDENCE = 0.80


def main(watcher=None):
    logger.info("--- VoltEdgeAI Daily Decision Engine ---")
    init_db()

    with SessionLocal() as session:
        recent_signals = (
            session.query(JurorSignal)
            .order_by(JurorSignal.created_at.desc())
            .limit(50)
            .all()
        )

        if not recent_signals:
            logger.info("No signals found in DB. Skipping decision engine.")
            return

        logger.info(f"Decision engine: evaluating {len(recent_signals)} signal(s).")

        for row in recent_signals:
            # Step 1: Filter by Juror confidence
            if row.label != "Positive" or row.confidence is None or row.confidence < MIN_JUROR_CONFIDENCE:
                continue

            # Step 2: Apply Sniper technical rules
            try:
                res = evaluate_signal(row.symbol)
            except Exception as e:
                logger.warning(f"Sniper evaluate_signal failed for {row.symbol}: {e}")
                continue

            status = res.get("status", "SKIP")

            # Log decision
            try:
                log_sniper_decision(
                    symbol=row.symbol,
                    res=res,
                    context={"juror_label": row.label, "juror_confidence": row.confidence}
                )
            except Exception as e:
                logger.warning(f"Failed to log sniper decision for {row.symbol}: {e}")

            if status != "KEEP":
                logger.info(
                    f"{row.symbol} | SKIP | Juror={row.label} ({row.confidence:.2f}), "
                    f"Sniper={status} ({res.get('reason', '')})"
                )
                # If WAIT + antigravity stretch detected, hand to watcher
                if status == "WAIT" and watcher:
                    ag = res.get("antigravity", {})
                    if ag.get("status") == "WAITING_FOR_GRAVITY":
                        watcher.add_wait_signal(
                            symbol=row.symbol,
                            z_score=ag.get("z_score", 0.0),
                            vwap=ag.get("vwap", 0.0),
                            ltp=ag.get("ltp", 0.0),
                            now=datetime.now(),
                        )
                continue

            logger.info(
                f"{row.symbol} | KEEP | Juror={row.label} ({row.confidence:.2f}), "
                f"Sniper={status} — queued for execution via runner."
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
