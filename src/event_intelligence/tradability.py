"""Tradability gate for the event-intelligence pipeline.

Classifies each filing's symbol into one of three buckets:

  COVERED    — ≥3 analyst estimates available (Nifty 100 + actively-covered midcaps)
  UNCOVERED  — < 3 estimates but above all liquidity gates (self-consensus applies)
  EXCLUDED   — fails at least one hard-kill liquidity gate (never reaches HYDRA)

All thresholds are v1 defaults exposed in `config.yaml` under `tradability.*`
and can be tuned without code changes. See `TradabilityConfig` for the full list.

Universe data source:
  - `data/tradability_universe.json` — daily-refreshed snapshot built by the
    overnight job (from Zerodha instruments file + 20d volume from market_history)
  - Falls back to UNCOVERED (not EXCLUDED) when the symbol is absent from the
    universe file, so new/unusual symbols are never silently dropped.

Hard-kill gates (all must pass):
  1. avg_daily_turnover_cr ≥ min_avg_turnover_cr           (default 2.0)
  2. avg_daily_volume_shares ≥ min_avg_volume_shares        (default 50_000)
  3. market_cap_cr ≥ min_market_cap_cr                      (default 200)
  4. price_inr ≥ min_price_inr                              (default 10)
  5. circuit_hits_30d ≤ max_circuit_hits_30d                (default 3)
  6. segment not in T2T                                      (exclude_t2t=True)
  7. platform not SME                                        (exclude_sme=True)
  8. asm_stage < 2 (or not on ASM Stage-II+)                (exclude_asm_stage2_plus=True)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Cache TTL — universe file is refreshed nightly; reload if stale
_CACHE_TTL_HOURS = 25


@dataclass(frozen=True)
class TradabilityConfig:
    """Configurable thresholds for the tradability gate.

    All defaults are v1 planning estimates. Tune from observed universe stats
    once the real Zerodha instruments data populates tradability_universe.json.
    """
    min_avg_turnover_cr: float = 2.0        # avg daily ₹ turnover (20d)
    min_avg_volume_shares: int = 50_000     # avg daily volume (20d)
    min_market_cap_cr: float = 200.0        # market capitalisation
    min_price_inr: float = 10.0             # last traded price
    max_circuit_hits_30d: int = 3           # upper/lower circuit triggers
    exclude_t2t: bool = True                # trade-to-trade segment
    exclude_sme: bool = True                # BSE SME / NSE Emerge
    exclude_asm_stage2_plus: bool = True    # ASM Stage-II or higher


@dataclass
class TradabilityVerdict:
    """Result of the tradability gate for one symbol."""
    symbol: str
    tradeable: bool
    bucket: str                 # 'COVERED' | 'UNCOVERED' | 'EXCLUDED'
    avg_turnover_cr: Optional[float] = None
    avg_volume_shares: Optional[float] = None
    market_cap_cr: Optional[float] = None
    reason: str = ""            # human-readable for logs/audit


# ── Universe cache ────────────────────────────────────────────────────────────

_universe_cache: Optional[Dict[str, dict]] = None
_universe_loaded_at: Optional[datetime] = None


def _load_universe(data_dir: str = "data") -> Dict[str, dict]:
    """Load tradability_universe.json, with in-process TTL caching."""
    global _universe_cache, _universe_loaded_at

    now = datetime.now(IST)
    if (
        _universe_cache is not None
        and _universe_loaded_at is not None
        and (now - _universe_loaded_at) < timedelta(hours=_CACHE_TTL_HOURS)
    ):
        return _universe_cache

    path = Path(data_dir) / "tradability_universe.json"
    if not path.exists():
        logger.debug("[Tradability] universe file absent: %s — all symbols will be UNCOVERED", path)
        _universe_cache = {}
        _universe_loaded_at = now
        return _universe_cache

    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            # Accept list-of-records or dict-keyed-by-symbol
            _universe_cache = {r["symbol"]: r for r in data if "symbol" in r}
        elif isinstance(data, dict):
            _universe_cache = data
        else:
            _universe_cache = {}
        _universe_loaded_at = now
        logger.debug("[Tradability] loaded universe: %d symbols", len(_universe_cache))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[Tradability] failed to load universe: %s", e)
        _universe_cache = {}
        _universe_loaded_at = now

    return _universe_cache


def invalidate_cache() -> None:
    """Force reload on next gate check (call after nightly universe refresh)."""
    global _universe_cache, _universe_loaded_at
    _universe_cache = None
    _universe_loaded_at = None


# ── Gate logic ────────────────────────────────────────────────────────────────

def check_tradability(
    symbol: str,
    data_dir: str = "data",
    cfg: Optional[TradabilityConfig] = None,
) -> TradabilityVerdict:
    """Evaluate tradability for one symbol. Never raises.

    If the symbol is absent from the universe file, returns UNCOVERED (not EXCLUDED)
    so novel listings are not silently suppressed — the caller must decide whether
    to proceed with reduced confidence.
    """
    cfg = cfg or TradabilityConfig()
    universe = _load_universe(data_dir)
    record = universe.get(symbol) or universe.get(symbol.upper())

    if record is None:
        return TradabilityVerdict(
            symbol=symbol,
            tradeable=True,
            bucket="UNCOVERED",
            reason="symbol_not_in_universe",
        )

    # ── Hard-kill gates ────────────────────────────────────────────────
    turnover = record.get("avg_turnover_cr")
    volume = record.get("avg_volume_shares")
    mktcap = record.get("market_cap_cr")
    price = record.get("price_inr")
    circuit_hits = int(record.get("circuit_hits_30d") or 0)
    segment = (record.get("segment") or "").upper()
    platform = (record.get("platform") or "").upper()
    asm_stage = int(record.get("asm_stage") or 0)
    n_analysts = int(record.get("n_analysts") or 0)

    if turnover is not None and turnover < cfg.min_avg_turnover_cr:
        return TradabilityVerdict(
            symbol=symbol, tradeable=False, bucket="EXCLUDED",
            avg_turnover_cr=turnover,
            reason=f"low_turnover:{turnover:.1f}cr<{cfg.min_avg_turnover_cr}",
        )

    if volume is not None and volume < cfg.min_avg_volume_shares:
        return TradabilityVerdict(
            symbol=symbol, tradeable=False, bucket="EXCLUDED",
            avg_volume_shares=volume,
            reason=f"low_volume:{volume:.0f}<{cfg.min_avg_volume_shares}",
        )

    if mktcap is not None and mktcap < cfg.min_market_cap_cr:
        return TradabilityVerdict(
            symbol=symbol, tradeable=False, bucket="EXCLUDED",
            market_cap_cr=mktcap,
            reason=f"small_cap:{mktcap:.0f}cr<{cfg.min_market_cap_cr}",
        )

    if price is not None and price < cfg.min_price_inr:
        return TradabilityVerdict(
            symbol=symbol, tradeable=False, bucket="EXCLUDED",
            reason=f"penny_stock:price={price:.2f}<{cfg.min_price_inr}",
        )

    if circuit_hits > cfg.max_circuit_hits_30d:
        return TradabilityVerdict(
            symbol=symbol, tradeable=False, bucket="EXCLUDED",
            reason=f"circuit_breaker:{circuit_hits}_hits>max_{cfg.max_circuit_hits_30d}",
        )

    if cfg.exclude_t2t and "T2T" in segment:
        return TradabilityVerdict(
            symbol=symbol, tradeable=False, bucket="EXCLUDED",
            reason="t2t_segment",
        )

    if cfg.exclude_sme and ("SME" in platform or "EMERGE" in platform):
        return TradabilityVerdict(
            symbol=symbol, tradeable=False, bucket="EXCLUDED",
            reason="sme_platform",
        )

    if cfg.exclude_asm_stage2_plus and asm_stage >= 2:
        return TradabilityVerdict(
            symbol=symbol, tradeable=False, bucket="EXCLUDED",
            reason=f"asm_stage{asm_stage}",
        )

    # ── Bucket assignment ──────────────────────────────────────────────
    bucket = "COVERED" if n_analysts >= 3 else "UNCOVERED"
    return TradabilityVerdict(
        symbol=symbol,
        tradeable=True,
        bucket=bucket,
        avg_turnover_cr=turnover,
        avg_volume_shares=volume,
        market_cap_cr=mktcap,
        reason=f"ok:n_analysts={n_analysts}",
    )


def is_tradeable(symbol: str, data_dir: str = "data") -> bool:
    """Convenience wrapper for pipeline callers that only need a boolean."""
    return check_tradability(symbol, data_dir).tradeable
