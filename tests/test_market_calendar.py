"""
test_market_calendar.py — year-aware NSE holiday lookup.

Steps 1-6 hardcoded NSE_HOLIDAYS_2026 and worked correctly through 2026.
Step 8 extends this with get_nse_holidays(year) which reads from
data/nse_holidays_{year}.json when present and falls back to weekends-only
otherwise.

Each test resets the in-memory caches (_holidays_cache, _warned_years) so
assertions are independent.
"""
import json
from datetime import date

import pytest

from src.utils import market_calendar as mc


@pytest.fixture(autouse=True)
def _reset_caches():
    """Clear year-lookup caches before each test for deterministic behaviour."""
    mc._holidays_cache.clear()
    mc._warned_years.clear()
    yield
    mc._holidays_cache.clear()
    mc._warned_years.clear()


# ── 2026: hardcoded list remains authoritative ───────────────────────────────

def test_get_holidays_2026_returns_hardcoded_list():
    """get_nse_holidays(2026) returns the NSE_HOLIDAYS_2026 dict."""
    holidays = mc.get_nse_holidays(2026)
    assert holidays is mc.NSE_HOLIDAYS_2026
    assert "2026-01-26" in holidays
    assert holidays["2026-01-26"] == "Republic Day"


def test_is_market_day_2026_republic_day_is_holiday():
    """Hardcoded 2026-01-26 must be flagged as non-trading."""
    assert mc.is_market_day(date(2026, 1, 26)) is False


def test_is_market_day_2026_regular_weekday_is_trading():
    """Regular 2026 weekday (no holiday) must be a trading day."""
    assert mc.is_market_day(date(2026, 5, 11)) is True  # Mon 2026-05-11


# ── 2027+: file-based cache with graceful fallback ───────────────────────────

def test_is_market_day_uses_year_specific_holidays(tmp_path, monkeypatch):
    """When data/nse_holidays_2027.json exists and contains Republic Day,
    is_market_day(2027-01-26) returns False."""
    monkeypatch.setattr(mc, "_DATA_DIR", str(tmp_path))
    (tmp_path / "nse_holidays_2027.json").write_text(
        json.dumps({"2027-01-26": "Republic Day"}, sort_keys=True)
    )

    holidays = mc.get_nse_holidays(2027)
    assert holidays == {"2027-01-26": "Republic Day"}
    assert mc.is_market_day(date(2027, 1, 26)) is False
    # Other weekdays in 2027 without entries remain trading days.
    assert mc.is_market_day(date(2027, 1, 27)) is True  # Wed


def test_graceful_fallback_when_year_file_absent(tmp_path, monkeypatch, caplog):
    """When data/nse_holidays_2027.json is missing, get_nse_holidays returns
    {} and emits exactly one WARNING (not one-per-call)."""
    monkeypatch.setattr(mc, "_DATA_DIR", str(tmp_path))

    with caplog.at_level("WARNING"):
        first = mc.get_nse_holidays(2027)
        second = mc.get_nse_holidays(2027)

    assert first == {}
    assert second == {}
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "2027" in r.getMessage()]
    assert len(warnings) == 1, (
        f"expected one WARNING per missing year, got {len(warnings)}"
    )


def test_is_market_day_2027_weekday_is_trading_day_without_file(tmp_path, monkeypatch):
    """With no 2027 holiday file, Republic Day 2027 is treated as a trading day
    (conservative default — better to over-trade than miss a real session)."""
    monkeypatch.setattr(mc, "_DATA_DIR", str(tmp_path))
    assert mc.is_market_day(date(2027, 1, 26)) is True  # Tue, no file → trading


def test_weekend_always_non_trading_regardless_of_year(tmp_path, monkeypatch):
    """Weekends are non-trading even when no holiday file exists for the year."""
    monkeypatch.setattr(mc, "_DATA_DIR", str(tmp_path))
    assert mc.is_market_day(date(2027, 1, 30)) is False  # Sat
    assert mc.is_market_day(date(2027, 1, 31)) is False  # Sun


# ── Corrupt file → fallback, not crash ───────────────────────────────────────

def test_corrupt_holiday_file_falls_back_gracefully(tmp_path, monkeypatch, caplog):
    """A malformed JSON file must not crash the scheduler — fall back to empty."""
    monkeypatch.setattr(mc, "_DATA_DIR", str(tmp_path))
    (tmp_path / "nse_holidays_2028.json").write_text("{not json")

    with caplog.at_level("WARNING"):
        holidays = mc.get_nse_holidays(2028)

    assert holidays == {}
    assert mc.is_market_day(date(2028, 1, 26)) is True  # Wed, no valid file
