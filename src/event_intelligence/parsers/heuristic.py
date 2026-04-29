"""Heuristic ultimate-fallback parser.

Used when both XBRL and PDF parsers fail. Looks at the filing subject
line and category to make a coarse classification. The goal is graceful
degradation: even when we cannot read the document, we should still
emit a low-confidence record so downstream HYDRA can decide based on
price action alone.

Caps `urgency` at 5.0 — we never let a heuristic-classified event reach
DAWN's high-conviction R1 gate (≥ 8.5).
"""

from __future__ import annotations

import re
from typing import Any, Dict

from src.event_intelligence.parsers.base import ParsedDocument, ParseStatus


# Period markers (quarterly or annual) and the word "result/earnings". When
# both are present in the same subject — even with "Outcome of Board
# Meeting" prefix — the dominant intent is a results announcement, not a
# board-meeting intimation.
_PERIOD_MARKERS = re.compile(
    r"\b(quarter(ly)?|annual|standalone|consolidated|q[1-4]|fy\d{0,4}|year\s+ended)\b",
    re.IGNORECASE,
)
_RESULT_NOUNS = re.compile(
    r"\b(result|results|earnings|financial)\b",
    re.IGNORECASE,
)
_DIVIDEND_KEYWORDS = re.compile(r"\bdividend\b", re.IGNORECASE)
_GUIDANCE_KEYWORDS = re.compile(r"\b(guidance|outlook|forecast)\b", re.IGNORECASE)
_BOARD_MEETING_KEYWORDS = re.compile(
    r"\b(board\s+meeting|intimation\s+of\s+board)\b", re.IGNORECASE
)


def parse_heuristic(subject: str, category: str = "") -> ParsedDocument:
    """Return a degraded parse based on subject-line keywords."""
    text = f"{subject} {category}".strip()
    fields: Dict[str, Any] = {
        "event_type_guess": "OTHER",
        "subject": subject,
        "category": category,
    }

    has_results_intent = bool(
        _PERIOD_MARKERS.search(text) and _RESULT_NOUNS.search(text)
    )

    if has_results_intent:
        fields["event_type_guess"] = "FINANCIAL_RESULTS"
    elif _DIVIDEND_KEYWORDS.search(text):
        fields["event_type_guess"] = "DIVIDEND_ACTION"
    elif _GUIDANCE_KEYWORDS.search(text):
        fields["event_type_guess"] = "GUIDANCE_CHANGE"
    elif _BOARD_MEETING_KEYWORDS.search(text):
        fields["event_type_guess"] = "BOARD_MEETING"

    return ParsedDocument(
        status=ParseStatus.HEURISTIC,
        fields=fields,
        raw_text_excerpt=subject[:200],
    )
