"""Document parsers for NSE/BSE corporate-action filings.

XBRL primary, PDF fallback, heuristic ultimate fallback. Each parser
returns a `ParsedDocument` and never raises — the pipeline must never
halt on a single bad filing.
"""

from src.event_intelligence.parsers.base import ParsedDocument, ParseStatus

__all__ = ["ParsedDocument", "ParseStatus"]
