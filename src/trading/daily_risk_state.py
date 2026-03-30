"""
daily_risk_state.py (v2)
------------------------
Tracks daily trading budget and P&L.

v2 changes:
  - All fields that accumulate money now use Decimal internally to prevent
    float drift across many trades in a session.
  - threading.RLock added: ExitMonitorThread updates daily_pnl on exit;
    main runner thread reads it for loss-cap checks. Concurrent access
    without locking would produce silent data races.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import threading


_TWO_DP = Decimal("0.01")


@dataclass
class DailyRiskState:
    trading_date: date
    trades_taken: int = 0
    _realized_pnl: Decimal = field(default_factory=lambda: Decimal("0.00"), repr=False)
    _unrealized_pnl: Decimal = field(default_factory=lambda: Decimal("0.00"), repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── Human-friendly float properties ──────────────────────────────────────

    @property
    def realized_pnl(self) -> float:
        with self._lock:
            return float(self._realized_pnl)

    @realized_pnl.setter
    def realized_pnl(self, value: float) -> None:
        with self._lock:
            self._realized_pnl = Decimal(str(value)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)

    @property
    def unrealized_pnl(self) -> float:
        with self._lock:
            return float(self._unrealized_pnl)

    @unrealized_pnl.setter
    def unrealized_pnl(self, value: float) -> None:
        with self._lock:
            self._unrealized_pnl = Decimal(str(value)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)

    # ── Preferred: add_pnl() instead of reassigning to preserve precision ────

    def add_realized_pnl(self, amount: float) -> None:
        """Thread-safe accumulation. Preferred over direct assignment."""
        with self._lock:
            self._realized_pnl += Decimal(str(amount)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)

    @property
    def daily_pnl(self) -> float:
        """Convenience alias: realized + unrealized (used by runner loss-cap)."""
        with self._lock:
            return float(self._realized_pnl + self._unrealized_pnl)

    @daily_pnl.setter
    def daily_pnl(self, value: float) -> None:
        """
        Legacy setter: runner.py assigns risk_state.daily_pnl = X directly.
        Treats the assigned value as the new realized_pnl total.
        Prefer add_realized_pnl() for new code.
        """
        with self._lock:
            self._realized_pnl = Decimal(str(value)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)

    def reset_for_new_day(self, new_date: date) -> None:
        with self._lock:
            self.trading_date = new_date
            self.trades_taken = 0
            self._realized_pnl = Decimal("0.00")
            self._unrealized_pnl = Decimal("0.00")

    @property
    def total_loss(self) -> float:
        with self._lock:
            total = self._realized_pnl + self._unrealized_pnl
            return float(min(Decimal("0.00"), total))