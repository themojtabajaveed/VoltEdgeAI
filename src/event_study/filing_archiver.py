"""
filing_archiver.py — NSE/BSE Filing Archiver for Event Study Pipeline
----------------------------------------------------------------------
Step 1 of the Event Study Pipeline. Fetches corporate announcements from
NSE/BSE, deduplicates, classifies via Groq, scores quality and freshness,
and persists in the filings_archive SQLite table (in data/history.db by default).
"""

import csv
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import List
from zoneinfo import ZoneInfo

from src.data_ingestion.exchange_filings import FilingEvent

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Corporate-suffix list shared by _build_name_mapping and _map_bse_to_nse_symbol.
# " LT" is included to handle Zerodha CSV truncation (e.g. "SERV LT" → "SERV").
_SUFFIXES: list[str] = [
    " LIMITED", " LTD", " LTD.", " CORP", " CORPORATION",
    " INC", " PVT", " PRIVATE", " CO.", " CO",
    " INDUSTRIES", " INDUSTRY", " TECHNOLOGIES", " TECHNOLOGY",
    " SOLUTIONS", " SERVICES", " ENTERPRISES", " HOLDINGS",
    " FINANCE", " FINANCIAL", " BANK", " BANKS",
    " LT",
]

CATEGORY_TO_EVENT_TYPE: dict = {
    "Board Meeting":                          "BOARD_MEETING",
    "Financial Results":                      "EARNINGS",
    "Mergers/Acquisitions/Restructuring":     "MERGER",
    "Orders":                                 "ORDER_WIN",
    "Dividend":                               "DIVIDEND",
    "Analysts/Investors Meet":                "INVESTOR_MEET",
    "Change in Directors/ Key Managerial Personnel / Auditor":
                                              "MANAGEMENT_CHANGE",
    "General":                                "GENERAL",
    "Outcome of Board Meeting":               "BOARD_OUTCOME",
    "Shareholding":                           "SHAREHOLDING",
    "Allotment":                              "ALLOTMENT",
    "Trading Window":                         "TRADING_WINDOW",
    "Insider Trading / SAST":                 "INSIDER",
    "Regulation 30 (LODR)-Newspaper Publication": "NEWSPAPER",
}
DEFAULT_EVENT_TYPE = "GENERAL"


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
        # ── Symbol universe (built once at init; used by BSE fetch pipeline) ──
        self._nifty200: set = self._load_nifty200()
        self._name_to_symbol: dict = self._build_name_mapping()
        logger.info(
            "FilingArchiver: loaded %d name→symbol mappings for %d Nifty200 symbols",
            len(self._name_to_symbol),
            len(self._nifty200),
        )

    def _load_nifty200(self) -> set:
        """Load Nifty200 trading symbols from data/nifty200_symbols.json."""
        try:
            with open("data/nifty200_symbols.json") as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(data)
            if isinstance(data, dict):
                # Handles {"date": "...", "symbols": [...]} format
                if "symbols" in data:
                    return set(data["symbols"])
                logger.warning(
                    "FilingArchiver: nifty200_symbols.json has unexpected format, "
                    "keys: %s — using empty universe", list(data.keys())
                )
                return set()
        except FileNotFoundError:
            logger.warning(
                "FilingArchiver: data/nifty200_symbols.json not found — universe empty"
            )
        except Exception as e:
            logger.warning("FilingArchiver: failed to load nifty200_symbols.json: %s", e)
        return set()

    def _build_name_mapping(self) -> dict:
        """
        Build name→symbol lookup from data/zerodha_instruments.csv.
        Filters to NSE EQ rows whose tradingsymbol is in self._nifty200.
        Also adds suffix-stripped variants of each company name so that
        BSE announcement names (which often include 'LIMITED', 'LTD' etc.)
        resolve without an exact match.
        """
        mapping: dict = {}
        try:
            with open("data/zerodha_instruments.csv", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("exchange") == "NSE" and row.get("instrument_type") == "EQ":
                        symbol = row["tradingsymbol"].strip().upper()
                        name = row["name"].strip().upper()
                        if symbol in self._nifty200:
                            mapping[name] = symbol
        except FileNotFoundError:
            logger.warning(
                "FilingArchiver: data/zerodha_instruments.csv not found — name mapping disabled"
            )
            return mapping
        except Exception as e:
            logger.warning("FilingArchiver: failed to load zerodha_instruments.csv: %s", e)
            return mapping

        # Add suffix-stripped variants so e.g. "INFOSYS LIMITED" → "INFOSYS" → INFY
        extra: dict = {}
        for name, symbol in mapping.items():
            for suffix in _SUFFIXES:
                if name.endswith(suffix):
                    stripped = name[: -len(suffix)].strip()
                    if len(stripped) >= 4 and stripped not in mapping:
                        extra[stripped] = symbol
                    break  # only strip ONE suffix per name
        mapping.update(extra)
        return mapping

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
        # ── Event Study archive table (11-col, simpler schema for study pipeline) ──
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filing_archive (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                exchange        TEXT NOT NULL,
                filing_date     TEXT NOT NULL,
                filing_time     TEXT,
                category        TEXT,
                subcategory     TEXT,
                headline        TEXT,
                filing_url      TEXT,
                raw_json        TEXT,
                fetched_at      TEXT NOT NULL,
                UNIQUE(symbol, exchange, filing_date, headline)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_filing_archive_symbol_date"
            " ON filing_archive(symbol, filing_date)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_filing_archive_date"
            " ON filing_archive(filing_date)"
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

    # ── BSE HTTP backfill ────────────────────────────────────────────────────

    def backfill_bse(self, days_back: int = 90) -> dict:
        """
        Fetch BSE filings for each trading day going back days_back calendar
        days and persist to filing_archive.

        Returns summary dict:
            {dates_fetched, filings_inserted,
             filings_skipped_duplicate, errors}
        """
        # Lazy import — avoids circular dependency at module load time
        from src.config_loader import get_event_study_config

        cfg        = get_event_study_config()
        rate_limit = float(cfg.get("bse_api_rate_limit_seconds", 2.0))

        today        = datetime.now(timezone.utc).date()
        dates        = [today - timedelta(days=i) for i in range(1, days_back + 1)]
        trading_days = [d for d in dates if d.weekday() < 5]  # Mon–Fri only

        summary: dict = {
            "dates_fetched":             0,
            "filings_inserted":          0,
            "filings_skipped_duplicate": 0,
            "errors":                    [],
        }

        for i, date in enumerate(trading_days, 1):
            try:
                rows = self._fetch_bse_day(date)
                for row in rows:
                    if self.insert_filing(row):
                        summary["filings_inserted"] += 1
                    else:
                        summary["filings_skipped_duplicate"] += 1
                summary["dates_fetched"] += 1

                if i % 10 == 0:
                    logger.info(
                        "FilingArchiver backfill progress: %d/%d dates, "
                        "%d filings so far",
                        i, len(trading_days), summary["filings_inserted"],
                    )
            except Exception as e:
                logger.warning(
                    "FilingArchiver: error fetching BSE for %s: %s", date, e
                )
                summary["errors"].append({"date": str(date), "error": str(e)})

            time.sleep(rate_limit)

        logger.info(
            "FilingArchiver backfill complete: %d dates, %d inserted, "
            "%d duplicates, %d errors",
            summary["dates_fetched"],
            summary["filings_inserted"],
            summary["filings_skipped_duplicate"],
            len(summary["errors"]),
        )
        return summary

    def _fetch_bse_day(self, date) -> list:
        """
        Fetch BSE announcements for a single calendar date.
        Returns list of dicts ready for insert_filing().
        Returns empty list on any error — never raises.
        """
        import requests

        date_str = date.strftime("%Y-%m-%d")
        url = (
            "https://api.bseindia.com/BseIndiaAPI/api/"
            "AnnSubCategoryGetData/w"
        )
        params = {
            "pageno":      "1",
            "strCat":      "-1",
            "strPrevDate": date_str,
            "strScrip":    "",
            "strSearch":   "P",
            "strToDate":   date_str,
            "strType":     "C",
            "subcategory": "-1",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bseindia.com/",
            "Accept":  "application/json",
        }

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("_fetch_bse_day %s failed: %s", date_str, e)
            return []

        rows_raw = data.get("Table", [])
        if not rows_raw:
            return []

        fetched_at = datetime.now(timezone.utc).isoformat()
        results: list = []

        for raw in rows_raw:
            bse_name = (raw.get("SLONGNAME") or "").strip()
            symbol   = self._map_bse_to_nse_symbol(bse_name)
            if symbol is None:
                continue  # not in Nifty200 universe

            dt_tm       = (raw.get("DT_TM") or "").strip()
            filing_date = date_str
            filing_time = None
            if dt_tm:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        dt_parsed   = datetime.strptime(dt_tm, fmt)
                        filing_date = dt_parsed.strftime("%Y-%m-%d")
                        filing_time = dt_parsed.strftime("%H:%M:%S")
                        break
                    except ValueError:
                        continue

            results.append({
                "symbol":      symbol,
                "exchange":    "BSE",
                "filing_date": filing_date,
                "filing_time": filing_time,
                "category":    (raw.get("CATEGORYNAME")    or "").strip(),
                "subcategory": (raw.get("SUBCATEGORYNAME") or "").strip(),
                "headline":    (raw.get("HEADLINE")        or "").strip(),
                "filing_url":  None,
                "raw_json":    json.dumps(raw),
                "fetched_at":  fetched_at,
            })

        return results

    def _is_trading_day(self, date) -> bool:
        """Returns True if date is Mon–Fri (simple weekend filter)."""
        return date.weekday() < 5

    def promote_to_filings_archive(
        self,
        start_date: str = None,
        end_date: str = None,
    ) -> dict:
        """
        Copy rows from filing_archive (BSE backfill, 11-col) into
        filings_archive (Groq live table, 17-col) so build_all() can
        process historical filing events.

        Idempotent — INSERT OR IGNORE prevents duplicates.

        Args:
            start_date: YYYY-MM-DD or None (all rows)
            end_date:   YYYY-MM-DD or None (all rows)

        Returns:
            {"promoted": N, "skipped_duplicate": N, "errors": N}
        """
        import hashlib
        import json as _json

        # Fetch rows from filing_archive
        if start_date and end_date:
            rows = self._conn.execute(
                """SELECT * FROM filing_archive
                   WHERE filing_date >= ? AND filing_date <= ?
                   ORDER BY filing_date""",
                (start_date, end_date),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM filing_archive ORDER BY filing_date"
            ).fetchall()

        summary = {"promoted": 0, "skipped_duplicate": 0, "errors": 0}

        insert_sql = """
            INSERT OR IGNORE INTO filings_archive (
                symbol, company_name, exchange, headline, headline_hash,
                category, filed_at, urgency_score, event_type,
                event_direction, event_summary, deal_size_inr,
                quality_score, freshness_tag, processed
            ) VALUES (
                :symbol, :company_name, :exchange, :headline, :headline_hash,
                :category, :filed_at, :urgency_score, :event_type,
                :event_direction, :event_summary, :deal_size_inr,
                :quality_score, :freshness_tag, :processed
            )
        """

        for row in rows:
            r = {}
            try:
                r = dict(row)

                headline     = (r.get("headline") or "").strip()
                filing_date  = r.get("filing_date", "")
                filing_time  = r.get("filing_time") or "09:15:00"
                category     = (r.get("category") or "").strip()
                symbol       = (r.get("symbol") or "").strip()

                filed_at      = f"{filing_date}T{filing_time}+05:30"
                headline_hash = hashlib.md5(
                    headline.encode()
                ).hexdigest()[:16]
                event_type    = CATEGORY_TO_EVENT_TYPE.get(
                    category, DEFAULT_EVENT_TYPE
                )

                # Parse urgency + quality from raw_json if present
                urgency_score = 5.0
                quality_score = 5.0
                raw = r.get("raw_json")
                if raw:
                    try:
                        raw_data      = _json.loads(raw)
                        urgency_score = float(raw_data.get(
                            "filing_urgency",
                            raw_data.get("urgency_score", 5.0)
                        ))
                        quality_score = float(raw_data.get(
                            "filing_impact_score",
                            raw_data.get("quality_score", 5.0)
                        ))
                    except Exception:
                        pass

                params = {
                    "symbol":          symbol,
                    "company_name":    symbol,
                    "exchange":        r.get("exchange", "BSE"),
                    "headline":        headline,
                    "headline_hash":   headline_hash,
                    "category":        category,
                    "filed_at":        filed_at,
                    "urgency_score":   urgency_score,
                    "event_type":      event_type,
                    "event_direction": "UNKNOWN",
                    "event_summary":   headline,
                    "deal_size_inr":   None,
                    "quality_score":   quality_score,
                    "freshness_tag":   "BACKFILL",
                    "processed":       0,
                }

                with self._conn:
                    cur = self._conn.execute(insert_sql, params)
                    if cur.rowcount == 1:
                        summary["promoted"] += 1
                    else:
                        summary["skipped_duplicate"] += 1

            except Exception as e:
                logger.warning(
                    "promote_to_filings_archive: error for headline %r: %s",
                    r.get("headline", "?"), e
                )
                summary["errors"] += 1

        logger.info(
            "promote_to_filings_archive: %d promoted, %d duplicates, %d errors",
            summary["promoted"],
            summary["skipped_duplicate"],
            summary["errors"],
        )
        return summary

    # ── Event Study DB layer ──────────────────────────────────────────────────

    def insert_filing(self, row: dict) -> bool:
        """
        Insert one filing into filing_archive (11-col event study table).
        Returns True if inserted, False if duplicate (UNIQUE constraint).

        Required keys: symbol, exchange, filing_date, headline, fetched_at
        Optional keys: filing_time, category, subcategory, filing_url, raw_json
        """
        sql = """
            INSERT OR IGNORE INTO filing_archive
                (symbol, exchange, filing_date, filing_time,
                 category, subcategory, headline, filing_url,
                 raw_json, fetched_at)
            VALUES
                (:symbol, :exchange, :filing_date, :filing_time,
                 :category, :subcategory, :headline, :filing_url,
                 :raw_json, :fetched_at)
        """
        params = {
            "symbol":      row["symbol"],
            "exchange":    row["exchange"],
            "filing_date": row["filing_date"],
            "filing_time": row.get("filing_time"),
            "category":    row.get("category"),
            "subcategory": row.get("subcategory"),
            "headline":    row["headline"],
            "filing_url":  row.get("filing_url"),
            "raw_json":    row.get("raw_json"),
            "fetched_at":  row["fetched_at"],
        }
        with self._conn:
            cur = self._conn.execute(sql, params)
            return cur.rowcount == 1

    def get_filings_for_date_range(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> list:
        """
        Return all filings for a symbol between start_date and end_date
        inclusive. Dates as YYYY-MM-DD strings.
        """
        sql = """
            SELECT * FROM filing_archive
            WHERE symbol = ?
              AND filing_date >= ?
              AND filing_date <= ?
            ORDER BY filing_date, filing_time
        """
        cur = self._conn.execute(sql, (symbol, start_date, end_date))
        return [dict(r) for r in cur.fetchall()]

    def get_all_filings_in_range(
        self,
        start_date: str,
        end_date: str,
        categories: list = None,
    ) -> list:
        """
        Return all filings in date range, optionally filtered by category.
        Used by EventStudyBuilder to iterate all filing events.
        """
        if categories:
            placeholders = ",".join("?" * len(categories))
            sql = f"""
                SELECT * FROM filing_archive
                WHERE filing_date >= ?
                  AND filing_date <= ?
                  AND category IN ({placeholders})
                ORDER BY filing_date, symbol
            """
            params = [start_date, end_date] + list(categories)
        else:
            sql = """
                SELECT * FROM filing_archive
                WHERE filing_date >= ?
                  AND filing_date <= ?
                ORDER BY filing_date, symbol
            """
            params = [start_date, end_date]

        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def get_stats(self) -> dict:
        """
        Return summary stats about filing_archive contents.
        Always returns a dict — never raises, even on empty DB.
        """
        try:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM filing_archive"
            ).fetchone()[0]

            if total == 0:
                return {
                    "total_filings": 0,
                    "date_range": [None, None],
                    "by_category": {},
                    "symbols_covered": 0,
                }

            date_range = self._conn.execute(
                "SELECT MIN(filing_date), MAX(filing_date) FROM filing_archive"
            ).fetchone()

            by_cat = self._conn.execute(
                """SELECT category, COUNT(*) as cnt
                   FROM filing_archive
                   GROUP BY category
                   ORDER BY cnt DESC"""
            ).fetchall()

            symbols = self._conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM filing_archive"
            ).fetchone()[0]

            return {
                "total_filings": total,
                "date_range": [date_range[0], date_range[1]],
                "by_category": {r[0] or "Unknown": r[1] for r in by_cat},
                "symbols_covered": symbols,
            }
        except Exception as e:
            logger.error("FilingArchiver.get_stats error: %s", e)
            return {
                "total_filings": 0,
                "date_range": [None, None],
                "by_category": {},
                "symbols_covered": 0,
            }

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()

    # ── Symbol mapping ────────────────────────────────────────────────────────

    def _map_bse_to_nse_symbol(self, bse_name: str) -> str | None:
        """
        Map a BSE company name to an NSE trading symbol using _name_to_symbol.

        Steps (first match wins):
          1. Direct uppercase lookup
          2. Suffix-stripped lookup (strips one common corporate suffix)
          3. Substring scan with suffix-normalised keys — shorter string >= 6 chars
          4. None
        """
        def _strip_one_suffix(s: str) -> str:
            for sfx in _SUFFIXES:
                if s.endswith(sfx):
                    return s[: -len(sfx)].strip()
            return s

        # Step 1 — Direct lookup
        normalized = bse_name.strip().upper()
        if normalized in self._name_to_symbol:
            return self._name_to_symbol[normalized]

        # Step 2 — Suffix-stripped lookup
        stripped = _strip_one_suffix(normalized)

        if stripped != normalized and stripped in self._name_to_symbol:
            return self._name_to_symbol[stripped]

        # Step 3 — Substring scan; both sides are suffix-normalised so that
        # "TATA CONSULTANCY SERVICES" matches "TATA CONSULTANCY SERV LT" after
        # stripping " LT" from the CSV key.
        if len(stripped) >= 6:
            for name_key, symbol in self._name_to_symbol.items():
                clean_key = _strip_one_suffix(name_key)
                shorter = min(clean_key, stripped, key=len)
                if len(shorter) < 6:
                    continue
                if stripped in clean_key or clean_key in stripped:
                    return symbol

        # Step 4 — No match
        return None
