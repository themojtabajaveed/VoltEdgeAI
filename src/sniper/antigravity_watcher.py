"""
antigravity_watcher.py (v2)
---------------------------
State machine that watches a list of stocks for VWAP-bounce confirmation
before emitting BUY signals.

CHANGE FROM v1: Removed yfinance fetch dependency.
Bars are now passed in from the live BarBuilder/IntradayStore via
`runner.py`. The `tick()` method accepts a `bars_provider` callable.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Callable

from src.marketdata.intraday import compute_vwap_stats

VWAP_TOUCH_BAND      = 0.002   # 0.2% — price within this % of VWAP triggers "touch"
MAX_WATCH_DURATION_MIN = 90     # Cancel watch after 90 minutes without a bounce


class WatchState(str, Enum):
    WAITING_FOR_GRAVITY  = "WAITING_FOR_GRAVITY"   # price well above VWAP, waiting for pullback
    WAITING_FOR_BOUNCE   = "WAITING_FOR_BOUNCE"     # touched VWAP, waiting for green confirmation
    COMPLETED            = "COMPLETED"
    CANCELLED            = "CANCELLED"


@dataclass
class WatchedSymbol:
    symbol: str
    created_at: datetime
    last_checked_at: Optional[datetime] = None
    initial_z_score: Optional[float] = None
    initial_vwap: Optional[float] = None
    initial_ltp: Optional[float] = None
    state: WatchState = WatchState.WAITING_FOR_GRAVITY
    notes: List[str] = field(default_factory=list)


class AntigravityWatcher:
    def __init__(self):
        self._watched: Dict[str, WatchedSymbol] = {}

    def add_wait_signal(
        self, symbol: str, z_score: float, vwap: float, ltp: float,
        now: Optional[datetime] = None
    ) -> None:
        dt_now = now or datetime.now()
        if symbol in self._watched:
            active = self._watched[symbol]
            if active.state not in (WatchState.COMPLETED, WatchState.CANCELLED):
                active.notes.append(
                    f"[{dt_now.isoformat()}] Received another WAIT signal (Z={z_score:.2f})."
                )
                return
        self._watched[symbol] = WatchedSymbol(
            symbol=symbol,
            created_at=dt_now,
            initial_z_score=z_score,
            initial_vwap=vwap,
            initial_ltp=ltp,
            state=WatchState.WAITING_FOR_GRAVITY,
            notes=[f"[{dt_now.isoformat()}] Initial stretch detected (Z={z_score:.2f}, LTP={ltp}, VWAP={vwap})."]
        )

    def remove_symbol(self, symbol: str, reason: str) -> None:
        if symbol in self._watched:
            self._watched[symbol].state = WatchState.CANCELLED
            self._watched[symbol].notes.append(f"Removed: {reason}")
            self._watched.pop(symbol, None)

    def get_active_symbols(self) -> List[WatchedSymbol]:
        return [
            w for w in self._watched.values()
            if w.state not in (WatchState.COMPLETED, WatchState.CANCELLED)
        ]

    def _is_vwap_touch(self, ltp: float, vwap: float) -> bool:
        if vwap == 0:
            return False
        return abs(ltp - vwap) / vwap <= VWAP_TOUCH_BAND

    def tick(
        self,
        bars_provider: Callable[[str], List],   # callable(symbol) -> List[Bar]
        now: Optional[datetime] = None,
    ) -> List[dict]:
        """
        Called every intraday cycle. `bars_provider` is a function that
        returns live bars from the IntradayStore for a given symbol.
        Returns a list of BUY_SIGNAL event dicts for confirmed bounces.
        """
        dt_now = now or datetime.now()
        buy_signals = []

        for w in self.get_active_symbols():
            w.last_checked_at = dt_now

            # 0. Timeout guard
            duration_mins = (dt_now - w.created_at).total_seconds() / 60.0
            if duration_mins > MAX_WATCH_DURATION_MIN:
                self.remove_symbol(w.symbol, f"Timeout after {MAX_WATCH_DURATION_MIN} mins.")
                continue

            # 1. Fetch bars from the live store (not yfinance)
            try:
                bars = bars_provider(w.symbol)
            except Exception as e:
                w.notes.append(f"[{dt_now.isoformat()}] Error fetching bars: {e}")
                continue

            if not bars or len(bars) < 2:
                continue

            # 2. Compute VWAP stats from live bars
            stats = compute_vwap_stats(bars, interval="1m")
            if not stats or stats.vwap == 0:
                continue

            ltp  = stats.ltp
            vwap = stats.vwap
            latest_bar = bars[-1]

            # 3. State machine transitions
            if w.state == WatchState.WAITING_FOR_GRAVITY:
                if self._is_vwap_touch(ltp, vwap):
                    w.state = WatchState.WAITING_FOR_BOUNCE
                    w.notes.append(f"[{dt_now.isoformat()}] VWAP touch at {ltp:.2f} (VWAP={vwap:.2f})")
                elif ltp < (vwap * 0.995):
                    self.remove_symbol(w.symbol, "Price collapsed below VWAP without hovering.")
                    continue

            elif w.state == WatchState.WAITING_FOR_BOUNCE:
                if ltp < (vwap * 0.995):
                    self.remove_symbol(w.symbol, "Support failed — price fell below VWAP (bear control).")
                    continue

                if len(bars) >= 4:
                    is_green   = latest_bar.close > latest_bar.open
                    above_vwap = latest_bar.close > vwap
                    recent_vols = [b.volume for b in bars[-4:-1]]
                    avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0

                    if is_green and above_vwap and latest_bar.volume > (avg_vol * 1.25):
                        w.state = WatchState.COMPLETED
                        w.notes.append(
                            f"[{dt_now.isoformat()}] Bounce confirmed at {ltp:.2f}! "
                            f"Vol={latest_bar.volume} vs Avg={avg_vol:.0f}"
                        )
                        buy_signals.append({
                            "symbol":    w.symbol,
                            "timestamp": dt_now,
                            "vwap":      vwap,
                            "ltp":       ltp,
                            "z_score":   stats.z_score,
                            "volume":    latest_bar.volume,
                            "event":     "ANTIGRAVITY_BOUNCE_CONFIRMED",
                            "strategy":  "ANTIGRAVITY_LONG",
                            "note":      w.notes[-1],
                        })
                        self.remove_symbol(w.symbol, "Bounce confirmed.")

        return buy_signals
