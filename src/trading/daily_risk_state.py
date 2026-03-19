from dataclasses import dataclass, field
from datetime import date

@dataclass
class DailyRiskState:
    trading_date: date
    trades_taken: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    def reset_for_new_day(self, new_date: date) -> None:
        self.trading_date = new_date
        self.trades_taken = 0
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0

    @property
    def total_loss(self) -> float:
        return min(0.0, self.realized_pnl + self.unrealized_pnl)
 