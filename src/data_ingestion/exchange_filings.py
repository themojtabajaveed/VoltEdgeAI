"""
exchange_filings.py — NSE/BSE Corporate Announcement Ingestion
--------------------------------------------------------------
Fetches exchange filings (corporate announcements) from the last N hours.

Primary source:   NSE corporate-announcements API (requires session cookie)
Secondary source: BSE AnnSubCategoryGetData API (no auth required)

This module caught RAILTEL's ₹608 crore order win filed on April 14 (holiday),
which generated a clean 13.9% return on April 15.

Usage:
    from src.data_ingestion.exchange_filings import fetch_exchange_filings
    filings = fetch_exchange_filings(hours_back=24)
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import requests
import zoneinfo

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# ── NSE session setup (same pattern as nse_scraper.py) ───────────────────────

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

_nse_session: Optional[requests.Session] = None


def _get_nse_session() -> requests.Session:
    """Return a requests.Session with valid NSE cookies (lazy init)."""
    global _nse_session
    if _nse_session is None:
        _nse_session = requests.Session()
        _nse_session.headers.update(_NSE_HEADERS)
        try:
            _nse_session.get("https://www.nseindia.com", timeout=10)
            logger.debug("[ExchFilings] NSE session initialised")
        except requests.RequestException as e:
            logger.warning(f"[ExchFilings] NSE session init failed (will retry on first call): {e}")
    return _nse_session


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FilingEvent:
    """A single corporate announcement from NSE or BSE."""
    symbol: str
    company_name: str
    headline: str         # announcement subject
    category: str         # e.g. "Order Win", "Board Meeting", "Results"
    filed_at: str         # ISO 8601 timestamp (IST)
    exchange: str         # "NSE" or "BSE"
    urgency: float        # 0-10 computed from category keywords
    # 4-layer event-evaluation enrichment — populated at fetch time where
    # possible; downstream callers fill remaining fields from cache/LTP.
    deal_size_inr: Optional[float] = None          # parsed from headline via regex
    price_at_filing_time: Optional[float] = None   # filled by enrich_filing()
    market_cap_inr: Optional[float] = None         # filled by enrich_filing()
    earnings_surprise_pct: Optional[float] = None  # reserved for future parser
    raw: dict = field(default_factory=dict, repr=False)


# ── Deal-size parser ──────────────────────────────────────────────────────────
# Used by Layer-3 event-quality scoring. Handles common Indian/USD formats in
# NSE/BSE subject lines: "₹500 crore", "Rs. 200 Cr", "INR 1500 Mn",
# "USD 50 million", "order worth 300 crores". USD conversion uses an
# approximate spot rate (good enough for scoring, not for trading).

_USD_INR_APPROX = 83.0  # circa 2026; scoring-only precision

_DEAL_SIZE_RE = re.compile(
    r"(?P<curr>₹|Rs\.?|INR|USD|US\$|\$)?"
    r"\s*"
    r"(?P<amt>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*"
    r"(?P<unit>crores?|billion|million|lakhs?|mln|cr|mn|bn)\b",
    re.IGNORECASE,
)

_UNIT_MULTIPLIERS = {
    "crore": 1e7, "crores": 1e7, "cr": 1e7,
    "lakh": 1e5, "lakhs": 1e5,
    "million": 1e6, "mn": 1e6, "mln": 1e6,
    "billion": 1e9, "bn": 1e9,
}


def _parse_deal_size_inr(text: str) -> Optional[float]:
    """Extract a deal/order size in INR from a subject/headline string.

    Returns the INR value, or None if no amount pattern is present. USD
    amounts are converted at _USD_INR_APPROX. Intentionally conservative:
    if multiple amounts appear, only the first is returned (subjects rarely
    have more than one meaningful number anyway).
    """
    if not text:
        return None
    m = _DEAL_SIZE_RE.search(text)
    if not m:
        return None
    try:
        amount = float(m.group("amt").replace(",", ""))
    except ValueError:
        return None
    unit = m.group("unit").lower()
    mult = _UNIT_MULTIPLIERS.get(unit)
    if mult is None:
        return None
    value_inr = amount * mult
    # Detect USD from either the captured currency group or a 10-char prefix
    # (covers "deal worth USD 50 million" where USD sits just before the amt).
    curr = (m.group("curr") or "").lower()
    prefix = text[max(0, m.start() - 10):m.start()].lower()
    if "usd" in curr or "$" in curr or "usd" in prefix or "us$" in prefix:
        value_inr *= _USD_INR_APPROX
    return value_inr


def enrich_filing(
    filing: FilingEvent,
    previous_close: Optional[float] = None,
    market_cap_inr: Optional[float] = None,
) -> FilingEvent:
    """Attach pre-market context (previous close, market cap) to a FilingEvent.

    Called by the pre-market pipeline after the premarket cache is available.
    Mutates and returns the same object for fluent use. Deal size is already
    set at fetch time; this fills the two fields that require per-symbol
    lookup data the filing fetchers don't have.
    """
    if previous_close is not None and filing.price_at_filing_time is None:
        filing.price_at_filing_time = float(previous_close)
    if market_cap_inr is not None and filing.market_cap_inr is None:
        filing.market_cap_inr = float(market_cap_inr)
    return filing


# ── Urgency scoring ───────────────────────────────────────────────────────────

def _score_urgency(category: str, subject: str) -> float:
    """
    Derive urgency (0-10) from the filing category and subject line.

    Keywords checked against lowercase(category + " " + subject).
    """
    blob = (category + " " + subject).lower()

    if any(k in blob for k in ("order", "contract", "work order", "order win")):
        return 9.0
    if any(k in blob for k in ("acquisition", "merger", "takeover", "amalgamation")):
        return 9.0
    if any(k in blob for k in ("result", "earnings", "financial result", "quarterly")):
        return 8.0
    if "dividend" in blob:
        return 6.0
    if any(k in blob for k in ("board meeting", "board of directors")):
        return 5.0
    if "press release" in blob:
        return 5.0
    if any(k in blob for k in ("basmati", "update", "general", "clarification", "note")):
        return 2.0
    return 4.0


def _normalise_filing_category(category: str, subject: str) -> Optional[str]:
    """
    Map a raw NSE filing category (smIndustry / announceType) and
    subject line to one of the DAWN_CATEGORIES catalyst strings.
    Returns None if no confident match — never guesses.
    """
    text = f"{category} {subject}".upper()

    # ORDER_WIN — contracts, orders, LOAs, work orders
    if any(k in text for k in [
        "ORDER", "CONTRACT", "LOA", "WORK ORDER", "PURCHASE ORDER",
        "LETTER OF AWARD", "AWARDED", "BAGGED", "SECURED CONTRACT",
        "SUPPLY AGREEMENT", "EMPANELLED",
    ]):
        return "ORDER_WIN"

    # MERGER / ACQUISITION
    if any(k in text for k in [
        "MERGER", "AMALGAMATION", "ACQUISITION", "TAKEOVER",
        "SCHEME OF ARRANGEMENT", "DEMERGER", "BUYOUT",
    ]):
        return "MERGER"

    # RESULTS_BLOWOUT — earnings surprises
    if any(k in text for k in [
        "FINANCIAL RESULT", "QUARTERLY RESULT", "ANNUAL RESULT",
        "Q1 RESULT", "Q2 RESULT", "Q3 RESULT", "Q4 RESULT",
        "STANDALONE RESULT", "CONSOLIDATED RESULT",
        "PAT GROWTH", "REVENUE GROWTH", "PROFIT",
    ]):
        return "RESULTS_BLOWOUT"

    # REGULATORY_CLEARANCE
    if any(k in text for k in [
        "APPROVAL", "CLEARANCE", "REGULATORY", "SEBI", "RBI",
        "NCLT", "CCI", "NCLAT", "LICENCE", "LICENSE",
        "FDA", "USFDA", "ANDA", "NDA", "CDSCO",
    ]):
        return "REGULATORY_CLEARANCE"

    # INDEX_ADDITION
    if any(k in text for k in [
        "INDEX", "NIFTY", "SENSEX", "INCLUSION", "ADDITION TO INDEX",
        "ADDED TO", "MSCI",
    ]):
        return "INDEX_ADDITION"

    # MAJOR_CONTRACT — large deals, MoUs, JVs
    if any(k in text for k in [
        "MOU", "MOA", "JOINT VENTURE", "JV", "PARTNERSHIP",
        "STRATEGIC ALLIANCE", "MEMORANDUM", "DEFINITIVE AGREEMENT",
    ]):
        return "MAJOR_CONTRACT"

    # EARNINGS_SURPRISE — analyst upgrades, guidance raises
    if any(k in text for k in [
        "UPGRADE", "GUIDANCE", "OUTLOOK", "REVISED ESTIMATE",
        "ANALYST", "RATING UPGRADE",
    ]):
        return "EARNINGS_SURPRISE"

    return None   # no confident match — do not guess


# ── Timestamp helpers ─────────────────────────────────────────────────────────

_NSE_DT_FORMATS = [
    "%d-%b-%Y %H:%M:%S",   # "14-Apr-2024 16:30:00"
    "%d-%b-%Y %H:%M",      # "14-Apr-2024 16:30"
    "%Y-%m-%dT%H:%M:%S",   # ISO
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
]

_BSE_DT_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
]


def _parse_dt_ist(raw_str: str, formats: List[str]) -> Optional[datetime]:
    """
    Parse a datetime string from exchange APIs into a timezone-aware IST datetime.
    NSE/BSE publish times in IST; we attach IST zone rather than converting.
    """
    if not raw_str:
        return None
    s = raw_str.strip()
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=IST)
        except ValueError:
            continue
    # Last-ditch: strip subseconds and retry ISO
    s_trimmed = re.sub(r"\.\d+$", "", s)
    try:
        dt = datetime.fromisoformat(s_trimmed)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt
    except ValueError:
        pass
    logger.debug(f"[ExchFilings] Could not parse timestamp: {raw_str!r}")
    return None


# ── NSE fetcher ───────────────────────────────────────────────────────────────

def _fetch_nse_filings(cutoff: datetime) -> List[FilingEvent]:
    """
    Fetch corporate announcements from NSE API.
    Requires session cookie — _get_nse_session() handles acquisition.
    Returns [] on any error.
    """
    global _nse_session
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    raw_data: Optional[list] = None

    session = _get_nse_session()
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code == 401:
            # Session expired — reset and retry once
            logger.info("[ExchFilings] NSE session expired, resetting...")
            _nse_session = None
            session = _get_nse_session()
            resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            logger.warning(
                f"[ExchFilings] NSE announcements returned HTTP {resp.status_code}"
            )
            return []
        raw_data = resp.json()
    except requests.Timeout:
        logger.warning("[ExchFilings] NSE announcements request timed out (10s)")
        return []
    except requests.RequestException as e:
        logger.warning(f"[ExchFilings] NSE announcements fetch error: {e}")
        return []
    except ValueError as e:
        logger.warning(f"[ExchFilings] NSE response JSON parse failed: {e}")
        return []

    if not isinstance(raw_data, list):
        logger.warning(f"[ExchFilings] NSE returned unexpected type: {type(raw_data)}")
        return []

    filings: List[FilingEvent] = []
    for item in raw_data:
        if not isinstance(item, dict):
            continue

        symbol = (item.get("symbol") or item.get("sm_symbol") or "").strip().upper()
        company = (
            item.get("comp") or item.get("sm_name") or item.get("company_name") or ""
        ).strip()
        subject = (item.get("subject") or item.get("desc") or "").strip()
        category = (item.get("smIndustry") or item.get("announceType") or "").strip()

        # NSE timestamp fields: try multiple key names
        raw_ts = (
            item.get("exchdisstime")
            or item.get("bcastdttm")
            or item.get("sort_date")
            or ""
        )
        filed_dt = _parse_dt_ist(str(raw_ts), _NSE_DT_FORMATS)

        if not symbol or not subject:
            continue
        if filed_dt is None or filed_dt < cutoff:
            continue

        urgency = _score_urgency(category, subject)
        filings.append(FilingEvent(
            symbol=symbol,
            company_name=company,
            headline=subject,
            category=category,
            filed_at=filed_dt.isoformat(),
            exchange="NSE",
            urgency=urgency,
            deal_size_inr=_parse_deal_size_inr(subject),
            raw=item,
        ))

    logger.info(f"[ExchFilings] NSE: {len(filings)} filings in window from {len(raw_data)} total")
    return filings


# ── BSE fetcher ───────────────────────────────────────────────────────────────

def _fetch_bse_filings(cutoff: datetime) -> List[FilingEvent]:
    """
    Fetch corporate announcements from BSE API.
    Visits BSE homepage first to acquire session cookies (same pattern as NSE).
    Date window = cutoff date → today (IST).
    Returns [] on any error or if BSE is unreachable from this host.
    """
    now_ist = datetime.now(IST)
    from_date = cutoff.strftime("%Y%m%d")
    to_date = now_ist.strftime("%Y%m%d")

    url = (
        f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={from_date}"
        f"&strScrip=&strSearch=&strToDate={to_date}&strType=C&subcategory=-1"
    )
    bse_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.bseindia.com",
        "Referer": "https://www.bseindia.com/",
    }

    raw_data: Optional[dict] = None
    try:
        session = requests.Session()
        session.headers.update(bse_headers)
        # Acquire BSE session cookie
        try:
            session.get("https://www.bseindia.com", timeout=10)
        except requests.RequestException as e:
            logger.debug(f"[ExchFilings] BSE homepage pre-fetch failed: {e}")

        resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            logger.warning(
                f"[ExchFilings] BSE announcements returned HTTP {resp.status_code}"
            )
            return []
        payload = resp.text.strip()
        if not payload or payload == "{}":
            logger.warning(
                "[ExchFilings] BSE returned empty response — likely geo-restricted "
                "or no filings in window; NSE is the active source"
            )
            return []
        raw_data = resp.json()
    except requests.Timeout:
        logger.warning("[ExchFilings] BSE announcements request timed out (10s)")
        return []
    except requests.RequestException as e:
        logger.warning(f"[ExchFilings] BSE announcements fetch error: {e}")
        return []
    except ValueError as e:
        logger.warning(f"[ExchFilings] BSE response JSON parse failed: {e}")
        return []

    table = raw_data.get("Table") or raw_data.get("table") or []
    if not isinstance(table, list):
        logger.warning(f"[ExchFilings] BSE 'Table' key missing or wrong type")
        return []

    filings: List[FilingEvent] = []
    for item in table:
        if not isinstance(item, dict):
            continue

        # BSE may carry NSE symbol or only BSE code
        symbol = (
            item.get("NSESYMBOL") or item.get("NSE_Symbol") or item.get("SCRIP_CD") or ""
        ).strip().upper()
        company = (item.get("SHORT_NAME") or item.get("SLONGNAME") or "").strip()
        headline = (item.get("HEADLINE") or item.get("NEWSSUB") or "").strip()
        category = (item.get("CATEGORYNAME") or item.get("SUBCATNAME") or "").strip()

        raw_ts = (
            item.get("NEWS_DT") or item.get("ANNOUNCEMENTDATE") or item.get("DT_TM") or ""
        )
        filed_dt = _parse_dt_ist(str(raw_ts), _BSE_DT_FORMATS)

        if not symbol or not headline:
            continue
        if filed_dt is None or filed_dt < cutoff:
            continue

        urgency = _score_urgency(category, headline)
        filings.append(FilingEvent(
            symbol=symbol,
            company_name=company,
            headline=headline,
            category=category,
            filed_at=filed_dt.isoformat(),
            exchange="BSE",
            urgency=urgency,
            deal_size_inr=_parse_deal_size_inr(headline),
            raw=item,
        ))

    logger.info(f"[ExchFilings] BSE: {len(filings)} filings in window from {len(table)} total")
    return filings


# ── Deduplication ─────────────────────────────────────────────────────────────

_DEDUP_NOISE_WORDS = re.compile(
    r"\b(CORRIGENDUM|CORRIGENDA|ADDENDUM|AMENDMENT|REVISED|REVISION|UPDATE)\b"
)
_DEDUP_PUNCT = re.compile(r"[^\w\s]")
_DEDUP_WS = re.compile(r"\s+")


def _normalise_dedup_subject(subject: str) -> str:
    """Normalise a filing subject for second-pass amendment dedup."""
    text = subject.upper()
    text = _DEDUP_NOISE_WORDS.sub(" ", text)
    text = _DEDUP_PUNCT.sub(" ", text)
    text = _DEDUP_WS.sub(" ", text).strip()
    return text[:60]


def _dedup_filings(filings: List[FilingEvent]) -> List[FilingEvent]:
    """
    Remove duplicate filings across NSE and BSE.

    Pass 1: same symbol + headline prefix (first 50 chars, lowercase).
            Prefers NSE over BSE when duplicate detected.
    Pass 2: same symbol + normalised subject (noise words stripped, first 60 chars).
            Handles corrigenda/addenda with slightly different headlines.
            Keeps the filing with the latest filed_at; prefers timestamped over
            untimstamped when one side has no timestamp.
    """
    # ── Pass 1: exchange-level dedup (existing logic, unchanged) ─────────────
    seen: dict[str, FilingEvent] = {}
    for f in filings:
        key = f"{f.symbol}|{f.headline[:50].lower().strip()}"
        if key not in seen:
            seen[key] = f
        elif f.exchange == "NSE" and seen[key].exchange == "BSE":
            seen[key] = f
    pass1 = list(seen.values())

    # ── Pass 2: amendment dedup (corrigenda / addenda with differing headlines) ─
    buckets: dict[tuple[str, str], FilingEvent] = {}
    for f in pass1:
        norm_key = (
            f.symbol.upper().strip(),
            _normalise_dedup_subject(f.headline or ""),
        )
        if norm_key not in buckets:
            buckets[norm_key] = f
        else:
            existing = buckets[norm_key]
            # Prefer the filing with the latest filed_at; prefer timestamped over none
            f_ts = f.filed_at or ""
            ex_ts = existing.filed_at or ""
            if f_ts and (not ex_ts or f_ts > ex_ts):
                buckets[norm_key] = f
    return list(buckets.values())


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_exchange_filings(hours_back: int = 24) -> List[FilingEvent]:
    """
    Fetch NSE + BSE filings from the last `hours_back` hours.

    Behaviour:
    - Fetches NSE first, BSE second (parallel ordering not needed — both fast)
    - Deduplicates by symbol + headline prefix
    - Filters to filings within the time window
    - Returns sorted: urgency desc, then filed_at desc
    - Never raises — returns [] on complete failure

    Args:
        hours_back: How far back to look (default 24 — catches holiday filings)

    Returns:
        List[FilingEvent] sorted by (urgency desc, filed_at desc)
    """
    now_ist = datetime.now(IST)
    cutoff = now_ist - timedelta(hours=hours_back)
    logger.info(
        f"[ExchFilings] Fetching filings since {cutoff.strftime('%Y-%m-%d %H:%M')} IST "
        f"({hours_back}h window)"
    )

    nse_filings: List[FilingEvent] = []
    bse_filings: List[FilingEvent] = []

    try:
        nse_filings = _fetch_nse_filings(cutoff)
    except Exception as e:
        logger.error(f"[ExchFilings] NSE fetch raised unexpected exception: {e}")

    try:
        bse_filings = _fetch_bse_filings(cutoff)
    except Exception as e:
        logger.error(f"[ExchFilings] BSE fetch raised unexpected exception: {e}")

    combined = _dedup_filings(nse_filings + bse_filings)
    combined.sort(key=lambda f: (-f.urgency, f.filed_at), reverse=False)
    # Sort: primary urgency desc, secondary filed_at desc
    combined.sort(key=lambda f: (-f.urgency, -(
        datetime.fromisoformat(f.filed_at).timestamp()
        if f.filed_at else 0
    )))

    logger.info(
        f"[ExchFilings] Done: {len(nse_filings)} NSE + {len(bse_filings)} BSE "
        f"→ {len(combined)} after dedup"
    )
    return combined
