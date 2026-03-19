from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import List, Dict, Optional

from trading.positions import PositionBook, Position
from config.risk import RiskConfig
from data_ingestion.market_live import KiteLiveClient

@dataclass
class ExitSignal:
    symbol: str
    side: str            # always "SELL" for now
    reason: str          # "STOP_LOSS" | "TIME_EXIT"
    ltp: float
    stop_price: Optional[float]
    mode: str
    strategy: str

class ExitEngine:
    def __init__(
        self,
        positions: PositionBook,
        live_client: KiteLiveClient,
        risk: RiskConfig,
    ) -> None:
        self.positions = positions
        self.live_client = live_client
        self.risk = risk
        self._exit_time = self._parse_intraday_exit_time()

    def _parse_intraday_exit_time(self) -> dt_time:
        """Parse 'HH:MM' string to datetime.time object."""
        try:
            hour_str, minute_str = self.risk.intraday_exit_time.split(":")
            return dt_time(int(hour_str), int(minute_str))
        except (ValueError, TypeError):
            # Safe fallback if config is broken
            return dt_time(15, 20)

    def tick(self, now: datetime) -> List[ExitSignal]:
        """Check all open positions and emit exit signals when:
        - LTP <= position.initial_stop_price (if set)
        - Or now >= intraday_exit_time for INTRADAY positions.
        """
        signals = []
        current_time = now.time()
        
        for pos in self.positions.get_open_positions():
            tick = self.live_client.get_last_tick(pos.symbol)
            if not tick:
                continue
                
            ltp = tick.ltp
            stop_price = pos.initial_stop_price
            
            # End of day time exit constraints
            if pos.mode == "INTRADAY" and current_time >= self._exit_time:
                signals.append(ExitSignal(
                    symbol=pos.symbol,
                    side="SELL",
                    reason="TIME_EXIT",
                    ltp=ltp,
                    stop_price=stop_price,
                    mode=pos.mode,
                    strategy=pos.strategy
                ))
            # Immediate stop-loss break
            elif stop_price is not None and ltp <= stop_price:
                signals.append(ExitSignal(
                    symbol=pos.symbol,
                    side="SELL",
                    reason="STOP_LOSS",
                    ltp=ltp,
                    stop_price=stop_price,
                    mode=pos.mode,
                    strategy=pos.strategy
                ))
                
        return signals
