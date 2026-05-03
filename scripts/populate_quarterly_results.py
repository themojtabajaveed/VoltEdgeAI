"""Bulk-populate quarterly financial results from yfinance.

Usage:
    .venv/bin/python scripts/populate_quarterly_results.py              # POC: 10 symbols
    .venv/bin/python scripts/populate_quarterly_results.py --full       # Nifty 200
    .venv/bin/python scripts/populate_quarterly_results.py --symbols RELIANCE,INFY,TCS

Fetches quarterly income statement data from Yahoo Finance (free, no auth),
converts to INR Crores, maps to Indian fiscal year/quarter, and upserts
into the quarterly_results SQLite table.

Limitations:
    - yfinance returns 4-5 quarters per symbol (not 8)
    - Some smaller stocks may have missing or stale data
    - EBITDA not available for banks/NBFCs
    - Dates use calendar quarter-end (aligns with Indian FY convention)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import date
from pathlib import Path
from typing import List, Optional

import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.models_quarterly import create_quarterly_tables, upsert_quarter
from src.db import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

INR_TO_CR = 1e7

POC_SYMBOLS = [
    "KOTAKBANK", "DMART", "RELIANCE", "INFY", "TCS",
    "HDFCBANK", "ICICIBANK", "BHARTIARTL", "AXISBANK", "SUNPHARMA",
]


def _quarter_end_to_fiscal(quarter_end: date) -> tuple[int, int]:
    """Map calendar quarter-end to Indian fiscal quarter and year.

    Indian FY runs April→March. The FY "year" is the year March falls in.
      Jun 30, 2025 → Q1 FY2026 (fiscal_quarter=1, fiscal_year=2026)
      Sep 30, 2025 → Q2 FY2026
      Dec 31, 2025 → Q3 FY2026
      Mar 31, 2026 → Q4 FY2026
    """
    month = quarter_end.month
    year = quarter_end.year
    if month == 6:
        return 1, year + 1
    elif month == 9:
        return 2, year + 1
    elif month == 12:
        return 3, year + 1
    elif month == 3:
        return 4, year
    # Non-standard quarter end — best effort
    if month in (4, 5):
        return 1, year + 1
    elif month in (7, 8):
        return 2, year + 1
    elif month in (10, 11):
        return 3, year + 1
    else:  # Jan, Feb
        return 4, year
    return 4, year


def _safe_float(val) -> Optional[float]:
    """Convert to float, returning None for NaN/None/inf."""
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def fetch_and_store(symbol: str, session) -> int:
    """Fetch quarterly results for one symbol and store in DB.

    Returns number of quarters successfully stored.
    """
    ticker_yf = f"{symbol}.NS"
    try:
        ticker = yf.Ticker(ticker_yf)
        qs = ticker.quarterly_income_stmt
    except Exception as e:
        logger.warning("[%s] yfinance fetch failed: %s", symbol, e)
        return 0

    if qs is None or qs.empty:
        logger.warning("[%s] no quarterly data returned", symbol)
        return 0

    stored = 0
    # Insert oldest-first so YoY computation finds prior-year data
    for col in reversed(qs.columns):
        quarter_end = col.date()
        fiscal_quarter, fiscal_year = _quarter_end_to_fiscal(quarter_end)

        revenue_raw = qs.loc["Total Revenue", col] if "Total Revenue" in qs.index else None
        pat_raw = qs.loc["Net Income", col] if "Net Income" in qs.index else None
        eps_raw = qs.loc["Basic EPS", col] if "Basic EPS" in qs.index else None
        ebitda_raw = qs.loc["EBITDA", col] if "EBITDA" in qs.index else None

        revenue_cr = _safe_float(revenue_raw)
        pat_cr = _safe_float(pat_raw)
        eps = _safe_float(eps_raw)
        ebitda_cr = _safe_float(ebitda_raw)

        if revenue_cr is not None:
            revenue_cr /= INR_TO_CR
        if pat_cr is not None:
            pat_cr /= INR_TO_CR
        if ebitda_cr is not None:
            ebitda_cr /= INR_TO_CR

        if revenue_cr is None and pat_cr is None:
            logger.debug("[%s] skipping %s — no revenue or PAT", symbol, quarter_end)
            continue

        try:
            upsert_quarter(
                symbol=symbol,
                quarter_end=quarter_end,
                fiscal_quarter=fiscal_quarter,
                fiscal_year=fiscal_year,
                revenue_cr=revenue_cr,
                pat_cr=pat_cr,
                ebitda_cr=ebitda_cr,
                eps=eps,
                parse_status="YFINANCE",
                source="yfinance",
                session=session,
            )
            stored += 1
        except Exception as e:
            logger.error("[%s] upsert failed for %s: %s", symbol, quarter_end, e)

    return stored


def load_nifty200_symbols() -> List[str]:
    """Load Nifty 200 symbols from data file."""
    path = Path("data/nifty200_symbols.json")
    if not path.exists():
        logger.error("data/nifty200_symbols.json not found")
        return POC_SYMBOLS
    with open(path) as f:
        data = json.load(f)
    symbols = data.get("symbols", [])
    # Filter out DUMMY placeholders
    return [s for s in symbols if not s.startswith("DUMMY")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Populate quarterly results DB")
    parser.add_argument("--full", action="store_true", help="Run on full Nifty 200")
    parser.add_argument("--symbols", type=str, help="Comma-separated symbol list")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between symbols")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    elif args.full:
        symbols = load_nifty200_symbols()
    else:
        symbols = POC_SYMBOLS

    logger.info("Populating quarterly results for %d symbols", len(symbols))

    create_quarterly_tables()

    session = SessionLocal()
    total_stored = 0
    success_count = 0
    fail_count = 0

    try:
        for i, symbol in enumerate(symbols, 1):
            logger.info("[%d/%d] Fetching %s...", i, len(symbols), symbol)
            n = fetch_and_store(symbol, session)
            if n > 0:
                success_count += 1
                total_stored += n
                logger.info("[%s] stored %d quarters", symbol, n)
            else:
                fail_count += 1

            if i < len(symbols):
                time.sleep(args.delay)
    finally:
        session.close()

    logger.info(
        "Done. %d symbols succeeded (%d quarters stored), %d failed.",
        success_count, total_stored, fail_count,
    )


if __name__ == "__main__":
    main()
