"""
pre_market_data.py — Shared Pre-Market Data Pipeline
----------------------------------------------------
Single source of truth for pre-market signals consumed by DAWN and HYDRA.

Fetches once at ~08:00 IST, caches to data/premarket_cache_YYYY-MM-DD.json,
and exposes:
    - get_scan_universe()         → List[str] of NSE symbols (Tier1 + Tier2)
    - fetch_all_premarket_data()  → Dict[str, PreMarketSignals]

Reuses existing patterns:
    - NSE session/cookie    → exchange_filings._get_nse_session
    - yfinance history      → strategies.dawn._fetch_history_signals
    - Filing universe       → exchange_filings.fetch_exchange_filings
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests
import zoneinfo

from src.data_ingestion.exchange_filings import (
    FilingEvent,
    _get_nse_session,
    fetch_exchange_filings,
)

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# ── Paths ────────────────────────────────────────────────────────────────────
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DATA_DIR = os.path.join(_REPO_ROOT, "data")
_NIFTY200_CACHE = os.path.join(_DATA_DIR, "nifty200_symbols.json")
_PATTERN_DB = os.path.join(_DATA_DIR, "pattern_db.json")

# ── Tuning ───────────────────────────────────────────────────────────────────
_TIER1_URGENCY_MIN = 5.0
_YF_MAX_WORKERS = 10
_HTTP_TIMEOUT = 15

# ── Minimal sector fallback (only used if macro_sector_mapper lacks lookup) ──
# Covers major Nifty 200 names across 11 sectors; unknown symbols → "UNKNOWN".
_SECTOR_FALLBACK: Dict[str, str] = {
    # Banks / Financials
    "HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "SBIN": "BANKING",
    "AXISBANK": "BANKING", "KOTAKBANK": "BANKING", "INDUSINDBK": "BANKING",
    "BANKBARODA": "BANKING", "PNB": "BANKING", "FEDERALBNK": "BANKING",
    "IDFCFIRSTB": "BANKING", "AUBANK": "BANKING",
    "BAJFINANCE": "FINANCIALS", "BAJAJFINSV": "FINANCIALS",
    "HDFCLIFE": "FINANCIALS", "SBILIFE": "FINANCIALS", "ICICIPRULI": "FINANCIALS",
    "CHOLAFIN": "FINANCIALS", "SHRIRAMFIN": "FINANCIALS", "MUTHOOTFIN": "FINANCIALS",
    "LICI": "FINANCIALS", "HDFCAMC": "FINANCIALS",
    # IT
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTIM": "IT", "PERSISTENT": "IT", "COFORGE": "IT",
    "MPHASIS": "IT",
    # Energy / Oil & Gas
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "IOC": "ENERGY", "BPCL": "ENERGY",
    "HINDPETRO": "ENERGY", "GAIL": "ENERGY", "COALINDIA": "ENERGY",
    "NTPC": "POWER", "POWERGRID": "POWER", "TATAPOWER": "POWER",
    "ADANIPOWER": "POWER", "JSWENERGY": "POWER", "ADANIGREEN": "POWER",
    # Auto
    "TATAMOTORS": "AUTO", "M&M": "AUTO", "MARUTI": "AUTO", "BAJAJ-AUTO": "AUTO",
    "HEROMOTOCO": "AUTO", "EICHERMOT": "AUTO", "TVSMOTOR": "AUTO",
    "ASHOKLEY": "AUTO", "BOSCHLTD": "AUTO", "MOTHERSON": "AUTO",
    # Metals
    "TATASTEEL": "METALS", "JSWSTEEL": "METALS", "HINDALCO": "METALS",
    "VEDL": "METALS", "JINDALSTEL": "METALS", "SAIL": "METALS",
    "NMDC": "METALS", "HINDZINC": "METALS", "APLAPOLLO": "METALS",
    # Pharma
    "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA",
    "DIVISLAB": "PHARMA", "LUPIN": "PHARMA", "AUROPHARMA": "PHARMA",
    "TORNTPHARM": "PHARMA", "ZYDUSLIFE": "PHARMA", "APOLLOHOSP": "PHARMA",
    "MAXHEALTH": "PHARMA", "BIOCON": "PHARMA", "ALKEM": "PHARMA",
    # FMCG
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "GODREJCP": "FMCG",
    "MARICO": "FMCG", "COLPAL": "FMCG", "TATACONSUM": "FMCG",
    "VBL": "FMCG", "UNITDSPR": "FMCG",
    # Cement / Infra
    "ULTRACEMCO": "CEMENT", "GRASIM": "CEMENT", "SHREECEM": "CEMENT",
    "AMBUJACEM": "CEMENT", "ACC": "CEMENT", "DALBHARAT": "CEMENT",
    "LT": "INFRA", "ADANIPORTS": "INFRA", "GMRINFRA": "INFRA",
    "IRB": "INFRA", "RAILTEL": "INFRA", "IRCTC": "INFRA", "IRFC": "INFRA",
    # Telecom / Media
    "BHARTIARTL": "TELECOM", "IDEA": "TELECOM", "INDUSTOWER": "TELECOM",
    "ZEEL": "MEDIA",
    # Consumer / Retail
    "DMART": "RETAIL", "TRENT": "RETAIL", "TITAN": "RETAIL",
    "ABFRL": "RETAIL", "PAGEIND": "RETAIL", "NYKAA": "RETAIL",
    # Chemicals
    "PIDILITIND": "CHEMICALS", "UPL": "CHEMICALS", "SRF": "CHEMICALS",
    "TATACHEM": "CHEMICALS", "DEEPAKNTR": "CHEMICALS", "PIIND": "CHEMICALS",
}


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class PreMarketSignals:
    """All pre-market signals for a single symbol."""
    symbol: str
    as_of: str
    # Price / gap
    prev_close: float = 0.0
    current_price: float = 0.0
    gap_pct: float = 0.0
    # Volume
    avg_volume_20d: int = 0
    last_volume: int = 0
    rel_volume: float = 1.0
    volume_signal: str = "NORMAL"
    # Technical levels
    high_52w: float = 0.0
    high_30d: float = 0.0
    sma_20d: float = 0.0
    pct_from_52w_high: float = 0.0
    pct_from_30d_high: float = 0.0
    pct_from_20d_sma: float = 0.0
    technical_setup: str = "BASING"
    # F&O
    oi_change_pct: Optional[float] = None
    pcr: Optional[float] = None
    fno_signal: str = "NA"
    # Deals
    bulk_deal: bool = False
    block_deal: bool = False
    deal_net_qty: int = 0
    # Sector
    sector: str = "UNKNOWN"
    sector_momentum: str = "NEUTRAL"
    # Filing
    filing_category: Optional[str] = None
    filing_urgency: Optional[float] = None
    filing_impact_score: Optional[float] = None
    # Meta
    tier: int = 2
    data_quality: str = "FULL"


# ── Sector lookup ────────────────────────────────────────────────────────────

def _sector_for(symbol: str) -> str:
    """Resolve sector for a symbol. Prefers macro_sector_mapper if it exposes a
    lookup; falls back to minimal in-module dict."""
    try:
        from src.data_ingestion import macro_sector_mapper as msm  # type: ignore
        for name in ("sector_for", "get_sector", "sector_of"):
            fn = getattr(msm, name, None)
            if callable(fn):
                s = fn(symbol)
                if s:
                    return str(s).upper()
    except Exception:
        pass
    return _SECTOR_FALLBACK.get(symbol.upper(), "UNKNOWN")


# ── Nifty 200 universe (fetch once/day, cache to disk) ───────────────────────

_NIFTY200_URL = "https://archives.nseindia.com/content/indices/ind_nifty200list.csv"


def _fetch_nifty200() -> List[str]:
    """Fetch Nifty 200 symbol list from NSE archives. Cache daily.
    Falls back to last cached version on network failure."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        if os.path.exists(_NIFTY200_CACHE):
            with open(_NIFTY200_CACHE, "r") as f:
                cached = json.load(f)
            if cached.get("date") == today and cached.get("symbols"):
                return list(cached["symbols"])
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"[PreMarket] Nifty200 cache read failed: {e}")

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.nseindia.com/",
        }
        resp = requests.get(_NIFTY200_URL, headers=headers, timeout=_HTTP_TIMEOUT)
        if resp.status_code == 200 and resp.text:
            reader = csv.DictReader(io.StringIO(resp.text))
            symbols = []
            for row in reader:
                sym = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
                if sym:
                    symbols.append(sym)
            if symbols:
                try:
                    os.makedirs(_DATA_DIR, exist_ok=True)
                    with open(_NIFTY200_CACHE, "w") as f:
                        json.dump({"date": today, "symbols": symbols}, f)
                except OSError as e:
                    logger.warning(f"[PreMarket] Nifty200 cache write failed: {e}")
                logger.info(f"[PreMarket] Nifty200 fetched: {len(symbols)} symbols")
                return symbols
        logger.warning(f"[PreMarket] Nifty200 fetch HTTP {resp.status_code}")
    except requests.RequestException as e:
        logger.warning(f"[PreMarket] Nifty200 fetch failed: {e}")

    # Fallback: last cached version (ignoring date)
    try:
        if os.path.exists(_NIFTY200_CACHE):
            with open(_NIFTY200_CACHE, "r") as f:
                cached = json.load(f)
            symbols = cached.get("symbols") or []
            if symbols:
                logger.info(f"[PreMarket] Nifty200 using stale cache: {len(symbols)} symbols")
                return list(symbols)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[PreMarket] Nifty200 stale-cache read failed: {e}")

    logger.error("[PreMarket] Nifty200 unavailable — returning empty list")
    return []


