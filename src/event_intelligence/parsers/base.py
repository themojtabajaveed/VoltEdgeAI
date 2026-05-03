"""Shared types for the parser stage.

Each parser produces a `ParsedDocument`. The classifier downstream looks
at `status` and `fields`. Parsers never raise — they return a degraded
`ParsedDocument(status=FAILED, ...)` instead so the pipeline keeps moving.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ParseStatus(str, Enum):
    # ── Current values — reflect the actual extraction source ──────────────
    PDF_TABLE  = "PDF_TABLE"   # table extraction from PDF (primary path)
    PDF_REGEX  = "PDF_REGEX"   # full-text regex on PDF (fallback)
    HEURISTIC  = "HEURISTIC"   # headline/subject-only; no document fetched
    FAILED     = "FAILED"
    # ── Deprecated — kept for backward-compat with JSON records pre-2026-05-03 ──
    # Do NOT emit these in new code. XBRL_FALLBACK_PDF is additionally
    # unreachable: the XBRL-primary parse path was removed; NSE /api/xbrl
    # returns announcement metadata only and is never a source of financials.
    SUCCESS          = "SUCCESS"           # was: any successful PDF extraction
    PARTIAL          = "PARTIAL"           # was: partial extraction (some fields)
    XBRL_FALLBACK_PDF = "XBRL_FALLBACK_PDF"  # unreachable since XBRL primary path removed


@dataclass
class ParsedDocument:
    status: ParseStatus
    fields: Dict[str, Any] = field(default_factory=dict)
    raw_text_excerpt: str = ""
    error: Optional[str] = None
