"""
filing_archiver.py — NSE/BSE Filing Archiver for Event Study Pipeline
----------------------------------------------------------------------
Step 1 of the Event Study Pipeline. Fetches corporate announcements from
NSE/BSE, deduplicates, classifies via Groq, scores quality and freshness,
and persists in the filings_archive SQLite table (in data/history.db by default).
"""

import logging
import sqlite3
import time
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

from src.data_ingestion.exchange_filings import FilingEvent

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class FilingArchiver:

    def __init__(
        self,
        db_path: str = "data/history.db",
        lookback_days: int = 90,
        batch_size: int = 20,
    ) -> None:
        self.db_path = db_path
        self.lookback_days = lookback_days
        self.batch_size = batch_size
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        cursor = self._conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filings_archive (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                company_name    TEXT,
                exchange        TEXT    DEFAULT 'NSE',
                headline        TEXT    NOT NULL,
                headline_hash   TEXT    NOT NULL,
                category        TEXT,
                filed_at        TEXT    NOT NULL,
                urgency_score   REAL,
                event_type      TEXT,
                event_direction TEXT,
                event_summary   TEXT,
                deal_size_inr   REAL,
                quality_score   REAL,
                freshness_tag   TEXT,
                processed       INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT (datetime('now')),
                UNIQUE(symbol, filed_at, headline_hash)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_filings_filed_at ON filings_archive(filed_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_filings_symbol ON filings_archive(symbol)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_filings_processed ON filings_archive(processed)"
        )
        self._conn.commit()

    def backfill(self) -> int:
        """Fetch NSE/BSE filings for the full lookback window and archive them.

        Known limitation: The NSE corporate-announcements endpoint has no real
        pagination — it returns a fixed recent window (typically last ~24-48h)
        regardless of the hours_back value. lookback_days controls the DB filter
        intent, not the NSE API behaviour.
        """
        filings = self._fetch_nse_filings_paginated()
        new_filings = self._exclude_known(filings)
        if not new_filings:
            logger.info("[FilingArchiver] backfill: no new filings to archive.")
            return 0
        classifications = self._classify_batch(new_filings)
        count = self._store_batch(new_filings, classifications)
        logger.info("[FilingArchiver] backfill: %d new filings archived.", count)
        return count

    def incremental_update(self) -> int:
        """Fetch only filings since the last archived filing.

        Queries MAX(filed_at) from filings_archive. If the table is empty,
        delegates to backfill(). Otherwise computes elapsed hours since the
        last filing + a 2h overlap buffer, then calls fetch_exchange_filings()
        with that window.
        """
        cursor = self._conn.cursor()
        try:
            cursor.execute("SELECT MAX(filed_at) FROM filings_archive")
            row = cursor.fetchone()
        except sqlite3.OperationalError:
            return self.backfill()

        max_filed_at = row[0] if row else None
        if not max_filed_at:
            return self.backfill()

        try:
            last_dt = datetime.fromisoformat(max_filed_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=IST)
        except ValueError:
            return self.backfill()

        now_ist = datetime.now(IST)
        elapsed_hours = (now_ist - last_dt.astimezone(IST)).total_seconds() / 3600.0
        hours_back = max(1, int(elapsed_hours) + 2)

        logger.info(
            "[FilingArchiver] incremental_update: fetching last %dh (last filing: %s).",
            hours_back,
            max_filed_at,
        )

        try:
            from src.data_ingestion.exchange_filings import fetch_exchange_filings
            filings = fetch_exchange_filings(hours_back=hours_back)
        except Exception as e:
            logger.error("[FilingArchiver] incremental_update fetch failed: %s", e)
            return 0

        new_filings = self._exclude_known(filings)
        if not new_filings:
            logger.info("[FilingArchiver] incremental_update: no new filings.")
            return 0

        classifications = self._classify_batch(new_filings)
        count = self._store_batch(new_filings, classifications)
        logger.info("[FilingArchiver] incremental_update: %d new filings archived.", count)
        return count

    def _fetch_nse_filings_paginated(self) -> List[FilingEvent]:
        try:
            from src.data_ingestion.exchange_filings import fetch_exchange_filings
            hours_back = self.lookback_days * 24
            return fetch_exchange_filings(hours_back=hours_back)
        except Exception as e:
            logger.error("[FilingArchiver] _fetch_nse_filings_paginated failed: %s", e)
            return []

    def _exclude_known(self, filings: List[FilingEvent]) -> List[FilingEvent]:
        """Return only filings not already in the DB.

        Dedup key = (symbol, filed_at, headline_hash) where
        headline_hash = headline[:50].
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute("SELECT symbol, filed_at, headline_hash FROM filings_archive")
            known: set[tuple[str, str, str]] = {
                (r[0], r[1], r[2]) for r in cursor.fetchall()
            }
        except sqlite3.OperationalError:
            known = set()

        new_filings: List[FilingEvent] = []
        for f in filings:
            headline_hash = f.headline[:50]
            if (f.symbol, f.filed_at, headline_hash) not in known:
                new_filings.append(f)
        return new_filings

    def _classify_batch(self, filings: List[FilingEvent]) -> List[dict]:
        """Classify filings via Groq in sub-batches of self.batch_size.

        Sleeps 2s between batches to stay within Groq rate limits.
        On any Groq failure the entire sub-batch falls back to safe defaults.
        """
        _SAFE_DEFAULT: dict = {
            "urgency": 0,
            "direction": "NEUTRAL",
            "event_type": "UNKNOWN",
            "summary": "",
            "material": False,
        }

        all_results: List[dict] = []
        for i in range(0, len(filings), self.batch_size):
            batch = filings[i : i + self.batch_size]
            payload = [
                {
                    "symbol": f.symbol,
                    "headline": f.headline,
                    "category": f.category,
                    "body": "",
                }
                for f in batch
            ]
            try:
                from src.llm.groq_client import classify_events_batch
                results = classify_events_batch(payload)
                all_results.extend(results)
            except Exception as e:
                logger.error("[FilingArchiver] Groq classify_events_batch failed: %s", e)
                for _ in batch:
                    all_results.append(dict(_SAFE_DEFAULT))

            if i + self.batch_size < len(filings):
                time.sleep(2.0)

        return all_results

    def _store_batch(self, filings: List[FilingEvent], classifications: List[dict]) -> int:
        """Persist (filing, classification) pairs to filings_archive.

        Computes quality_score via score_event_quality() and freshness_tag via
        classify_filing_freshness() for each row. Returns count of rows actually
        inserted (INSERT OR IGNORE, so duplicates count as 0).
        """
        try:
            from src.utils.event_quality import score_event_quality
            from src.utils.filing_freshness import classify_filing_freshness
            from src.config_loader import get_event_quality_config, get_filing_freshness_config
        except Exception as e:
            logger.error("[FilingArchiver] _store_batch import error: %s", e)
            return 0

        eq_config = get_event_quality_config()
        ff_config = get_filing_freshness_config()
        now_ist = datetime.now(IST)

        inserted = 0
        cursor = self._conn.cursor()

        for filing, clf in zip(filings, classifications):
            try:
                headline_hash = filing.headline[:50]
                event_type = clf.get("event_type", "UNKNOWN")

                quality_score, _ = score_event_quality(
                    category=event_type,
                    filing_subject=filing.headline,
                    deal_size_inr=filing.deal_size_inr,
                    market_cap_inr=filing.market_cap_inr,
                    earnings_surprise_pct=filing.earnings_surprise_pct,
                    sector_followthrough=0.5,
                    recent_same_category_count=0,
                    config=eq_config,
                )

                try:
                    filing_ts = datetime.fromisoformat(filing.filed_at)
                    if filing_ts.tzinfo is None:
                        filing_ts = filing_ts.replace(tzinfo=IST)
                except ValueError:
                    filing_ts = now_ist

                freshness, _ = classify_filing_freshness(
                    filing_ts=filing_ts,
                    as_of=now_ist,
                    price_at_filing=None,
                    price_now=0.0,
                    config=ff_config,
                )
                freshness_tag = freshness.value

                cursor.execute("""
                    INSERT OR IGNORE INTO filings_archive (
                        symbol, company_name, exchange, headline, headline_hash,
                        category, filed_at, urgency_score, event_type, event_direction,
                        event_summary, deal_size_inr, quality_score, freshness_tag
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    filing.symbol,
                    filing.company_name,
                    filing.exchange,
                    filing.headline,
                    headline_hash,
                    filing.category,
                    filing.filed_at,
                    clf.get("urgency", filing.urgency),
                    event_type,
                    clf.get("direction", "NEUTRAL"),
                    clf.get("summary", ""),
                    filing.deal_size_inr,
                    quality_score,
                    freshness_tag,
                ))
                inserted += cursor.rowcount

            except Exception as e:
                logger.error(
                    "[FilingArchiver] _store_batch failed for %s: %s",
                    filing.symbol,
                    e,
                )

        self._conn.commit()
        return inserted