# ── Universe assembly ────────────────────────────────────────────────────────

def _tier1_from_filings(filings: List[FilingEvent]) -> List[str]:
    seen = set()
    out = []
    for f in filings:
        if f.urgency >= _TIER1_URGENCY_MIN and f.symbol and f.symbol not in seen:
            seen.add(f.symbol)
            out.append(f.symbol)
    return out


def get_scan_universe() -> List[str]:
    """Return combined Tier1 (filings urgency>=5) + Tier2 (Nifty 200) symbol list.
    Deduplicated, preserves Tier1-first ordering. Safe to call independently of
    fetch_all_premarket_data — both DAWN and HYDRA can use this to know what to scan.
    """
    try:
        filings = fetch_exchange_filings(hours_back=24)
    except Exception as e:
        logger.warning(f"[PreMarket] filings fetch failed: {e}")
        filings = []
    tier1 = _tier1_from_filings(filings)

    tier2 = _fetch_nifty200()

    combined: List[str] = []
    seen = set()
    for sym in tier1 + tier2:
        if sym not in seen:
            seen.add(sym)
            combined.append(sym)
    logger.info(
        f"[PreMarket] Universe: {len(tier1)} tier1 + {len(tier2)} tier2 "
        f"→ {len(combined)} unique"
    )
    return combined


