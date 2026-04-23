"""
run_event_study.py
------------------
One-time runner for the full Event Study Pipeline.

Execution order:
  Step 1:   FilingArchiver.incremental_update()  — fetch + store NSE filings
  Step 2-4: EventStudyBuilder.build_all()        — price windows + intraday + TA
  Backfill: build_intraday_only() + build_ta_only() for any rows skipped on auth error
  Step 5:   EventStudyExporter.export_all()      — JSON + 3 CSVs

Designed to run on the GCP VM where:
  - data/history.db has real Kite OHLCV cache
  - Kite access token is live (auto-refreshed by voltedge.service)
  - NSE API is accessible

Kite auth errors (token expired / 401):
  - Logged per filing; those rows are left with NULL price/TA columns
  - Safe to re-run after token refresh — backfill methods resume from NULLs
"""

if __name__ == "__main__":
    import logging
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("logs/event_study_run.log"),
            logging.StreamHandler()
        ]
    )
    logger = logging.getLogger("event_study_runner")
    IST = ZoneInfo("Asia/Kolkata")
    start_time = datetime.now(IST)
    logger.info("=" * 60)
    logger.info("VoltEdgeAI Event Study Pipeline — START")
    logger.info("Run time: %s", start_time.isoformat())
    logger.info("=" * 60)

    # ── Step 1: Filing Archive ──────────────────────────────────
    logger.info("[Step 1] FilingArchiver.incremental_update()")
    try:
        from src.event_study.filing_archiver import FilingArchiver
        archiver = FilingArchiver(lookback_days=90, batch_size=20)
        filings_added = archiver.incremental_update()
        logger.info("[Step 1] DONE — %d new filings archived.", filings_added)
    except Exception as e:
        logger.error("[Step 1] FAILED — %s", e)
        logger.error("Cannot proceed without filing data. Exiting.")
        raise SystemExit(1)

    # ── Step 2+3+4: Event Study Builder ────────────────────────
    logger.info("[Step 2-4] EventStudyBuilder.build_all()")
    builder = None
    try:
        from src.event_study.event_study_builder import EventStudyBuilder
        builder = EventStudyBuilder()
        rows_built = builder.build_all()
        logger.info("[Step 2-4] DONE — %d event study rows built.", rows_built)
    except Exception as e:
        logger.error("[Step 2-4] FAILED — %s", e)
        logger.warning("Partial data may exist. Exporter will run on whatever is available.")

    # ── Backfill pass: intraday + TA for any skipped rows ──────
    logger.info("[Backfill] Running build_intraday_only() for any NULL intraday rows...")
    try:
        intraday_filled = builder.build_intraday_only()
        logger.info("[Backfill] %d intraday rows backfilled.", intraday_filled)
    except Exception as e:
        logger.warning("[Backfill] build_intraday_only failed — %s", e)

    logger.info("[Backfill] Running build_ta_only() for any NULL TA rows...")
    try:
        ta_filled = builder.build_ta_only()
        logger.info("[Backfill] %d TA rows backfilled.", ta_filled)
    except Exception as e:
        logger.warning("[Backfill] build_ta_only failed — %s", e)

    # ── Step 5: Export ──────────────────────────────────────────
    logger.info("[Step 5] EventStudyExporter.export_all()")
    try:
        from src.event_study.event_study_exporter import EventStudyExporter
        exporter = EventStudyExporter(output_dir="data/exports")
        result = exporter.export_all()
        if not result:
            logger.warning("[Step 5] No processed rows to export yet.")
        else:
            logger.info("[Step 5] DONE — %d events exported.", result["total_events"])
            logger.info("  JSON:          %s", result["json"])
            logger.info("  CSV master:    %s", result["csv_master"])
            logger.info("  CSV daily:     %s", result["csv_daily"])
            logger.info("  CSV intraday:  %s", result["csv_intraday"])
    except Exception as e:
        logger.error("[Step 5] FAILED — %s", e)

    # ── Summary ─────────────────────────────────────────────────
    end_time = datetime.now(IST)
    elapsed = (end_time - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info("Event Study Pipeline COMPLETE in %.1fs", elapsed)
    logger.info("=" * 60)
