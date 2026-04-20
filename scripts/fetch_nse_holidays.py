#!/usr/bin/env python3
"""
fetch_nse_holidays.py — Populate data/nse_holidays_{year}.json

Fetches the NSE trading-holiday list from the NSE public API and writes it to
data/nse_holidays_{year}.json in the shape expected by
src.utils.market_calendar.get_nse_holidays().

Run once in December for the upcoming year (add to cron notes in AGENTS.md).
Graceful failure: if the API is unavailable or the payload shape is unexpected,
the script logs an error and exits with code 1.

Usage:
    python scripts/fetch_nse_holidays.py          # defaults to next year
    python scripts/fetch_nse_holidays.py 2027     # explicit year
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, Optional

import requests


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fetch_nse_holidays")

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DATA_DIR = os.path.join(_REPO_ROOT, "data")

_NSE_API = "https://www.nseindia.com/api/holiday-master?type=trading"
_NSE_HOME = "https://www.nseindia.com"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _fetch_holiday_payload() -> Optional[dict]:
    """Acquire NSE session cookies then fetch the holiday-master JSON.

    Returns the parsed payload or None on any failure. Never raises.
    """
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": _NSE_HOME + "/",
    }
    try:
        session = requests.Session()
        session.headers.update(headers)
        session.get(_NSE_HOME, timeout=10)
        resp = session.get(_NSE_API, timeout=10)
        if resp.status_code != 200:
            logger.error(f"NSE API returned HTTP {resp.status_code}")
            return None
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.error(f"NSE holiday fetch failed: {e}")
        return None


def _extract_year(payload: dict, year: int) -> Dict[str, str]:
    """Extract {ISO-date: holiday_name} for `year` from the NSE payload.

    NSE returns a structure with segment arrays (CM, SLBS, etc.). Each entry
    has fields like "tradingDate" ("26-Jan-2026"), "description", "day".
    We read the CM (cash market) segment and keep only rows matching `year`.
    """
    out: Dict[str, str] = {}
    if not isinstance(payload, dict):
        return out
    # CM is the equities segment — the one we care about.
    rows = payload.get("CM") or payload.get("cm") or []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_dt = (row.get("tradingDate") or row.get("TradingDate") or "").strip()
        name = (row.get("description") or row.get("Description") or "").strip()
        if not raw_dt:
            continue
        try:
            # "26-Jan-2026" → 2026-01-26
            parsed = datetime.strptime(raw_dt, "%d-%b-%Y").date()
        except ValueError:
            continue
        if parsed.year != year:
            continue
        out[parsed.isoformat()] = name or "Trading Holiday"
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "year", nargs="?", type=int,
        default=datetime.now().year + 1,
        help="Target year (default: next calendar year)",
    )
    args = parser.parse_args()

    logger.info(f"Fetching NSE holiday list for {args.year}")
    payload = _fetch_holiday_payload()
    if payload is None:
        return 1

    holidays = _extract_year(payload, args.year)
    if not holidays:
        logger.error(
            f"No holidays found for {args.year} in NSE payload — "
            f"API shape may have changed. Payload keys: {list(payload.keys())}"
        )
        return 1

    os.makedirs(_DATA_DIR, exist_ok=True)
    out_path = os.path.join(_DATA_DIR, f"nse_holidays_{args.year}.json")
    with open(out_path, "w") as f:
        json.dump(holidays, f, indent=2, sort_keys=True)
    logger.info(f"Wrote {len(holidays)} holidays → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
