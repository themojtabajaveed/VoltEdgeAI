"""
position_monitor.py
-------------------
Continuously monitors open positions for real-time risk events:
  1. Breaking news that could affect the stock
  2. Technical deterioration (VWAP breakdown, volume dying)
  3. Correlated stock movements (sector peer drops)
  4. Trade thesis invalidation

Returns alerts that the exit engine can act on.
"""
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class PositionAlert:
    symbol: str
    severity: str          # "URGENT" | "WARNING" | "INFO"
    alert_type: str        # "NEWS_RISK" | "TECH_DETERIORATION" | "SECTOR_DRAG" | "THESIS_INVALID"
    message: str
    should_exit: bool      # recommendation: exit immediately?
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class PositionMonitor:
    """
    Checks open positions for danger signals.
    Called periodically (every 1-2 minutes) by the runner.
    """

    def __init__(self, live_client=None, news_client=None):
        self.live_client = live_client
        self.news_client = news_client
        self._last_news_check: dict = {}  # symbol -> datetime

    def check_position(
        self,
        symbol: str,
        entry_price: float,
        side: str,             # "LONG" or "SHORT"
        ltp: float,
        intraday_bars: List = None,
    ) -> List[PositionAlert]:
        """
        Run all monitors for a given position and return any alerts.
        """
        alerts = []

        # 1. Momentum exhaustion check
        if intraday_bars and len(intraday_bars) >= 6:
            alert = self._check_exhaustion(symbol, side, intraday_bars)
            if alert:
                alerts.append(alert)

        # 2. VWAP breakdown check
        if intraday_bars and len(intraday_bars) >= 10:
            alert = self._check_vwap_breakdown(symbol, side, ltp, intraday_bars)
            if alert:
                alerts.append(alert)

        # 3. Volume dry-up check
        if intraday_bars and len(intraday_bars) >= 15:
            alert = self._check_volume_dryup(symbol, intraday_bars)
            if alert:
                alerts.append(alert)

        # 4. Excessive drawdown from peak
        if intraday_bars and len(intraday_bars) >= 5:
            alert = self._check_drawdown_from_peak(symbol, side, ltp, intraday_bars)
            if alert:
                alerts.append(alert)

        return alerts

    def _check_exhaustion(self, symbol: str, side: str, bars: List) -> Optional[PositionAlert]:
        """
        Detect momentum exhaustion:
        - Volume declining 3 consecutive bars
        - RSI-like overbought (for LONG) or oversold (for SHORT)
        """
        last_3_vols = [b.volume for b in bars[-3:]]
        vol_declining = all(last_3_vols[i] < last_3_vols[i-1] for i in range(1, 3))

        if not vol_declining:
            return None

        # Check if price is extended
        recent_highs = [b.high for b in bars[-6:]]
        recent_lows = [b.low for b in bars[-6:]]
        ltp = bars[-1].close

        if side == "LONG":
            # Are we at the top of the range with dying volume?
            rng = max(recent_highs) - min(recent_lows)
            if rng > 0 and (ltp - min(recent_lows)) / rng > 0.85:
                return PositionAlert(
                    symbol=symbol,
                    severity="WARNING",
                    alert_type="TECH_DETERIORATION",
                    message=f"Momentum exhaustion: volume declining 3 bars while at range highs",
                    should_exit=False,
                )
        elif side == "SHORT":
            rng = max(recent_highs) - min(recent_lows)
            if rng > 0 and (ltp - min(recent_lows)) / rng < 0.15:
                return PositionAlert(
                    symbol=symbol,
                    severity="WARNING",
                    alert_type="TECH_DETERIORATION",
                    message=f"Selling exhaustion: volume declining 3 bars while at range lows",
                    should_exit=False,
                )

        return None

    def _check_vwap_breakdown(self, symbol: str, side: str, ltp: float, bars: List) -> Optional[PositionAlert]:
        """
        For LONG: if price breaks below VWAP decisively, it's a warning.
        For SHORT: if price breaks above VWAP decisively, it's a warning.
        """
        # Simple VWAP calc
        cum_pv = sum((b.high + b.low + b.close) / 3.0 * b.volume for b in bars)
        cum_vol = sum(b.volume for b in bars)
        if cum_vol == 0:
            return None
        vwap = cum_pv / cum_vol

        if side == "LONG" and ltp < vwap * 0.995:
            # Closed 2 consecutive bars below VWAP?
            if len(bars) >= 2 and bars[-1].close < vwap and bars[-2].close < vwap:
                return PositionAlert(
                    symbol=symbol,
                    severity="WARNING",
                    alert_type="TECH_DETERIORATION",
                    message=f"VWAP breakdown: LTP={ltp:.2f} < VWAP={vwap:.2f}, 2 bars below",
                    should_exit=False,
                )
        elif side == "SHORT" and ltp > vwap * 1.005:
            if len(bars) >= 2 and bars[-1].close > vwap and bars[-2].close > vwap:
                return PositionAlert(
                    symbol=symbol,
                    severity="WARNING",
                    alert_type="TECH_DETERIORATION",
                    message=f"VWAP reclaim: LTP={ltp:.2f} > VWAP={vwap:.2f}, short under pressure",
                    should_exit=False,
                )
        return None

    def _check_volume_dryup(self, symbol: str, bars: List) -> Optional[PositionAlert]:
        """
        If the last 5 bars have significantly less volume than the prior 10,
        the move is losing conviction.
        """
        recent_vol = sum(b.volume for b in bars[-5:]) / 5
        older_vol = sum(b.volume for b in bars[-15:-5]) / 10

        if older_vol > 0 and recent_vol / older_vol < 0.4:
            return PositionAlert(
                symbol=symbol,
                severity="INFO",
                alert_type="TECH_DETERIORATION",
                message=f"Volume dry-up: recent avg {recent_vol:.0f} vs prior {older_vol:.0f} ({recent_vol/older_vol:.0%})",
                should_exit=False,
            )
        return None

    def _check_drawdown_from_peak(self, symbol: str, side: str, ltp: float, bars: List) -> Optional[PositionAlert]:
        """
        If we've given back more than 60% of the peak unrealized gain,
        momentum is fading fast.
        """
        if side == "LONG":
            peak = max(b.high for b in bars)
            trough = bars[0].open  # approximate entry
            if peak > trough:
                total_move = peak - trough
                given_back = peak - ltp
                if total_move > 0 and given_back / total_move > 0.6:
                    return PositionAlert(
                        symbol=symbol,
                        severity="WARNING",
                        alert_type="TECH_DETERIORATION",
                        message=f"Given back {given_back/total_move:.0%} of peak move (peak={peak:.2f}, now={ltp:.2f})",
                        should_exit=False,
                    )
        elif side == "SHORT":
            trough = min(b.low for b in bars)
            peak_entry = bars[0].open
            if peak_entry > trough:
                total_move = peak_entry - trough
                given_back = ltp - trough
                if total_move > 0 and given_back / total_move > 0.6:
                    return PositionAlert(
                        symbol=symbol,
                        severity="WARNING",
                        alert_type="TECH_DETERIORATION",
                        message=f"Short given back {given_back/total_move:.0%} of drop",
                        should_exit=False,
                    )
        return None
