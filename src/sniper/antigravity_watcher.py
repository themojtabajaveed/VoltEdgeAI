from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from marketdata.intraday import IntradayBar, VWAPStats, fetch_intraday_bars, compute_vwap_stats

VWAP_TOUCH_BAND = 0.002  # 0.2%
MAX_WATCH_DURATION_MIN = 90  # e.g., stop watching after 90 minutes if no bounce

class WatchState(str, Enum):
    WAITING_FOR_GRAVITY = "WAITING_FOR_GRAVITY"  # price stretched above VWAP
    WAITING_FOR_VWAP_TOUCH = "WAITING_FOR_VWAP_TOUCH"
    WAITING_FOR_BOUNCE = "WAITING_FOR_BOUNCE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

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
        # symbol -> WatchedSymbol
        self._watched: Dict[str, WatchedSymbol] = {}

    def add_wait_signal(self, symbol: str, z_score: float, vwap: float, ltp: float, now: Optional[datetime] = None) -> None:
        """
        Register a symbol that Sniper marked as WAIT because Z-score was too high.
        If the symbol is already being watched, update its notes but don't reset state.
        """
        dt_now = now or datetime.now()
        if symbol in self._watched:
            active_state = self._watched[symbol].state
            if active_state not in (WatchState.COMPLETED, WatchState.CANCELLED):
                self._watched[symbol].notes.append(f"[{dt_now.isoformat()}] Received another WAIT signal (Z={z_score:.2f}).")
                return
                
        # Fresh registration or overriding a closed state
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
        """Mark a symbol as COMPLETED or CANCELLED and remove it from active watchlist."""
        if symbol in self._watched:
            self._watched[symbol].state = WatchState.CANCELLED
            self._watched[symbol].notes.append(f"Removed: {reason}")
            # Could pop it, but leaving it mutated allows introspection if needed externally before cleanup.
            # To strictly remove from active iteration:
            self._watched.pop(symbol, None)

    def get_active_symbols(self) -> List[WatchedSymbol]:
        """Return currently watched symbols (not COMPLETED/CANCELLED)."""
        return [
            w for w in self._watched.values() 
            if w.state not in (WatchState.COMPLETED, WatchState.CANCELLED)
        ]

    def _is_vwap_touch(self, ltp: float, vwap: float) -> bool:
        """Detect if price is within a strict VWAP band."""
        if vwap == 0:
            return False
        return abs(ltp - vwap) / vwap <= VWAP_TOUCH_BAND

    def tick(self, now: Optional[datetime] = None) -> List[dict]:
        """
        Called periodically (e.g., every few minutes) during market hours.
        Returns a list of BUY_SIGNAL event dicts for confirmed bounces.
        """
        dt_now = now or datetime.now()
        buy_signals = []
        active_symbols = self.get_active_symbols()

        for w in active_symbols:
            w.last_checked_at = dt_now
            
            # 0. Check timeout
            duration_mins = (dt_now - w.created_at).total_seconds() / 60.0
            if duration_mins > MAX_WATCH_DURATION_MIN:
                w.state = WatchState.CANCELLED
                w.notes.append(f"[{dt_now.isoformat()}] Watch timeout: exceeded {MAX_WATCH_DURATION_MIN} minutes without confirmed bounce.")
                self.remove_symbol(w.symbol, f"Timeout after {MAX_WATCH_DURATION_MIN} mins.")
                continue
            
            # 1. Fetch latest intraday bars (5m interval is best for bounce confirmation)
            try:
                bars = fetch_intraday_bars(w.symbol, interval="5m")
            except Exception as e:
                w.notes.append(f"[{dt_now.isoformat()}] Error fetching data: {e}")
                continue
                
            if not bars or len(bars) < 2:
                continue
                
            # 2. Compute stats
            stats = compute_vwap_stats(bars, interval="5m")
            if not stats:
                continue
                
            ltp = stats.ltp
            vwap = stats.vwap
            latest_bar = bars[-1]
            
            # 3. State transitions
            
            # Phase 1 -> 2: Waiting for Gravity to pull it down to VWAP
            if w.state == WatchState.WAITING_FOR_GRAVITY:
                # Did it touch the 0.2% band?
                if self._is_vwap_touch(ltp, vwap):
                    w.state = WatchState.WAITING_FOR_BOUNCE
                    w.notes.append(f"[{dt_now.isoformat()}] VWAP touch detected at {ltp} (VWAP={vwap:.2f})")
                # Did it collapse completely through without us catching the touch?
                elif ltp < (vwap * 0.995): # deeply below VWAP
                    w.state = WatchState.CANCELLED
                    w.notes.append(f"[{dt_now.isoformat()}] Price collapsed below VWAP cleanly to {ltp}. Bear control.")
                    self.remove_symbol(w.symbol, "Price fell below VWAP without hovering.")
                    
            # Phase 2 -> 3: Waiting for the green volume bounce
            elif w.state == WatchState.WAITING_FOR_BOUNCE:
                # Check for collapse during wait
                if ltp < (vwap * 0.995):
                    w.state = WatchState.CANCELLED
                    w.notes.append(f"[{dt_now.isoformat()}] Support failed. Price fell below VWAP to {ltp}.")
                    self.remove_symbol(w.symbol, "Price fell below VWAP (bear control).")
                    continue

                # We need at least a few bars to check average volume
                if len(bars) >= 4:
                    # Is the current 5m candle green, closing above VWAP?
                    is_green = latest_bar.close > latest_bar.open
                    is_above_vwap = latest_bar.close > vwap
                    
                    if is_green and is_above_vwap:
                        # Simple proxy for "volume > sum of red candles":
                        # Is this green candle's volume significantly higher than recent average?
                        recent_vols = [b.volume for b in bars[-4:-1]]
                        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
                        
                        if latest_bar.volume > (avg_vol * 1.25): # 25% larger than recent averages
                            # BOUNCE CONFIRMED!
                            w.state = WatchState.COMPLETED
                            w.notes.append(f"[{dt_now.isoformat()}] Bounce confirmed at {ltp}! Vol: {latest_bar.volume} vs Avg {avg_vol:.0f}")
                            
                            buy_signals.append({
                                "symbol": w.symbol,
                                "timestamp": dt_now,
                                "vwap": vwap,
                                "ltp": ltp,
                                "z_score": stats.z_score,
                                "volume": latest_bar.volume,
                                "event": "ANTIGRAVITY_BOUNCE_CONFIRMED",
                                "note": w.notes[-1]
                            })
                            # Remove from active tracking since we emitted the buy.
                            self.remove_symbol(w.symbol, "Bounce confirmed.")

        return buy_signals
