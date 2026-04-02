"""
market_phase.py — Intraday Market Phase Classifier
----------------------------------------------------
Classifies the current NSE market state into one of 6 phases.
Each phase drives Layer A of the dynamic conviction engine.

Phases:
  PANIC        — Nifty < -1.5%, still falling, first 30 min
  STABILISATION — Market stopped falling, waiting
  RECOVERY     — Bouncing from lows, A/D improving
  TRENDING_BULL — Clear uptrend with breadth confirmation
  TRENDING_BEAR — Clear downtrend, broad selling
  CHOPPY       — No direction, default/fallback

Data sources (all fetched by runner, passed in as MarketSnapshot):
  - Nifty 50 LTP + prev close → % change
  - Nifty 5-min candle direction (last 3 bars)
  - Advance/Decline ratio from NSE
  - India VIX live
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


class MarketPhase(Enum):
    PANIC = "panic"
    STABILISATION = "stabilisation"
    RECOVERY = "recovery"
    TRENDING_BULL = "trending_bull"
    TRENDING_BEAR = "trending_bear"
    CHOPPY = "choppy"


# Layer A base scores per phase, keyed by (phase, direction)
PHASE_LAYER_A: Dict[Tuple[MarketPhase, str], int] = {
    # LONG direction
    (MarketPhase.PANIC, "BUY"):            10,
    (MarketPhase.STABILISATION, "BUY"):    35,
    (MarketPhase.RECOVERY, "BUY"):         65,
    (MarketPhase.TRENDING_BULL, "BUY"):    85,
    (MarketPhase.TRENDING_BEAR, "BUY"):    15,
    (MarketPhase.CHOPPY, "BUY"):           45,
    # SHORT direction
    (MarketPhase.PANIC, "SHORT"):          85,
    (MarketPhase.STABILISATION, "SHORT"):  55,
    (MarketPhase.RECOVERY, "SHORT"):       30,
    (MarketPhase.TRENDING_BULL, "SHORT"):  15,
    (MarketPhase.TRENDING_BEAR, "SHORT"):  80,
    (MarketPhase.CHOPPY, "SHORT"):         45,
}


@dataclass
class MarketSnapshot:
    """All live market data needed for phase classification + Layer A/B scoring."""
    # Nifty
    nifty_ltp: float = 0.0
    nifty_prev_close: float = 0.0
    nifty_open: float = 0.0
    nifty_day_low: float = 0.0
    nifty_pct: float = 0.0           # % change from prev close
    nifty_direction_5m: str = "FLAT"  # "UP", "DOWN", "FLAT" — from last 3 five-min candles

    # Breadth
    ad_ratio: float = 0.5            # advances / (advances + declines), 0.0–1.0

    # VIX
    vix: float = 15.0

    # Sector indices: sector_name → % change today
    sector_changes: Dict[str, float] = field(default_factory=dict)

    # Metadata
    timestamp: Optional[datetime] = None

    # For phase transition detection
    nifty_pct_from_low: float = 0.0  # % recovery from intraday low
    prev_ad_ratio: float = 0.5       # previous cycle's A/D for trend detection


@dataclass
class PhaseState:
    """Tracks the current phase and transition history."""
    current_phase: MarketPhase = MarketPhase.CHOPPY
    entered_at: Optional[datetime] = None
    prev_phase: Optional[MarketPhase] = None
    transitions: List[str] = field(default_factory=list)  # log of transitions


def classify_phase(snapshot: MarketSnapshot, prev_state: PhaseState) -> MarketPhase:
    """
    Classify the current market phase from live data.

    Rules (evaluated in priority order — first match wins):
    1. PANIC:          Nifty < -1.5%, direction DOWN, time < 09:45
    2. RECOVERY:       Up > 0.5% from intraday low AND A/D improving
    3. STABILISATION:  Was PANIC/BEAR and Nifty direction FLAT
    4. TRENDING_BULL:  Nifty > +0.3%, direction UP, A/D > 0.6
    5. TRENDING_BEAR:  Nifty < -0.3%, direction DOWN, A/D < 0.4
    6. CHOPPY:         Everything else (default)
    """
    now = datetime.now(IST)
    current_time = now.time()
    pct = snapshot.nifty_pct
    direction = snapshot.nifty_direction_5m
    ad = snapshot.ad_ratio
    vix = snapshot.vix
    pct_from_low = snapshot.nifty_pct_from_low
    prev_ad = snapshot.prev_ad_ratio
    prev_phase = prev_state.current_phase

    # 1. PANIC: heavy gap-down, still falling, early session
    if (pct < -1.5
            and direction == "DOWN"
            and current_time < dt_time(9, 45)
            and vix > 16):
        return MarketPhase.PANIC

    # 2. RECOVERY: bouncing from lows with improving breadth
    if (pct_from_low > 0.5
            and ad > prev_ad
            and prev_phase in (MarketPhase.PANIC, MarketPhase.STABILISATION, MarketPhase.TRENDING_BEAR)):
        return MarketPhase.RECOVERY

    # 3. STABILISATION: was in panic/bear and now flat
    if (prev_phase in (MarketPhase.PANIC, MarketPhase.TRENDING_BEAR)
            and direction in ("FLAT", "UP")
            and pct < -0.3):
        return MarketPhase.STABILISATION

    # 4. TRENDING_BULL: clear uptrend with breadth
    if pct > 0.3 and direction == "UP" and ad > 0.6:
        return MarketPhase.TRENDING_BULL

    # 5. TRENDING_BEAR: clear downtrend (not first 30 min — that's PANIC)
    if (pct < -0.3
            and direction == "DOWN"
            and ad < 0.4
            and current_time >= dt_time(9, 45)):
        return MarketPhase.TRENDING_BEAR

    # 6. CHOPPY: default
    return MarketPhase.CHOPPY


def update_phase(snapshot: MarketSnapshot, state: PhaseState) -> PhaseState:
    """
    Classify phase and update state with transition tracking.
    Returns the updated PhaseState.
    """
    new_phase = classify_phase(snapshot, state)

    if new_phase != state.current_phase:
        now = datetime.now(IST)
        transition = (
            f"[Phase] {state.current_phase.value} → {new_phase.value} "
            f"at {now.strftime('%H:%M')} IST "
            f"| Nifty={snapshot.nifty_pct:+.1f}% "
            f"| A/D={snapshot.ad_ratio:.2f} "
            f"| VIX={snapshot.vix:.1f}"
        )
        logger.info(transition)
        state.transitions.append(transition)
        state.prev_phase = state.current_phase
        state.current_phase = new_phase
        state.entered_at = now

    return state


def compute_layer_a(phase: MarketPhase, direction: str, snapshot: MarketSnapshot) -> float:
    """
    Compute Layer A score (0–100) from market phase + fine-tuning modifiers.

    Args:
        phase: Current MarketPhase
        direction: "BUY" or "SHORT"
        snapshot: Live market data for VIX/A/D modifiers
    """
    base = PHASE_LAYER_A.get((phase, direction), 50)

    # VIX modifiers
    if snapshot.vix > 22:
        base += -10 if direction == "BUY" else 5
    elif snapshot.vix < 14:
        base += 5 if direction == "BUY" else -5

    # A/D ratio modifiers
    if snapshot.ad_ratio > 0.65:
        base += 10 if direction == "BUY" else -10
    elif snapshot.ad_ratio < 0.35:
        base += -10 if direction == "BUY" else 10

    return max(0.0, min(100.0, float(base)))


def fetch_market_snapshot(kite_client, prev_snapshot: Optional[MarketSnapshot] = None) -> MarketSnapshot:
    """
    Fetch all live data needed for phase classification.
    Uses Kite API for Nifty/VIX and nsepython for A/D ratio.

    Args:
        kite_client: Kite Connect client (raw, not KiteLiveClient)
        prev_snapshot: Previous cycle's snapshot (for A/D trend detection)
    """
    snap = MarketSnapshot(timestamp=datetime.now(IST))

    if prev_snapshot:
        snap.prev_ad_ratio = prev_snapshot.ad_ratio

    # ── Nifty 50 ──────────────────────────────────────────────────────
    if kite_client:
        try:
            nifty_data = kite_client.ltp("NSE:NIFTY 50")
            if "NSE:NIFTY 50" in nifty_data:
                snap.nifty_ltp = nifty_data["NSE:NIFTY 50"]["last_price"]

            ohlc_data = kite_client.ohlc("NSE:NIFTY 50")
            if "NSE:NIFTY 50" in ohlc_data:
                d = ohlc_data["NSE:NIFTY 50"]
                snap.nifty_prev_close = d.get("ohlc", {}).get("close", 0)
                snap.nifty_open = d.get("ohlc", {}).get("open", 0)
                snap.nifty_day_low = d.get("ohlc", {}).get("low", 0) or snap.nifty_ltp

            if snap.nifty_prev_close > 0:
                snap.nifty_pct = (snap.nifty_ltp - snap.nifty_prev_close) / snap.nifty_prev_close * 100

            if snap.nifty_day_low > 0 and snap.nifty_ltp > 0:
                snap.nifty_pct_from_low = (snap.nifty_ltp - snap.nifty_day_low) / snap.nifty_day_low * 100

        except Exception as e:
            logger.warning(f"[Phase] Nifty fetch failed: {e}")

    # ── India VIX ─────────────────────────────────────────────────────
    if kite_client:
        try:
            vix_data = kite_client.ltp("NSE:INDIA VIX")
            if "NSE:INDIA VIX" in vix_data:
                vix_val = vix_data["NSE:INDIA VIX"]["last_price"]
                if 8 <= vix_val <= 50:
                    snap.vix = vix_val
        except Exception as e:
            logger.warning(f"[Phase] VIX fetch failed: {e}")

    # ── Advance/Decline Ratio ─────────────────────────────────────────
    try:
        from nsepython import nse_get_advances_declines
        ad_df = nse_get_advances_declines()
        if ad_df is not None and not ad_df.empty:
            # nsepython returns a DataFrame with advances/declines columns
            # Sum across all market segments
            advances = 0
            declines = 0
            for col in ad_df.columns:
                col_lower = col.strip().lower()
                if "advance" in col_lower:
                    advances += ad_df[col].sum()
                elif "decline" in col_lower:
                    declines += ad_df[col].sum()
            total = advances + declines
            if total > 0:
                snap.ad_ratio = advances / total
    except Exception as e:
        logger.warning(f"[Phase] A/D ratio fetch failed: {e}")

    # ── Nifty 5-min direction ─────────────────────────────────────────
    # Heuristic: compare current vs open to infer direction
    # A more precise method would use BarBuilder candles for NIFTY 50
    if snap.nifty_ltp > 0 and snap.nifty_open > 0:
        recent_move = (snap.nifty_ltp - snap.nifty_open) / snap.nifty_open * 100
        if prev_snapshot and prev_snapshot.nifty_ltp > 0:
            # Direction from last snapshot to now
            delta = (snap.nifty_ltp - prev_snapshot.nifty_ltp) / prev_snapshot.nifty_ltp * 100
            if delta > 0.05:
                snap.nifty_direction_5m = "UP"
            elif delta < -0.05:
                snap.nifty_direction_5m = "DOWN"
            else:
                snap.nifty_direction_5m = "FLAT"
        else:
            # First snapshot — use open-to-current
            if recent_move > 0.2:
                snap.nifty_direction_5m = "UP"
            elif recent_move < -0.2:
                snap.nifty_direction_5m = "DOWN"
            else:
                snap.nifty_direction_5m = "FLAT"

    # ── Sector Indices ────────────────────────────────────────────────
    SECTOR_INDEX_MAP = {
        "PHARMA": "NSE:NIFTY PHARMA",
        "IT": "NSE:NIFTY IT",
        "BANKING": "NSE:NIFTY BANK",
        "ENERGY": "NSE:NIFTY ENERGY",
        "AUTO": "NSE:NIFTY AUTO",
        "METALS": "NSE:NIFTY METAL",
        "FMCG": "NSE:NIFTY FMCG CONSUMPTION",
        "INFRA": "NSE:NIFTY INFRA",
    }
    if kite_client:
        try:
            all_sector_symbols = list(SECTOR_INDEX_MAP.values())
            sector_ohlc = kite_client.ohlc(all_sector_symbols)
            for sector_name, nse_sym in SECTOR_INDEX_MAP.items():
                if nse_sym in sector_ohlc:
                    d = sector_ohlc[nse_sym]
                    ltp = d.get("last_price", 0)
                    prev_c = d.get("ohlc", {}).get("close", 0)
                    if prev_c > 0 and ltp > 0:
                        snap.sector_changes[sector_name] = (ltp - prev_c) / prev_c * 100
        except Exception as e:
            logger.warning(f"[Phase] Sector index fetch failed: {e}")

    return snap
