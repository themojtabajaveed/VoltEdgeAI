"""
market_calendar.py — NSE Holiday Guard Utility
-----------------------------------------------
Determines whether the Indian equity market (NSE/BSE) is open on the current
IST date. Pure Python — no external API calls, no new dependencies.
"""
import logging
import zoneinfo
from datetime import datetime, date
from typing import Optional

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


def _today_ist() -> date:
    """Return today's date in IST. Never uses UTC date."""
    return datetime.now(IST).date()


def get_today_holiday_name() -> Optional[str]:
    """
    Return the NSE holiday name if today (IST) is a listed holiday, else None.
    Does NOT check weekends — call is_market_open_today() for the full guard.
    """
    return NSE_HOLIDAYS_2026.get(_today_ist().isoformat())


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
