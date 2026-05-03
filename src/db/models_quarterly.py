"""SQLAlchemy model and CRUD helpers for historical quarterly results.

Used by:
  - src/event_intelligence/consensus.py — self-consensus model reads prior 8 quarters
  - scripts/populate_quarterly_results.py — bulk populate from NSE XBRL archive
  - src/event_intelligence/classifier.py — auto-inserts each new financial result

Design notes:
  - (symbol, quarter_end, source) is the unique key — the same quarter can have
    rows from multiple sources (NSE XBRL + BSE XBRL + manual). `get_prior_quarters`
    dedupes by source priority so callers always get one row per quarter.
  - All monetary values in Indian Crores (₹ Cr). This avoids unit ambiguity across
    PDFs that sometimes report in Lakhs and sometimes in Crores.
  - fiscal_quarter (1-4) and fiscal_year are stored explicitly to support the
    seasonality component of the self-consensus model without date arithmetic.
  - YoY % fields are computed at insert time from same fiscal_quarter prior year;
    they are nullable and may be missing for the first year of data.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from sqlalchemy import (
    Column, Date, DateTime, Float, Integer, String, UniqueConstraint, func,
)
from sqlalchemy.orm import Session

from src.db import Base, SessionLocal, engine

logger = logging.getLogger(__name__)


class QuarterlyResult(Base):
    __tablename__ = "quarterly_results"
    __table_args__ = (
        UniqueConstraint("symbol", "quarter_end", "source", name="uq_symbol_quarter_source"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    quarter_end = Column(Date, nullable=False, index=True)   # e.g. 2026-03-31
    fiscal_quarter = Column(Integer, nullable=False)          # 1 / 2 / 3 / 4
    fiscal_year = Column(Integer, nullable=False)             # Indian FY ending year, e.g. 2026

    # P&L metrics in ₹ Crores. All nullable — not every source provides all.
    revenue_cr = Column(Float, nullable=True)
    pat_cr = Column(Float, nullable=True)
    ebitda_cr = Column(Float, nullable=True)
    eps = Column(Float, nullable=True)

    # YoY deltas — computed at upsert time vs same quarter prior year
    revenue_yoy_pct = Column(Float, nullable=True)
    pat_yoy_pct = Column(Float, nullable=True)

    # Provenance
    parse_status = Column(String(30), nullable=False, default="UNKNOWN")
    source = Column(String(30), nullable=False)     # 'nse_xbrl' | 'bse_xbrl' | 'nse_pdf' | 'manual'
    filing_id = Column(String(100), nullable=True)  # NSE seq_id or BSE filing ID for audit

    scraped_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# Source priority for deduplication (lower index = more trusted)
# When the same quarter has rows from multiple sources, get_prior_quarters
# keeps the highest-priority (lowest index) source.
_SOURCE_PRIORITY = ("nse_xbrl", "bse_xbrl", "nse_pdf", "pipeline", "yfinance", "manual")


def create_quarterly_tables() -> None:
    """Create quarterly_results table if it does not exist. Safe to call repeatedly."""
    Base.metadata.create_all(bind=engine, tables=[QuarterlyResult.__table__])
    logger.info("[QuarterlyDB] table ensured")


def upsert_quarter(
    *,
    symbol: str,
    quarter_end: date,
    fiscal_quarter: int,
    fiscal_year: int,
    revenue_cr: Optional[float] = None,
    pat_cr: Optional[float] = None,
    ebitda_cr: Optional[float] = None,
    eps: Optional[float] = None,
    parse_status: str = "UNKNOWN",
    source: str = "manual",
    filing_id: Optional[str] = None,
    session: Optional[Session] = None,
) -> None:
    """Insert or update one quarter row. Computes YoY deltas automatically.

    Caller can pass an existing Session (for bulk operations); otherwise a
    short-lived session is created and closed.
    """
    _session = session or SessionLocal()
    _owns = session is None
    try:
        row = (
            _session.query(QuarterlyResult)
            .filter_by(symbol=symbol, quarter_end=quarter_end, source=source)
            .first()
        )
        if row is None:
            row = QuarterlyResult(
                symbol=symbol,
                quarter_end=quarter_end,
                source=source,
            )
            _session.add(row)

        row.fiscal_quarter = fiscal_quarter
        row.fiscal_year = fiscal_year
        row.parse_status = parse_status
        row.filing_id = filing_id

        if revenue_cr is not None:
            row.revenue_cr = revenue_cr
        if pat_cr is not None:
            row.pat_cr = pat_cr
        if ebitda_cr is not None:
            row.ebitda_cr = ebitda_cr
        if eps is not None:
            row.eps = eps

        # Auto-compute YoY vs same fiscal_quarter in prior fiscal year
        prior = _get_same_quarter_prior_year(
            _session, symbol, fiscal_quarter, fiscal_year - 1
        )
        if prior:
            if row.revenue_cr is not None and prior.revenue_cr:
                row.revenue_yoy_pct = (row.revenue_cr / prior.revenue_cr - 1.0) * 100.0
            if row.pat_cr is not None and prior.pat_cr:
                row.pat_yoy_pct = (row.pat_cr / prior.pat_cr - 1.0) * 100.0

        _session.commit()
    except Exception as e:
        _session.rollback()
        logger.error("[QuarterlyDB] upsert failed %s %s %s: %s", symbol, quarter_end, source, e)
        raise
    finally:
        if _owns:
            _session.close()


def get_prior_quarters(
    symbol: str,
    n: int = 8,
) -> List[dict]:
    """Return up to n most-recent quarters for symbol, one row per quarter (best source).

    Returns list sorted ascending (oldest first) so the self-consensus model can
    iterate index 0 = oldest, -1 = most recent without extra sorting.
    """
    session = SessionLocal()
    try:
        rows = (
            session.query(QuarterlyResult)
            .filter(QuarterlyResult.symbol == symbol)
            .order_by(QuarterlyResult.quarter_end.desc())
            .limit(n * len(_SOURCE_PRIORITY))
            .all()
        )
        # Deduplicate: keep highest-priority source per quarter_end
        best: dict = {}
        for row in rows:
            key = row.quarter_end
            if key not in best:
                best[key] = row
            else:
                if _prio(row.source) < _prio(best[key].source):
                    best[key] = row

        sorted_rows = sorted(best.values(), key=lambda r: r.quarter_end)
        recent = sorted_rows[-n:] if len(sorted_rows) > n else sorted_rows
        return [_to_dict(r) for r in recent]
    finally:
        session.close()


def _prio(source: str) -> int:
    try:
        return _SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(_SOURCE_PRIORITY)


def _get_same_quarter_prior_year(
    session: Session,
    symbol: str,
    fiscal_quarter: int,
    fiscal_year: int,
) -> Optional[QuarterlyResult]:
    return (
        session.query(QuarterlyResult)
        .filter_by(symbol=symbol, fiscal_quarter=fiscal_quarter, fiscal_year=fiscal_year)
        .order_by(QuarterlyResult.scraped_at.desc())
        .first()
    )


def _to_dict(row: QuarterlyResult) -> dict:
    return {
        "symbol": row.symbol,
        "quarter_end": row.quarter_end.isoformat() if row.quarter_end else None,
        "fiscal_quarter": row.fiscal_quarter,
        "fiscal_year": row.fiscal_year,
        "revenue_cr": row.revenue_cr,
        "pat_cr": row.pat_cr,
        "ebitda_cr": row.ebitda_cr,
        "eps": row.eps,
        "revenue_yoy_pct": row.revenue_yoy_pct,
        "pat_yoy_pct": row.pat_yoy_pct,
        "parse_status": row.parse_status,
        "source": row.source,
    }
