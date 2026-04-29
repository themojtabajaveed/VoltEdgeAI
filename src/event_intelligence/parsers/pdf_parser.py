"""PDF parser fallback for filings without XBRL.

Uses pdfplumber if available, falls back to pdfminer.six. Extracts text,
then regex-searches for headline financial line items in common Indian
quarterly-result formats (Revenue from Operations, Profit Before Tax,
Profit After Tax, EBITDA, EPS).

Known limitations: small-cap PDFs sometimes use scanned images — those
will produce empty text and trigger the heuristic fallback at the
classifier level. We do not OCR in v1; that's a v2 concern.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.event_intelligence.parsers.base import ParsedDocument, ParseStatus

logger = logging.getLogger(__name__)


_NUMBER_RE = re.compile(r"\(?-?[\d,]+\.?\d*\)?")


# Each pattern is anchored to a label that appears near a numeric value.
# The captured number is post-processed to handle parenthesized negatives
# (Indian convention) and commas.
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


def _parse_indian_number(s: str) -> Optional[float]:
    """Handle '1,234.56' and '(1,234.56)' (parens = negative)."""
    if not s:
        return None
    s = s.strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").strip()
    if not s:
        return None
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return None


def _extract_text(content: bytes) -> str:
    """Try pdfplumber first, then pdfminer. Returns empty string on total failure."""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except ImportError:
        pass
    except Exception as e:  # pdfplumber raises various; degrade.
        logger.warning("[PDFParser] pdfplumber failed: %s — trying pdfminer", e)

    try:
        from pdfminer.high_level import extract_text as miner_extract  # type: ignore
        return miner_extract(io.BytesIO(content)) or ""
    except ImportError:
        logger.error("[PDFParser] neither pdfplumber nor pdfminer.six installed")
    except Exception as e:
        logger.warning("[PDFParser] pdfminer failed: %s", e)
    return ""


def parse_pdf(content: bytes) -> ParsedDocument:
    """Extract headline metrics from a PDF filing. Never raises."""
    if not content:
        return ParsedDocument(status=ParseStatus.FAILED, error="empty content")

    text = _extract_text(content)
    if not text.strip():
        return ParsedDocument(
            status=ParseStatus.FAILED,
            error="empty text extraction (likely scanned image)",
        )

    fields: Dict[str, Any] = {}
    found_count = 0
    for metric, patterns in _LINE_PATTERNS.items():
        picked: Optional[float] = None
        for pat in patterns:
            m = pat.search(text)
            if m:
                picked = _parse_indian_number(m.group(1))
                if picked is not None:
                    break
        fields[metric] = picked
        if picked is not None:
            found_count += 1

    excerpt = text[:500] if text else ""
    if found_count == 0:
        return ParsedDocument(
            status=ParseStatus.FAILED,
            fields=fields,
            raw_text_excerpt=excerpt,
            error="no metric patterns matched",
        )

    status = ParseStatus.SUCCESS if found_count >= 2 else ParseStatus.PARTIAL
    return ParsedDocument(
        status=status, fields=fields, raw_text_excerpt=excerpt
    )
