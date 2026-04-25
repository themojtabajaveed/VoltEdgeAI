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
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".env", override=False)

    import logging
    import os
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

    # ── Startup: verify env vars ──────────────────────────────────
    groq_key = os.getenv("GROQ_API_KEY", "")
    kite_token = os.getenv("ZERODHA_ACCESS_TOKEN", "")
    if not groq_key:
        logger.warning("[Startup] GROQ_API_KEY not set — filings will use UNKNOWN event_type")
    if not kite_token:
        logger.error("[Startup] ZERODHA_ACCESS_TOKEN not set — price data will be empty")
        raise SystemExit(1)

    # ── Startup: initialize KiteConnect ──────────────────────────
    from kiteconnect import KiteConnect
    api_key = os.getenv("ZERODHA_API_KEY", "")
    if not api_key:
        logger.error("[Startup] ZERODHA_API_KEY not set — cannot initialize KiteConnect")
        raise SystemExit(1)
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(kite_token)
    logger.info("[Startup] KiteConnect initialized with access token.")

    # ── Startup: clear stale empty event_study rows ───────────────
    import sqlite3
    _stale_conn = sqlite3.connect("data/history.db")
    _stale_conn.execute("""
        UPDATE filings_archive SET processed=0
        WHERE id IN (
            SELECT filing_id FROM event_study
            WHERE price_t0_close IS NULL
        )
    """)
    cur2 = _stale_conn.execute("""
        DELETE FROM event_study WHERE price_t0_close IS NULL
    """)
    deleted = cur2.rowcount
    _stale_conn.commit()
    _stale_conn.close()
    logger.info("[Startup] Cleared %d empty event_study rows — filings_archive.processed reset for re-processing.", deleted)

    # Step 12: counters are initialised here so the final summary line is
    # well-defined even if a downstream step fails.
    filings_added = 0
    rows_built = 0
    intraday_filled = 0
    ta_filled = 0
    exported_count = 0

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
        builder = EventStudyBuilder(kite_client=kite)
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
            exported_count = result.get("total_events", 0)
            logger.info("[Step 5] DONE — %d events exported.", exported_count)
            logger.info("  JSON:          %s", result["json"])
            logger.info("  CSV master:    %s", result["csv_master"])
            logger.info("  CSV daily:     %s", result["csv_daily"])
            logger.info("  CSV intraday:  %s", result["csv_intraday"])
    except Exception as e:
        logger.error("[Step 5] FAILED — %s", e)

    # ── Summary ─────────────────────────────────────────────────
    # Step 12: Single-line operator summary. `skipped` counts filings whose
    # symbol could not be resolved to a Kite token (processed=-1 sentinel
    # introduced in Step 6); these are retry-eligible on a future run.
    skipped_count = 0
    try:
        _sum_conn = sqlite3.connect("data/history.db")
        row = _sum_conn.execute(
            "SELECT COUNT(*) FROM filings_archive WHERE processed=-1"
        ).fetchone()
        skipped_count = row[0] if row else 0
        _sum_conn.close()
    except sqlite3.Error as e:
        logger.warning("[Summary] Could not read skipped count: %s", e)

    end_time = datetime.now(IST)
    elapsed = (end_time - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info(
        "[event_study] filings_added=%d rows_built=%d skipped=%d "
        "intraday_filled=%d ta_filled=%d exported=%d elapsed=%.1fs",
        filings_added, rows_built, skipped_count,
        intraday_filled, ta_filled, exported_count, elapsed,
    )
    logger.info("Event Study Pipeline COMPLETE in %.1fs", elapsed)
    logger.info("=" * 60)
