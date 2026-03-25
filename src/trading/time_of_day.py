"""
time_of_day.py
--------------
Indian market time-of-day patterns for scoring adjustments.

Market phases:
  09:15–09:30  OPENING_CHAOS    Wild volatility, gap fills. Avoid entries.
  09:30–10:30  PRIME_TIME       Real trends emerge. Best entries.
  10:30–11:30  TREND_CONFIRM    Trends confirmed or fake-out clear. Good entries.
  11:30–13:30  LUNCH_LULL       Low volume, choppy. Avoid new entries.
  13:30–14:30  AFTERNOON_BUILD  Volume picks up. Okay for entries.
  14:30–15:15  CLOSING_MOMENTUM Institutional flows. Good exits, risky entries.
  15:15–15:30  SQUARE_OFF       MIS auto-close. Extreme slippage risk.

Also handles F&O expiry awareness.
"""
from datetime import time as dt_time, datetime, date
from dataclasses import dataclass
from typing import Optional
import calendar


@dataclass
class MarketPhase:
    name: str
    entry_score_modifier: float   # multiply tech score by this for entries
    description: str
    should_enter: bool            # hard gate: allow new entries?


# Phase definitions
PHASES = [
    (dt_time(9, 15), dt_time(9, 30),   MarketPhase("OPENING_CHAOS",     0.5, "High volatility, gap fills", False)),
    (dt_time(9, 30), dt_time(10, 30),  MarketPhase("PRIME_TIME",        1.2, "Trend emergence, best entries", True)),
    (dt_time(10, 30), dt_time(11, 30), MarketPhase("TREND_CONFIRM",     1.0, "Confirmed setups only", True)),
    (dt_time(11, 30), dt_time(13, 30), MarketPhase("LUNCH_LULL",        0.7, "Low volume, choppy action", False)),
    (dt_time(13, 30), dt_time(14, 30), MarketPhase("AFTERNOON_BUILD",   0.9, "Volume returning", True)),
    (dt_time(14, 30), dt_time(15, 15), MarketPhase("CLOSING_MOMENTUM",  0.6, "Institutional rebalancing flows", False)),
    (dt_time(15, 15), dt_time(15, 30), MarketPhase("SQUARE_OFF",        0.0, "MIS auto-close, avoid entirely", False)),
]


def get_current_phase(now: Optional[datetime] = None) -> MarketPhase:
    """Return the market phase for the current time."""
    if now is None:
        now = datetime.now()
    current_time = now.time()

    for start, end, phase in PHASES:
        if start <= current_time < end:
            return phase

    # Outside market hours
    return MarketPhase("CLOSED", 0.0, "Market closed", False)


def adjust_score_for_time(raw_score: float, now: Optional[datetime] = None) -> float:
    """
    Adjust a technical score based on time-of-day.
    During PRIME_TIME, scores get a 20% boost.
    During LUNCH_LULL, scores are penalized by 30%.
    """
    phase = get_current_phase(now)
    return raw_score * phase.entry_score_modifier


def should_allow_new_entry(now: Optional[datetime] = None) -> tuple:
    """
    Check if new entries should be allowed at this time.
    Returns (allowed, reason).
    """
    phase = get_current_phase(now)
    if not phase.should_enter:
        return False, f"[{phase.name}] {phase.description}"
    return True, f"[{phase.name}] {phase.description}"


# ── F&O Expiry Calendar ───────────────────────────────────────────────────

def is_weekly_expiry(d: Optional[date] = None) -> bool:
    """Thursday is weekly F&O expiry on NSE."""
    if d is None:
        d = date.today()
    return d.weekday() == 3  # Thursday


def is_monthly_expiry(d: Optional[date] = None) -> bool:
    """Last Thursday of the month is monthly F&O expiry."""
    if d is None:
        d = date.today()
    if d.weekday() != 3:
        return False
    # Check if this is the last Thursday
    _, last_day = calendar.monthrange(d.year, d.month)
    next_thursday = d.day + 7
    return next_thursday > last_day


def get_expiry_risk_factor(d: Optional[date] = None) -> float:
    """
    Returns a risk multiplier for F&O expiry days.
    1.0 = normal day
    0.7 = weekly expiry (reduce positions by 30%)
    0.5 = monthly expiry (reduce positions by 50%)
    """
    if d is None:
        d = date.today()
    if is_monthly_expiry(d):
        return 0.5
    if is_weekly_expiry(d):
        return 0.7
    return 1.0
