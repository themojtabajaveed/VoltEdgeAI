"""Document fetcher: download XBRL/PDF attachments from NSE/BSE.

Uses the same cookie-based NSE session as exchange_filings.py to access
nsearchives.nseindia.com. BSE attachments are public and don't need cookies.

Supports:
  - NSE session cookie propagation (required for nsearchives downloads)
  - HTTP/SOCKS proxy routing (for geo-blocked GCP instances)
  - Retry with configurable backoff delays
  - Local file caching to avoid re-downloads

Returns raw bytes. Never raises — on any failure, returns None so the
parser layer can degrade to heuristic.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_nse_session: Optional[requests.Session] = None
_nse_session_created_at: float = 0.0
_SESSION_TTL_S = 5400.0  # refresh cookies every 90 minutes


def _get_nse_session(proxy_url: str = "") -> requests.Session:
    """Return a requests.Session with valid NSE cookies. Refreshes every 90min."""
    global _nse_session, _nse_session_created_at

    now = time.time()
    if _nse_session is not None and (now - _nse_session_created_at) < _SESSION_TTL_S:
        return _nse_session

    sess = requests.Session()
    sess.headers.update(_NSE_HEADERS)
    if proxy_url:
        sess.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        sess.get("https://www.nseindia.com", timeout=10)
        logger.info("[DocFetcher] NSE session initialised (proxy=%s)", bool(proxy_url))
    except requests.RequestException as e:
        logger.warning("[DocFetcher] NSE session init failed: %s", e)

    _nse_session = sess
    _nse_session_created_at = now
    return sess


def _is_nse_url(url: str) -> bool:
    return "nseindia.com" in url or "nsearchives" in url


def _cache_path(url: str, cache_dir: str) -> Path:
    """Deterministic cache path: cache_dir/filings/<sha256_prefix>.<ext>"""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    ext = "xbrl" if "xbrl" in url.lower() else "pdf"
    base = Path(cache_dir) / "cache" / "filings"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{url_hash}.{ext}"


def _read_cache(url: str, cache_dir: str) -> Optional[bytes]:
    """Return cached document bytes if available."""
    path = _cache_path(url, cache_dir)
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    return None


def _write_cache(url: str, cache_dir: str, content: bytes) -> None:
    """Persist downloaded document to local cache."""
    path = _cache_path(url, cache_dir)
    try:
        path.write_bytes(content)
    except OSError as e:
        logger.debug("[DocFetcher] cache write failed: %s", e)


def _attempt_download(
    url: str,
    session: requests.Session,
    timeout: float,
) -> Tuple[Optional[bytes], str]:
    """Single download attempt. Returns (content, status_description)."""
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and resp.content and len(resp.content) > 100:
            return resp.content, "ok"
        if resp.status_code == 403:
            return None, "forbidden"
        if resp.status_code == 404:
            return None, "not_found"
        return None, f"status_{resp.status_code}"
    except requests.Timeout:
        return None, "timeout"
    except requests.RequestException as e:
        return None, f"error:{type(e).__name__}"


def fetch_attachment(
    url: str,
    proxy_url: str = "",
    timeout: float = 20.0,
    max_retries: int = 3,
    retry_delays: tuple = (30.0, 60.0, 120.0),
    cache_dir: str = "data",
) -> Optional[bytes]:
    """Download a document with retry and caching. Returns raw bytes or None."""
    if not url:
        return None

    cached = _read_cache(url, cache_dir)
    if cached:
        logger.debug("[DocFetcher] cache hit url=%s", url[:80])
        return cached

    if _is_nse_url(url):
        session = _get_nse_session(proxy_url)
    else:
        session = requests.Session()
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}

    content, status = _attempt_download(url, session, timeout)
    if content:
        _write_cache(url, cache_dir, content)
        return content

    if status == "not_found":
        logger.info("[DocFetcher] 404 url=%s (attachment may not be uploaded yet)", url[:80])
    else:
        logger.warning("[DocFetcher] attempt 1 failed status=%s url=%s", status, url[:80])

    for attempt_idx in range(min(max_retries - 1, len(retry_delays))):
        delay = retry_delays[attempt_idx] if attempt_idx < len(retry_delays) else retry_delays[-1]
        time.sleep(delay)

        if _is_nse_url(url):
            session = _get_nse_session(proxy_url)

        content, status = _attempt_download(url, session, timeout)
        if content:
            logger.info(
                "[DocFetcher] retry %d succeeded url=%s", attempt_idx + 2, url[:80]
            )
            _write_cache(url, cache_dir, content)
            return content

        logger.warning(
            "[DocFetcher] retry %d failed status=%s url=%s",
            attempt_idx + 2, status, url[:80],
        )

    logger.error(
        "[DocFetcher] all %d attempts exhausted url=%s — degrading to heuristic",
        max_retries, url[:80],
    )
    return None