# ── yfinance per-symbol signals ──────────────────────────────────────────────

def _classify_volume(rel_vol: float, gap_pct: float) -> str:
    if rel_vol >= 2.0 and gap_pct >= 0.5:
        return "ACCUMULATION"
    if rel_vol >= 2.0 and gap_pct <= -0.5:
        return "DISTRIBUTION"
    if rel_vol < 0.5:
        return "DRY"
    return "NORMAL"


def _classify_technical(pct_30d: float, pct_52w: float, pct_sma: float) -> str:
    # Within 2% of 30d high with price above 20d SMA
    if pct_30d >= -2.0 and pct_sma >= 0:
        return "BREAKOUT_READY"
    if pct_52w <= -20.0:
        return "WEAK"
    if pct_sma >= 5.0 and pct_30d <= -5.0:
        return "EXTENDED"
    if -5.0 <= pct_sma < 2.0:
        return "PULLBACK"
    return "BASING"


def _fetch_yf_signals(symbol: str) -> dict:
    """Single yfinance history call, returns safe defaults on failure."""
    out = {
        "prev_close": 0.0, "current_price": 0.0, "gap_pct": 0.0,
        "avg_volume_20d": 0, "last_volume": 0, "rel_volume": 1.0,
        "high_52w": 0.0, "high_30d": 0.0, "sma_20d": 0.0,
        "pct_from_52w_high": 0.0, "pct_from_30d_high": 0.0, "pct_from_20d_sma": 0.0,
        "data_quality": "FULL",
    }
    try:
        import yfinance as yf
        tk = yf.Ticker(f"{symbol}.NS")
        hist = tk.history(period="1y")
        if hist.empty or len(hist) < 21:
            out["data_quality"] = "STALE"
            return out

        close = hist["Close"]
        volume = hist["Volume"]
        high = hist["High"]

        prev_close = float(close.iloc[-1])
        high_52w = float(high.max())
        high_30d = float(high.iloc[-30:].max())
        sma_20d = float(close.iloc[-20:].mean())
        avg_vol_20d = int(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else int(volume.mean())
        last_vol = int(volume.iloc[-1])
        rel_vol = (last_vol / avg_vol_20d) if avg_vol_20d > 0 else 1.0

        # Pre-market / current price via fast_info; fall back to prev_close
        current_price = prev_close
        try:
            fi = tk.fast_info
            lp = getattr(fi, "last_price", None) or fi.get("lastPrice") if hasattr(fi, "get") else getattr(fi, "last_price", None)
            if lp and float(lp) > 0:
                current_price = float(lp)
        except Exception:
            pass

        gap_pct = ((current_price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

        out.update({
            "prev_close": round(prev_close, 2),
            "current_price": round(current_price, 2),
            "gap_pct": round(gap_pct, 3),
            "avg_volume_20d": avg_vol_20d,
            "last_volume": last_vol,
            "rel_volume": round(rel_vol, 2),
            "high_52w": round(high_52w, 2),
            "high_30d": round(high_30d, 2),
            "sma_20d": round(sma_20d, 2),
            "pct_from_52w_high": round((current_price - high_52w) / high_52w * 100.0, 2) if high_52w > 0 else 0.0,
            "pct_from_30d_high": round((current_price - high_30d) / high_30d * 100.0, 2) if high_30d > 0 else 0.0,
            "pct_from_20d_sma": round((current_price - sma_20d) / sma_20d * 100.0, 2) if sma_20d > 0 else 0.0,
        })
        if gap_pct == 0.0:
            out["data_quality"] = "PARTIAL"
        return out
    except Exception as e:
        logger.debug(f"[PreMarket] yfinance failed for {symbol}: {e}")
        out["data_quality"] = "STALE"
        return out


# ── NSE F&O bhavcopy (yesterday) ─────────────────────────────────────────────

def _last_trading_day(now_ist: datetime) -> datetime:
    d = now_ist - timedelta(days=1)
    # Skip Sat/Sun
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _fetch_fno_bhavcopy() -> Dict[str, dict]:
    """Return {symbol: {oi_change_pct, pcr, fno_signal}} from yesterday's F&O bhavcopy.
    Computes OI change for futures (EXPIRY nearest) and PCR from option volumes.
    Empty dict on failure."""
    now_ist = datetime.now(IST)
    d = _last_trading_day(now_ist)
    date_str = d.strftime("%d%b%Y").upper()  # e.g. 16APR2026
    url = (
        f"https://archives.nseindia.com/content/historical/DERIVATIVES/"
        f"{d.year}/{d.strftime('%b').upper()}/fo{date_str}bhav.csv.zip"
    )
    session = _get_nse_session()
    try:
        resp = session.get(url, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"[PreMarket] F&O bhavcopy HTTP {resp.status_code} at {url}")
            return {}
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_name = zf.namelist()[0]
            raw = zf.read(csv_name).decode("utf-8", errors="ignore")
    except (requests.RequestException, zipfile.BadZipFile, OSError, IndexError) as e:
        logger.warning(f"[PreMarket] F&O bhavcopy fetch failed: {e}")
        return {}

    reader = csv.DictReader(io.StringIO(raw))
    # Aggregate per symbol: pick nearest-expiry futures + option volumes
    fut_by_sym: Dict[str, dict] = {}
    opt_agg: Dict[str, dict] = {}
    for row in reader:
        try:
            inst = (row.get("INSTRUMENT") or "").strip()
            sym = (row.get("SYMBOL") or "").strip().upper()
            expiry = (row.get("EXPIRY_DT") or "").strip()
            oi = float(row.get("OPEN_INT") or 0)
            chg_oi = float(row.get("CHG_IN_OI") or 0)
            contracts = float(row.get("CONTRACTS") or 0)
            opt_type = (row.get("OPTION_TYP") or "").strip()
        except ValueError:
            continue
        if not sym:
            continue
        if inst in ("FUTSTK", "FUTIDX"):
            existing = fut_by_sym.get(sym)
            if existing is None or expiry < existing["expiry"]:
                fut_by_sym[sym] = {"oi": oi, "chg_oi": chg_oi, "expiry": expiry}
        elif inst in ("OPTSTK", "OPTIDX"):
            agg = opt_agg.setdefault(sym, {"put_vol": 0.0, "call_vol": 0.0})
            if opt_type == "PE":
                agg["put_vol"] += contracts
            elif opt_type == "CE":
                agg["call_vol"] += contracts

    out: Dict[str, dict] = {}
    for sym, fut in fut_by_sym.items():
        oi_change_pct = (fut["chg_oi"] / (fut["oi"] - fut["chg_oi"]) * 100.0) \
            if (fut["oi"] - fut["chg_oi"]) > 0 else 0.0
        opts = opt_agg.get(sym)
        pcr = None
        if opts and opts["call_vol"] > 0:
            pcr = round(opts["put_vol"] / opts["call_vol"], 3)
        # Classify futures signal — oi_change vs we need price direction;
        # conservative classification from OI change alone.
        if oi_change_pct >= 5.0:
            fno_signal = "LONG_BUILDUP"
        elif oi_change_pct <= -5.0:
            fno_signal = "LONG_UNWIND"
        else:
            fno_signal = "NA"
        out[sym] = {
            "oi_change_pct": round(oi_change_pct, 2),
            "pcr": pcr,
            "fno_signal": fno_signal,
        }
    logger.info(f"[PreMarket] F&O bhavcopy: {len(out)} symbols parsed")
    return out


# ── NSE bulk/block deals ─────────────────────────────────────────────────────

def _fetch_deals(kind: str) -> Dict[str, dict]:
    """Fetch yesterday's bulk or block deals. kind in {'bulk','block'}.
    Returns {symbol: {'net_qty': int, 'side': 'BUY'|'SELL'}}."""
    now_ist = datetime.now(IST)
    d = _last_trading_day(now_ist)
    from_str = to_str = d.strftime("%d-%m-%Y")
    endpoint = "bulk-deals" if kind == "bulk" else "block-deals"
    url = (
        f"https://www.nseindia.com/api/historical/cm/{endpoint}"
        f"?from={from_str}&to={to_str}"
    )
    session = _get_nse_session()
    try:
        resp = session.get(url, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"[PreMarket] {kind}-deals HTTP {resp.status_code}")
            return {}
        raw = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"[PreMarket] {kind}-deals fetch failed: {e}")
        return {}

    rows = raw.get("data") or []
    out: Dict[str, dict] = {}
    for r in rows:
        sym = (r.get("symbol") or r.get("BD_SYMBOL") or "").strip().upper()
        qty_raw = r.get("quantity") or r.get("BD_QTY_TRD") or "0"
        side = (r.get("buySell") or r.get("BD_BUY_SELL") or "").strip().upper()
        try:
            qty = int(str(qty_raw).replace(",", ""))
        except ValueError:
            qty = 0
        if not sym:
            continue
        entry = out.setdefault(sym, {"net_qty": 0})
        if side.startswith("B"):
            entry["net_qty"] += qty
        elif side.startswith("S"):
            entry["net_qty"] -= qty
    logger.info(f"[PreMarket] {kind} deals: {len(out)} unique symbols")
    return out


# ── Sector momentum ──────────────────────────────────────────────────────────

def _compute_sector_momentum(per_symbol: Dict[str, dict]) -> Dict[str, str]:
    """Compute hot/warm/neutral/cold per sector from aggregate gap_pct."""
    buckets: Dict[str, List[float]] = {}
    for sym, d in per_symbol.items():
        sec = d.get("sector", "UNKNOWN")
        gap = d.get("gap_pct", 0.0)
        if sec == "UNKNOWN":
            continue
        buckets.setdefault(sec, []).append(gap)
    out: Dict[str, str] = {}
    for sec, gaps in buckets.items():
        if not gaps:
            out[sec] = "NEUTRAL"
            continue
        avg = sum(gaps) / len(gaps)
        if avg >= 0.5:
            out[sec] = "HOT"
        elif avg >= 0.1:
            out[sec] = "WARM"
        elif avg <= -0.5:
            out[sec] = "COLD"
        else:
            out[sec] = "NEUTRAL"
    return out


# ── Filing impact scorer ─────────────────────────────────────────────────────

_pattern_db_cache: Optional[dict] = None


def _load_pattern_db() -> dict:
    global _pattern_db_cache
    if _pattern_db_cache is not None:
        return _pattern_db_cache
    try:
        with open(_PATTERN_DB, "r") as f:
            _pattern_db_cache = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"[PreMarket] pattern_db load failed: {e}")
        _pattern_db_cache = {}
    return _pattern_db_cache


def _category_hit_rate(category: str) -> float:
    """Lookup historical hit-rate for a filing category from pattern_db.json.
    Returns 0-3 points. Read-only; never mutates the DB."""
    db = _load_pattern_db()
    if not db or not category:
        return 1.0
    # Best-effort scan of common shapes — don't guess schema rigidly
    cat_key = category.strip().lower()
    if not isinstance(db, dict):
        return 1.0
    buckets = db.get("categories") or db.get("filing_categories") or {}
    if isinstance(buckets, dict):
        entry = buckets.get(cat_key) or buckets.get(category)
        if isinstance(entry, dict):
            hr = entry.get("hit_rate") or entry.get("win_rate")
            if isinstance(hr, (int, float)):
                # Map 0.0-1.0 → 0-3
                return round(max(0.0, min(1.0, float(hr))) * 3.0, 2)
    return 1.0


def _sector_alignment_bonus(sector_momentum: str, direction_hint: str = "LONG") -> float:
    if direction_hint == "LONG":
        return {"HOT": 2.0, "WARM": 1.0, "NEUTRAL": 0.5, "COLD": 0.0}.get(sector_momentum, 0.5)
    return {"COLD": 2.0, "NEUTRAL": 0.5, "WARM": 0.0, "HOT": 0.0}.get(sector_momentum, 0.5)


def _score_filing_impact(filing: FilingEvent, sector_momentum: str) -> float:
    urgency_pts = min(filing.urgency * 0.5, 5.0)
    hit_rate_pts = _category_hit_rate(filing.category)
    sector_pts = _sector_alignment_bonus(sector_momentum)
    return round(min(urgency_pts + hit_rate_pts + sector_pts, 10.0), 2)


# ── Master function ──────────────────────────────────────────────────────────

def _cache_path(date_str: str) -> str:
    return os.path.join(_DATA_DIR, f"premarket_cache_{date_str}.json")


def fetch_all_premarket_data(
    symbols: Optional[List[str]] = None,
    date_str: Optional[str] = None,
    force_refresh: bool = False,
) -> Dict[str, PreMarketSignals]:
    """Fetch all pre-market signals for the universe.
    Cached to data/premarket_cache_YYYY-MM-DD.json — same-day cache is reused.
    If `symbols` is None, uses get_scan_universe().
    """
    now_ist = datetime.now(IST)
    date_str = date_str or now_ist.strftime("%Y-%m-%d")
    cache_file = _cache_path(date_str)

    # Cache hit (only when pulling the full universe — partial calls bypass)
    if symbols is None and not force_refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                cached = json.load(f)
            if cached.get("date") == date_str:
                signals_raw = cached.get("signals", {})
                out = {sym: PreMarketSignals(**d) for sym, d in signals_raw.items()}
                logger.info(f"[PreMarket] Loaded from cache: {len(out)} symbols")
                return out
        except (OSError, json.JSONDecodeError, TypeError) as e:
            logger.warning(f"[PreMarket] cache read failed, refetching: {e}")

    t0 = time.time()
    if symbols is None:
        symbols = get_scan_universe()

    # Tier1 filings map (symbol → latest filing)
    try:
        filings = fetch_exchange_filings(hours_back=24)
    except Exception as e:
        logger.warning(f"[PreMarket] filings fetch failed: {e}")
        filings = []
    filing_map: Dict[str, FilingEvent] = {}
    for f in filings:
        if f.urgency >= _TIER1_URGENCY_MIN:
            prev = filing_map.get(f.symbol)
            if prev is None or f.urgency > prev.urgency:
                filing_map[f.symbol] = f

    # Bhavcopy + deals (single fetch each, shared lookups)
    fno_map = _fetch_fno_bhavcopy()
    bulk_map = _fetch_deals("bulk")
    block_map = _fetch_deals("block")

    # Parallel yfinance fetch
    per_symbol_raw: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_YF_MAX_WORKERS) as ex:
        futures = {ex.submit(_fetch_yf_signals, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                per_symbol_raw[sym] = fut.result()
            except Exception as e:
                logger.debug(f"[PreMarket] worker failed for {sym}: {e}")
                per_symbol_raw[sym] = _fetch_yf_signals.__wrapped__(sym) if hasattr(
                    _fetch_yf_signals, "__wrapped__") else {}

    # Attach sector first (needed for sector momentum)
    for sym, d in per_symbol_raw.items():
        d["sector"] = _sector_for(sym)

    sector_mom = _compute_sector_momentum(per_symbol_raw)

    # Build final signal objects
    as_of = now_ist.isoformat()
    results: Dict[str, PreMarketSignals] = {}
    for sym in symbols:
        raw = per_symbol_raw.get(sym, {})
        sector = raw.get("sector", "UNKNOWN")
        sec_mom = sector_mom.get(sector, "NEUTRAL")
        fno = fno_map.get(sym, {})
        bulk = bulk_map.get(sym)
        block = block_map.get(sym)
        deal_net = (bulk["net_qty"] if bulk else 0) + (block["net_qty"] if block else 0)

        filing = filing_map.get(sym)
        sig = PreMarketSignals(
            symbol=sym,
            as_of=as_of,
            prev_close=raw.get("prev_close", 0.0),
            current_price=raw.get("current_price", 0.0),
            gap_pct=raw.get("gap_pct", 0.0),
            avg_volume_20d=raw.get("avg_volume_20d", 0),
            last_volume=raw.get("last_volume", 0),
            rel_volume=raw.get("rel_volume", 1.0),
            volume_signal=_classify_volume(raw.get("rel_volume", 1.0), raw.get("gap_pct", 0.0)),
            high_52w=raw.get("high_52w", 0.0),
            high_30d=raw.get("high_30d", 0.0),
            sma_20d=raw.get("sma_20d", 0.0),
            pct_from_52w_high=raw.get("pct_from_52w_high", 0.0),
            pct_from_30d_high=raw.get("pct_from_30d_high", 0.0),
            pct_from_20d_sma=raw.get("pct_from_20d_sma", 0.0),
            technical_setup=_classify_technical(
                raw.get("pct_from_30d_high", 0.0),
                raw.get("pct_from_52w_high", 0.0),
                raw.get("pct_from_20d_sma", 0.0),
            ),
            oi_change_pct=fno.get("oi_change_pct"),
            pcr=fno.get("pcr"),
            fno_signal=fno.get("fno_signal", "NA"),
            bulk_deal=bulk is not None,
            block_deal=block is not None,
            deal_net_qty=deal_net,
            sector=sector,
            sector_momentum=sec_mom,
            filing_category=filing.category if filing else None,
            filing_urgency=filing.urgency if filing else None,
            filing_impact_score=_score_filing_impact(filing, sec_mom) if filing else None,
            tier=1 if filing else 2,
            data_quality=raw.get("data_quality", "FULL"),
        )
        results[sym] = sig

    # Write cache (only when this was the full-universe call)
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        payload = {
            "date": date_str,
            "generated_at": as_of,
            "universe_size": len(results),
            "tier1_count": sum(1 for s in results.values() if s.tier == 1),
            "tier2_count": sum(1 for s in results.values() if s.tier == 2),
            "bhavcopy_symbols": len(fno_map),
            "bulk_deal_symbols": len(bulk_map),
            "block_deal_symbols": len(block_map),
            "signals": {sym: asdict(sig) for sym, sig in results.items()},
        }
        with open(cache_file, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        logger.info(
            f"[PreMarket] Cache written: {cache_file} "
            f"({os.path.getsize(cache_file)/1024:.1f} KB)"
        )
    except OSError as e:
        logger.warning(f"[PreMarket] cache write failed: {e}")

    logger.info(
        f"[PreMarket] Done in {time.time()-t0:.1f}s: {len(results)} symbols, "
        f"{len(filing_map)} tier1, {len(fno_map)} fno, "
        f"{len(bulk_map)} bulk, {len(block_map)} block"
    )
    return results
