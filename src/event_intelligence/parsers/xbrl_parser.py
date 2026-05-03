"""XBRL parser for NSE/BSE corporate-result filings.

NSE/BSE publish quarterly and annual results as XBRL with tags from the
ICAI / Ind-AS taxonomies. This parser extracts headline metrics (revenue,
PAT, EBITDA, EPS) using context-period disambiguation to prefer the
"current single quarter" over YTD or prior-period values.

Context parsing logic:
  - Every value element references a context via contextRef attribute
  - Context elements define period: <period><startDate>/<endDate></period>
    or <period><instant/></period>
  - We compute the period duration and prefer ~90-day windows (quarterly)
    over ~365-day windows (annual/YTD)
  - Falls back to max-absolute-value heuristic when contexts cannot be
    resolved (e.g. malformed or missing context elements)
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from src.event_intelligence.parsers.base import ParsedDocument, ParseStatus

logger = logging.getLogger(__name__)


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


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y%m%d"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_contexts(root: ET.Element) -> Dict[str, int]:
    """Extract context_id -> period_days mapping.

    Period_days:
      - ~90 for single quarter (startDate + ~90 days)
      - ~180 for H1/H2
      - ~270 for 9-month YTD
      - ~365 for annual / full-year
      - 0 for instant contexts (balance-sheet values)

    Returns empty dict if context parsing fails.
    """
    contexts: Dict[str, int] = {}
    for elem in root.iter():
        local = _local_name(elem.tag)
        if local.lower() != "context":
            continue
        ctx_id = elem.get("id")
        if not ctx_id:
            continue
        period_elem = None
        for child in elem:
            if _local_name(child.tag).lower() == "period":
                period_elem = child
                break
        if period_elem is None:
            contexts[ctx_id] = 0
            continue

        start_date = end_date = None
        for child in period_elem:
            lname = _local_name(child.tag).lower()
            if lname == "startdate":
                start_date = _parse_date(child.text)
            elif lname == "enddate":
                end_date = _parse_date(child.text)
            elif lname == "instant":
                contexts[ctx_id] = 0
                break
        else:
            if start_date and end_date:
                contexts[ctx_id] = (end_date - start_date).days
            else:
                contexts[ctx_id] = -1  # unknown

    return contexts


def _is_quarterly_context(days: int) -> bool:
    """True if the period looks like a single quarter (~60-110 days)."""
    return 60 <= days <= 110


def _is_half_year_context(days: int) -> bool:
    return 150 <= days <= 200


def _scan_xbrl(
    root: ET.Element,
    contexts: Dict[str, int],
) -> Dict[str, List[Tuple[float, int]]]:
    """Walk the tree and collect (value, period_days) per metric.

    Returns dict metric → [(value, period_days), ...].
    """
    candidates: Dict[str, List[Tuple[float, int]]] = {k: [] for k in _METRIC_TAGS}
    for elem in root.iter():
        local = _local_name(elem.tag)
        local_lower = local.lower()
        for metric, tag_list in _METRIC_TAGS.items():
            for tag in tag_list:
                if local_lower == tag.lower():
                    val = _to_float(elem.text)
                    if val is not None:
                        ctx_id = elem.get("contextRef", "")
                        days = contexts.get(ctx_id, -1)
                        candidates[metric].append((val, days))
                    break
    return candidates


def _pick_best(candidates: List[Tuple[float, int]]) -> Optional[float]:
    """Select the best value using period-context priority.

    Priority order:
      1. Single-quarter values (~90 days) — most specific and what we want
      2. Half-year values (~180 days) — second choice
      3. Unknown-period values (days = -1)
      4. Instant/balance-sheet values (days = 0)
      5. Annual/YTD values (days >= 180) — last resort, largest absolute value

    Within each tier, prefer largest absolute value.
    """
    if not candidates:
        return None

    quarterly = [(v, d) for v, d in candidates if _is_quarterly_context(d)]
    if quarterly:
        return max((v for v, _ in quarterly), key=abs)

    half_year = [(v, d) for v, d in candidates if _is_half_year_context(d)]
    if half_year:
        return max((v for v, _ in half_year), key=abs)

    unknown = [(v, d) for v, d in candidates if d == -1]
    if unknown:
        return max((v for v, _ in unknown), key=abs)

    # Fallback: use largest absolute value across all (preserves old behavior)
    return max((v for v, _ in candidates), key=abs)


def parse_xbrl(content: bytes) -> ParsedDocument:
    """Parse an XBRL document. Never raises."""
    if not content:
        return ParsedDocument(status=ParseStatus.FAILED, error="empty content")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.warning("[XBRLParser] parse error: %s", e)
        return ParsedDocument(status=ParseStatus.FAILED, error=f"xml parse error: {e}")

    contexts = _parse_contexts(root)
    raw_candidates = _scan_xbrl(root, contexts)

    fields: Dict[str, Any] = {}
    found_count = 0
    for metric, candidates in raw_candidates.items():
        picked = _pick_best(candidates)
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
