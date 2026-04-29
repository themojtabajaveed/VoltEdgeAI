"""Document fetcher: download XBRL/PDF attachments from NSE/BSE.

Reuses the NSE session pattern from `src.data_ingestion.exchange_filings`
(cookie-based, lazy session). BSE attachments are public; NSE requires
the same cookie dance the existing filings ingestion already handles.

Returns raw bytes. Never raises — on any failure, returns `None` so the
parser layer can degrade to heuristic.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 15.0


def fetch_attachment(url: str, session: Optional[requests.Session] = None) -> Optional[bytes]:
    """Download a document. Returns the raw body, or None on failure."""
    if not url:
        return None
    sess = session or requests.Session()
    try:
        resp = sess.get(url, timeout=_DEFAULT_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200 and resp.content:
            return resp.content
        logger.warning(
            "[DocFetcher] non-200 status=%s url=%s", resp.status_code, url
        )
        return None
    except requests.RequestException as e:
        logger.warning("[DocFetcher] request failed url=%s: %s", url, e)
        return None
