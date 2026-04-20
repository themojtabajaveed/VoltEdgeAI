"""
market_calendar.py — NSE Holiday Guard Utility
-----------------------------------------------
Determines whether the Indian equity market (NSE/BSE) is open on the current
IST date. Pure Python — no external API calls, no new dependencies.

Holiday coverage:
  - 2026 is hardcoded below (NSE_HOLIDAYS_2026).
  - Other years: loaded from data/nse_holidays_{year}.json (written once/year
    by scripts/fetch_nse_holidays.py). Absence of the file logs a WARNING and
    falls back to weekends-only (conservative: better to trade a holiday by
    accident than to miss a real trading day silently).
"""
import json
import logging
import os
import zoneinfo
from datetime import datetime, date, time as dt_time, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# NSE 2026 trading holidays (source: NSE India official holiday calendar)
NSE_HOLIDAYS_2026: dict[str, str] = {
    "2026-01-26": "Republic Day",
    "2026-02-18": "Mahashivratri",
    "2026-03-25": "Holi",
    "2026-04-02": "Ram Navami",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Ambedkar Jayanti / Baisakhi",
    "2026-04-30": "Buddha Purnima",
    "2026-06-19": "Bakri Id",
    "2026-07-16": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-08-27": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti / Dussehra",
    "2026-10-26": "Diwali Laxmi Puja",
    "2026-10-27": "Diwali Balipratipada",
    "2026-11-25": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}


# ── Year-aware holiday resolver ──────────────────────────────────────────────

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DATA_DIR = os.path.join(_REPO_ROOT, "data")

# In-memory cache so repeated calls for the same year don't re-read the file
# or re-emit the fallback warning.
_holidays_cache: Dict[int, Dict[str, str]] = {}
_warned_years: set = set()


def get_nse_holidays(year: int) -> Dict[str, str]:
    """Return the NSE trading-holiday map for `year` ({ISO-date: name}).

    Resolution order:
      1. In-memory cache (fastest — no I/O on hot paths like is_market_day).
      2. data/nse_holidays_{year}.json (written by scripts/fetch_nse_holidays.py).
      3. Hardcoded NSE_HOLIDAYS_2026 when year == 2026.
      4. Empty dict + one-shot WARNING per year (conservative fallback).
    """
    cached = _holidays_cache.get(year)
    if cached is not None:
        return cached

    cache_path = os.path.join(_DATA_DIR, f"nse_holidays_{year}.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _holidays_cache[year] = loaded
                return loaded
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                f"[MarketCalendar] Failed to read {cache_path}: {e} — using fallback"
            )

    if year == 2026:
        _holidays_cache[year] = NSE_HOLIDAYS_2026
        return NSE_HOLIDAYS_2026

    if year not in _warned_years:
        logger.warning(
            f"[MarketCalendar] NSE holiday list for {year} not found — "
            f"treating all weekdays as trading days. "
            f"Run scripts/fetch_nse_holidays.py {year} to populate."
        )
        _warned_years.add(year)
    _holidays_cache[year] = {}
    return {}


def _today_ist() -> date:
    """Return today's date in IST. Never uses UTC date."""
    return datetime.now(IST).date()


def get_today_holiday_name() -> Optional[str]:
    """
    Return the NSE holiday name if today (IST) is a listed holiday, else None.
    Does NOT check weekends — call is_market_open_today() for the full guard.
    """
    today = _today_ist()
    return get_nse_holidays(today.year).get(today.isoformat())


def is_market_open_today() -> bool:
    """
    Return True if NSE/BSE is a trading day today (IST date).

    Returns False when:
      - Today is Saturday or Sunday
      - Today appears in the NSE 2026 holiday list
    """
    today = _today_ist()
    weekday = today.weekday()  # Monday=0 … Sunday=6

    if weekday >= 5:  # Saturday or Sunday
        logger.debug(
            f"[MarketCalendar] {today} is a weekend (weekday={weekday}) — market closed"
        )
        return False

    holiday = get_today_holiday_name()
    if holiday:
        logger.debug(
            f"[MarketCalendar] {today} is NSE holiday: {holiday} — market closed"
        )
        return False

    logger.debug(f"[MarketCalendar] {today} is a trading day — market open")
    return True


# ── NSE session boundaries ────────────────────────────────────────────────────

_NSE_OPEN: dt_time = dt_time(9, 15)
_NSE_CLOSE: dt_time = dt_time(15, 30)


def is_market_day(d: date) -> bool:
    """Return True if d is an NSE trading day (not weekend, not a listed holiday).

    Holiday list is resolved per-year via get_nse_holidays(). Years without a
    cached holiday file fall back to weekends-only (see get_nse_holidays docstring).
    """
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d.isoformat() not in get_nse_holidays(d.year)


def market_minutes_of_exposure(filing_ts: datetime, as_of: datetime) -> int:
    """Count NSE market minutes elapsed between filing_ts and as_of.

    Only minutes within the NSE session window (09:15–15:30 IST, non-weekend,
    non-holiday) are counted. Returns 0 when as_of <= filing_ts.

    Naive datetimes are assumed IST (debug warning emitted). Tz-aware datetimes
    are converted to IST before comparison.
    """
    if filing_ts.tzinfo is None:
        logger.debug(f"[market_calendar] naive datetime assumed IST: {filing_ts}")
        filing_ts = filing_ts.replace(tzinfo=IST)
    else:
        filing_ts = filing_ts.astimezone(IST)

    if as_of.tzinfo is None:
        logger.debug(f"[market_calendar] naive datetime assumed IST: {as_of}")
        as_of = as_of.replace(tzinfo=IST)
    else:
        as_of = as_of.astimezone(IST)

    if as_of <= filing_ts:
        return 0

    total_minutes = 0
    current_date = filing_ts.date()
    end_date = as_of.date()

    while current_date <= end_date:
        if is_market_day(current_date):
            session_start = datetime.combine(current_date, _NSE_OPEN).replace(tzinfo=IST)
            session_end = datetime.combine(current_date, _NSE_CLOSE).replace(tzinfo=IST)
            effective_start = max(filing_ts, session_start)
            effective_end = min(as_of, session_end)
            if effective_start < effective_end:
                total_minutes += int((effective_end - effective_start).total_seconds() // 60)
        current_date += timedelta(days=1)

    return total_minutes
