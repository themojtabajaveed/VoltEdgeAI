"""XBRL parser for NSE/BSE corporate-result filings.

NSE/BSE publish quarterly and annual results as XBRL with tags from the
ICAI / Ind-AS taxonomies. This parser extracts the headline metrics
(revenue, PAT, EBITDA, EPS) without the full XBRL semantics — we treat
the document as a flat tag-name → value lookup, which is sufficient for
the v1 use case.

A more rigorous XBRL implementation would parse contexts and resolve the
"latest period" tag relationships. For v1 we use a pragmatic
"highest-magnitude tag wins" heuristic combined with explicit known-good
tag names, which catches the common cases without dragging in heavy
XBRL libraries.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from src.event_intelligence.parsers.base import ParsedDocument, ParseStatus

logger = logging.getLogger(__name__)


# Known tag suffixes (after stripping namespace) for each metric. Order
# matters: earlier entries are preferred. Suffixes are matched
# case-insensitively against the local-name of the element.
_METRIC_TAGS: Dict[str, List[str]] = {
    "revenue": [
        "RevenueFromOperations",
        "Revenue",
        "TotalIncome",
    ],
    "pat": [
        "ProfitLossForPeriod",
        "ProfitLoss",
        "NetProfitLossForPeriod",
        "ProfitAfterTax",
    ],
    "ebitda": [
        "EBITDA",
        "EarningsBeforeInterestTaxesDepreciationAmortization",
    ],
    "eps": [
        "BasicEarningsLossPerShareFromContinuingOperations",
        "BasicEarningsPerShare",
        "EarningsPerShareBasic",
    ],
}


_LOCAL_NAME_RE = re.compile(r"\{[^}]*\}")


def _local_name(tag: str) -> str:
    return _LOCAL_NAME_RE.sub("", tag)


def _to_float(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _scan_xbrl(root: ET.Element) -> Dict[str, List[float]]:
    """Walk the tree and bucket numeric values by metric.

    Returns a dict metric → list of candidate values. The first non-None
    candidate is used (post-sort: largest absolute value wins, since
    XBRL filings typically include both quarterly and YTD figures and we
    want the larger one for absolute revenue/PAT — this is a pragmatic
    v1 heuristic, refine later if it misclassifies).
    """
    candidates: Dict[str, List[float]] = {k: [] for k in _METRIC_TAGS}
    for elem in root.iter():
        local = _local_name(elem.tag)
        local_lower = local.lower()
        for metric, tag_list in _METRIC_TAGS.items():
            for tag in tag_list:
                if local_lower == tag.lower():
                    val = _to_float(elem.text)
                    if val is not None:
                        candidates[metric].append(val)
                    break
    return candidates


def _pick_best(values: List[float]) -> Optional[float]:
    if not values:
        return None
    # Largest absolute value (handles both quarterly + YTD; YTD wins).
    return max(values, key=lambda v: abs(v))


def parse_xbrl(content: bytes) -> ParsedDocument:
    """Parse an XBRL document. Never raises."""
    if not content:
        return ParsedDocument(
            status=ParseStatus.FAILED, error="empty content"
        )
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.warning("[XBRLParser] parse error: %s", e)
        return ParsedDocument(
            status=ParseStatus.FAILED, error=f"xml parse error: {e}"
        )

    candidates = _scan_xbrl(root)
    fields: Dict[str, Any] = {}
    found_count = 0
    for metric, vals in candidates.items():
        picked = _pick_best(vals)
        fields[metric] = picked
        if picked is not None:
            found_count += 1

    if found_count == 0:
        return ParsedDocument(
            status=ParseStatus.FAILED,
            fields=fields,
            error="no recognized metric tags found",
        )

    status = ParseStatus.SUCCESS if found_count >= 2 else ParseStatus.PARTIAL
    return ParsedDocument(status=status, fields=fields)
