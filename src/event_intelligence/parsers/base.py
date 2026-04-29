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
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    XBRL_FALLBACK_PDF = "XBRL_FALLBACK_PDF"
    HEURISTIC = "HEURISTIC"
    FAILED = "FAILED"


@dataclass
class ParsedDocument:
    status: ParseStatus
    fields: Dict[str, Any] = field(default_factory=dict)
    raw_text_excerpt: str = ""
    error: Optional[str] = None
