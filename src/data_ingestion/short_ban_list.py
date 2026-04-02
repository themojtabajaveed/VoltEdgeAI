"""
short_ban_list.py — F&O Ban List + T2T/BE Series Detection
-----------------------------------------------------------
SHORT-6: F&O ban list — stocks under SEBI's F&O ban cannot be shorted.
SHORT-7: T2T (Trade-to-Trade / BE series) — must be delivery-settled,
         no intraday shorting allowed.

Data sources:
  - F&O ban: NSE /api/securities-under-ban (daily, refreshed at 08:00 IST)
  - T2T/BE: NSE bhavcopy via nsepython (series column: BE, BZ = restricted)

Fail-open: if any fetch fails, return empty set (don't block all trades).
"""
import os
import json
import time
import logging
from datetime import datetime, date
from typing import Set, Optional

logger = logging.getLogger(__name__)

# ── Module-level caches (refreshed daily) ────────────────────────────────────
_fo_ban_set: Set[str] = set()
_t2t_set: Set[str] = set()
_last_refresh_date: Optional[date] = None

# Restricted series codes — cannot be shorted intraday
_RESTRICTED_SERIES = {"BE", "BZ"}


def fetch_fo_ban_list() -> Set[str]:
    """
    Fetch today's F&O securities under ban from NSE.

    Returns set of NSE symbols (e.g. {"DELTACORP", "HINDCOPPER"}).
    Fail-open: returns empty set on any error.
    """
    global _fo_ban_set

    # Primary: NSE API via nsepython's session (handles cookies)
    try:
        from nsepython import nsefetch
        url = "https://www.nseindia.com/api/securities-under-ban"
        data = nsefetch(url)
        if isinstance(data, dict):
            # Response format varies: sometimes 'data', sometimes direct list
            ban_entries = data.get("data", data.get("secBan", []))
            if isinstance(ban_entries, list):
                symbols = set()
                for entry in ban_entries:
                    sym = entry.get("symbol", "") if isinstance(entry, dict) else ""
                    if sym:
                        symbols.add(sym.strip().upper())
                _fo_ban_set = symbols
                logger.info(f"[BanList] F&O ban list: {len(symbols)} symbols — {sorted(symbols)[:10]}")
                return _fo_ban_set
    except Exception as e:
        logger.warning(f"[BanList] NSE ban list API failed: {e}")

    # Fallback: try cached file
    cache_path = "data/fo_ban_cache.json"
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            cached_date = cached.get("date", "")
            today_str = str(date.today())
            if cached_date == today_str:
                _fo_ban_set = set(cached.get("symbols", []))
                logger.info(f"[BanList] Using cached F&O ban list ({len(_fo_ban_set)} symbols)")
                return _fo_ban_set
            else:
                logger.warning(f"[BanList] Cache stale (date={cached_date}), discarding")
        except Exception:
            pass

    # Fail-open: empty set
    logger.warning("[BanList] F&O ban list unavailable — fail-open (empty set)")
    _fo_ban_set = set()
    return _fo_ban_set


def fetch_t2t_symbols() -> Set[str]:
    """
    Fetch T2T / BE / BZ series symbols from NSE bhavcopy.

    These stocks are trade-to-trade — settlement is compulsory delivery,
    no intraday squareoff allowed, hence no shorting.

    Returns set of NSE symbols. Fail-open on error.
    """
    global _t2t_set

    try:
        from nsepython import get_bhavcopy

        # Use yesterday or last trading day for bhavcopy
        # Try today first, then yesterday, then day before
        import zoneinfo
        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        today = datetime.now(IST).date()

        for days_back in range(0, 4):
            try_date = today.replace(day=today.day)  # Will try today/yesterday/etc
            from datetime import timedelta
            try_date = today - timedelta(days=days_back)
            date_str = try_date.strftime("%d-%m-%Y")

            try:
                df = get_bhavcopy(date_str)
                if df is None or df.empty:
                    continue

                # Find SERIES column (may have leading space)
                series_col = next((c for c in df.columns if "SERIES" in c), None)
                if series_col is None:
                    logger.warning("[BanList] Bhavcopy missing SERIES column")
                    continue

                df[series_col] = df[series_col].str.strip()
                restricted = df[df[series_col].isin(_RESTRICTED_SERIES)]
                symbols = set(restricted["SYMBOL"].str.strip().str.upper())
                _t2t_set = symbols
                logger.info(f"[BanList] T2T/BE symbols: {len(symbols)} (from bhavcopy {date_str})")
                return _t2t_set

            except Exception as inner_e:
                logger.debug(f"[BanList] Bhavcopy {date_str} failed: {inner_e}")
                continue

    except ImportError:
        logger.warning("[BanList] nsepython not available — T2T detection disabled")
    except Exception as e:
        logger.warning(f"[BanList] T2T fetch failed: {e}")

    # Fail-open
    logger.warning("[BanList] T2T list unavailable — fail-open (empty set)")
    _t2t_set = set()
    return _t2t_set


def is_short_banned(symbol: str) -> bool:
    """Check if a symbol is in the F&O ban list (cannot open new F&O positions)."""
    return symbol.strip().upper() in _fo_ban_set


def is_t2t(symbol: str) -> bool:
    """Check if a symbol is T2T / BE series (delivery-only, no intraday shorting)."""
    return symbol.strip().upper() in _t2t_set


def is_safe_to_short(symbol: str) -> bool:
    """
    Master gate: returns True only if the symbol is NOT restricted for shorting.

    Checks:
      1. Not in F&O ban list
      2. Not a T2T / BE / BZ series stock

    This is the single function executor.py should call before any SHORT order.
    """
    sym = symbol.strip().upper()

    if sym in _fo_ban_set:
        logger.warning(f"[BanList] SHORT blocked: {sym} is in F&O ban list")
        return False

    if sym in _t2t_set:
        logger.warning(f"[BanList] SHORT blocked: {sym} is T2T/BE series (delivery-only)")
        return False

    return True


def refresh_ban_lists() -> None:
    """
    Refresh both ban lists. Called at 08:00 IST by runner.py.
    Persists F&O ban list to cache for fail-over.
    """
    global _last_refresh_date

    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).date()

    logger.info(f"[BanList] Refreshing ban lists for {today}...")

    # Fetch both
    fo_bans = fetch_fo_ban_list()
    t2t_syms = fetch_t2t_symbols()

    # Persist F&O ban cache
    if fo_bans:
        try:
            os.makedirs("data", exist_ok=True)
            with open("data/fo_ban_cache.json", "w") as f:
                json.dump({"date": str(today), "symbols": sorted(fo_bans)}, f, indent=2)
        except Exception as e:
            logger.warning(f"[BanList] Failed to cache ban list: {e}")

    _last_refresh_date = today
    logger.info(
        f"[BanList] Refresh complete: {len(fo_bans)} F&O banned, "
        f"{len(t2t_syms)} T2T/BE restricted"
    )


def get_ban_summary() -> str:
    """Human-readable summary for logging."""
    parts = []
    if _fo_ban_set:
        parts.append(f"F&O ban: {sorted(_fo_ban_set)[:8]}{'...' if len(_fo_ban_set) > 8 else ''}")
    if _t2t_set:
        parts.append(f"T2T/BE: {len(_t2t_set)} symbols")
    if not parts:
        parts.append("No restrictions loaded (fail-open)")
    return " | ".join(parts)
