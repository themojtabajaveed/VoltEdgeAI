"""PDF parser fallback for filings without XBRL.

Parse strategy (primary → fallback chain):
  1. pdfplumber table extraction — handles standard quarterly-result tables.
     Indian results use a 6-column layout:
     [standalone-current-Q | standalone-prior-Q | standalone-YTD-current |
      standalone-YTD-prior | consolidated-current-Q | consolidated-prior-Q]
     We identify column headers and prefer consolidated current-quarter values.
  2. pdfplumber full-text regex — fallback when table extraction finds nothing.
  3. pdfminer.six full-text regex — fallback when pdfplumber is unavailable.

Known limitations:
  - Scanned-image PDFs produce empty text → trigger heuristic fallback.
    OCR path (pytesseract) is a future enhancement.
  - Very large PDFs (>50 pages investor decks) may contain appendix tables
    that confuse column alignment — sanity bounds guard against this.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.event_intelligence.parsers.base import ParsedDocument, ParseStatus

logger = logging.getLogger(__name__)


# ── Sanity bounds for extracted numbers ──────────────────────────────────────
# Catches Gemini/parser hallucinations or misaligned columns.
_BOUNDS = {
    "revenue": (0.0, 5_000_000.0),    # 0 to ₹50 lakh crore
    "pat":     (-500_000.0, 500_000.0),
    "ebitda":  (-500_000.0, 500_000.0),
    "eps":     (-10_000.0, 10_000.0),
}

# ── Regex patterns for label-anchored text extraction ────────────────────────
_LINE_PATTERNS: Dict[str, List[re.Pattern]] = {
    "revenue": [
        re.compile(r"revenue\s+from\s+operations[^\n]*?([\d,]+\.?\d*)", re.IGNORECASE),
        re.compile(r"total\s+income[^\n]*?([\d,]+\.?\d*)", re.IGNORECASE),
    ],
    "pat": [
        re.compile(r"profit\s*(?:/\(loss\))?\s*after\s+tax[^\n]*?(\(?[\d,]+\.?\d*\)?)", re.IGNORECASE),
        re.compile(r"net\s+profit[^\n]*?(\(?[\d,]+\.?\d*\)?)", re.IGNORECASE),
    ],
    "ebitda": [
        re.compile(r"ebitda[^\n]*?(\(?[\d,]+\.?\d*\)?)", re.IGNORECASE),
    ],
    "eps": [
        re.compile(r"earnings\s+per\s+share[^\n]*?basic[^\n]*?(\(?-?[\d,]+\.?\d*\)?)", re.IGNORECASE),
        re.compile(r"basic\s+eps[^\n]*?(\(?-?[\d,]+\.?\d*\)?)", re.IGNORECASE),
    ],
}

# Column header keywords for identifying current-quarter consolidated columns
_CONSOL_HEADERS = re.compile(r"consolid", re.IGNORECASE)
_CURRENT_Q_HEADERS = re.compile(r"current\s+quarter|three\s+months|quarter\s+ended", re.IGNORECASE)
_RESULT_ROW_LABELS: Dict[str, List[str]] = {
    "revenue": ["revenue from operations", "total income", "net revenue"],
    "pat": ["profit after tax", "profit / (loss) after tax", "net profit", "profit for the period"],
    "ebitda": ["ebitda", "operating profit"],
    "eps": ["basic eps", "earnings per share", "basic earnings per share"],
}


def _parse_indian_number(s: str) -> Optional[float]:
    """Handle '1,234.56' and '(1,234.56)' (parens = negative)."""
    if not s:
        return None
    s = s.strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return None


def _within_bounds(metric: str, value: Optional[float]) -> bool:
    """Sanity-check extracted value against known bounds."""
    if value is None:
        return True
    lo, hi = _BOUNDS.get(metric, (-1e12, 1e12))
    return lo <= value <= hi


def _label_matches(cell: str, labels: List[str]) -> bool:
    c = cell.strip().lower()
    return any(lbl in c for lbl in labels)


def _pick_best_column(headers: List[str], rows: List[List[Any]], row_labels: List[str]) -> Optional[float]:
    """Identify the current-quarter column from headers and extract value.

    Column priority:
      1. Consolidated current-quarter column
      2. Standalone current-quarter column
      3. First numeric column found in the matching row

    Row matching uses label keywords.
    """
    if not headers or not rows:
        return None

    # Find column indices for: consolidated + current quarter
    consol_cols = [i for i, h in enumerate(headers) if _CONSOL_HEADERS.search(str(h))]
    current_q_cols = [i for i, h in enumerate(headers) if _CURRENT_Q_HEADERS.search(str(h))]
    preferred_cols = [c for c in consol_cols if c in current_q_cols]
    if not preferred_cols:
        preferred_cols = current_q_cols or consol_cols

    for row in rows:
        if not row:
            continue
        label = str(row[0]) if row else ""
        if not _label_matches(label, row_labels):
            continue

        # Try preferred columns first, then scan all non-label columns
        cols_to_try = preferred_cols if preferred_cols else list(range(1, len(row)))
        for col_idx in cols_to_try:
            if col_idx >= len(row):
                continue
            val = _parse_indian_number(str(row[col_idx]))
            if val is not None:
                return val

        # Fallback: first parseable number in the row
        for cell in row[1:]:
            val = _parse_indian_number(str(cell))
            if val is not None:
                return val

    return None


def _extract_via_tables(content: bytes) -> Dict[str, Optional[float]]:
    """Primary extraction: use pdfplumber table extraction."""
    results: Dict[str, Optional[float]] = {}
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                if not tables:
                    continue
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    # First row is likely headers
                    headers = [str(c or "").strip() for c in table[0]]
                    data_rows = table[1:]
                    for metric, labels in _RESULT_ROW_LABELS.items():
                        if metric in results:
                            continue
                        val = _pick_best_column(headers, data_rows, labels)
                        if val is not None and _within_bounds(metric, val):
                            results[metric] = val
    except ImportError:
        pass
    except Exception as e:
        logger.debug("[PDFParser] table extraction failed: %s", e)
    return results


def _extract_text(content: bytes) -> str:
    """Try pdfplumber text, then pdfminer. Returns empty string on total failure."""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except ImportError:
        pass
    except Exception as e:
        logger.debug("[PDFParser] pdfplumber text failed: %s — trying pdfminer", e)

    try:
        from pdfminer.high_level import extract_text as miner_extract  # type: ignore
        return miner_extract(io.BytesIO(content)) or ""
    except ImportError:
        logger.error("[PDFParser] neither pdfplumber nor pdfminer.six installed")
    except Exception as e:
        logger.debug("[PDFParser] pdfminer failed: %s", e)
    return ""


def _extract_via_regex(text: str) -> Dict[str, Optional[float]]:
    """Fallback extraction: regex patterns on full document text."""
    results: Dict[str, Optional[float]] = {}
    for metric, patterns in _LINE_PATTERNS.items():
        for pat in patterns:
            m = pat.search(text)
            if m:
                val = _parse_indian_number(m.group(1))
                if val is not None and _within_bounds(metric, val):
                    results[metric] = val
                    break
    return results


def parse_pdf(content: bytes) -> ParsedDocument:
    """Extract headline metrics from a PDF filing. Never raises.

    Tries table extraction first (better for structured results PDFs),
    then falls back to regex on full text.
    """
    if not content:
        return ParsedDocument(status=ParseStatus.FAILED, error="empty content")

    # ── Primary: table extraction ──────────────────────────────────────
    table_fields = _extract_via_tables(content)
    found_in_tables = sum(1 for v in table_fields.values() if v is not None)

    if found_in_tables >= 2:
        logger.debug("[PDFParser] table extraction succeeded: %d metrics", found_in_tables)
        return ParsedDocument(
            status=ParseStatus.PDF_TABLE,
            fields=table_fields,
        )

    # ── Fallback: full-text regex ──────────────────────────────────────
    text = _extract_text(content)
    if not text.strip():
        return ParsedDocument(
            status=ParseStatus.FAILED,
            error="empty text extraction (likely scanned image)",
        )

    regex_fields = _extract_via_regex(text)

    # Merge: table results have priority over regex for same metric
    merged: Dict[str, Any] = {}
    for metric in _RESULT_ROW_LABELS:
        merged[metric] = table_fields.get(metric) or regex_fields.get(metric)

    found_count = sum(1 for v in merged.values() if v is not None)
    excerpt = text[:500]

    if found_count == 0:
        return ParsedDocument(
            status=ParseStatus.FAILED,
            fields=merged,
            raw_text_excerpt=excerpt,
            error="no metric patterns matched",
        )

    status = ParseStatus.PDF_REGEX if found_count >= 2 else ParseStatus.PARTIAL
    return ParsedDocument(status=status, fields=merged, raw_text_excerpt=excerpt)
