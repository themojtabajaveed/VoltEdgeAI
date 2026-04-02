"""
executor.py (v2)
----------------
Handles LONG entry (execute_buy), LONG exit (execute_sell),
SHORT entry (execute_short_sell), and SHORT exit (execute_short_cover).
All methods respect DRY_RUN / LIVE_MODE from RiskConfig.
"""
import logging
from datetime import datetime
from typing import Optional

from src.config.risk import RiskConfig
from src.trading.orders import OrderRequest, OrderResult, OrderSide, OrderType
from src.trading.daily_risk_state import DailyRiskState
from src.trading.sizing import calculate_position_size, SymbolStats, MarketRegime
from src.data_ingestion.short_ban_list import is_safe_to_short

logger = logging.getLogger(__name__)

try:
    from src.brokers.zerodha_client import ZerodhaClient
except ImportError:
    ZerodhaClient = None


class TradeExecutor:
    def __init__(self, risk: RiskConfig, daily_state: DailyRiskState):
        self.risk = risk
        self.daily_state = daily_state
        self._zerodha: Optional[ZerodhaClient] = None
        if self.risk.live_mode and ZerodhaClient is not None:
            try:
                self._zerodha = ZerodhaClient.from_env()
            except Exception:
                self._zerodha = None

    # ── LONG entry ────────────────────────────────────────────────────────────
    def execute_buy(
        self,
        symbol: str,
        ltp: float,
        qty: int,
        market_regime: Optional[MarketRegime] = None,
        symbol_stats: Optional[SymbolStats] = None,
    ) -> OrderResult:
        """Execute a LONG entry. qty is already computed from ATR-based sizing."""
        if ltp <= 0 or qty <= 0:
            return OrderResult(
                success=False, broker_order_id=None,
                message=f"Invalid ltp={ltp} or qty={qty} for {symbol}", filled_qty=0
            )

        if not self.risk.live_mode or self._zerodha is None:
            msg = f"DRY_RUN BUY: {qty} × {symbol} @ ~{ltp:.2f}"
            logger.info(msg)
            return OrderResult(success=True, broker_order_id=None, message=msg,
                               filled_qty=qty, avg_price=ltp)
        try:
            req = OrderRequest(symbol=symbol, side=OrderSide.BUY, quantity=qty,
                               order_type=OrderType.MARKET, timestamp=datetime.now())
            return self._zerodha.place_equity_order(req)
        except Exception as e:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"BUY crash {symbol}: {e}", filled_qty=0)

    # ── LONG exit ─────────────────────────────────────────────────────────────
    def execute_sell(self, symbol: str, qty: int, ltp: float) -> OrderResult:
        """Exit a LONG position."""
        if qty <= 0 or ltp <= 0:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"Invalid qty={qty} or ltp={ltp}", filled_qty=0)

        if not self.risk.live_mode or self._zerodha is None:
            msg = f"DRY_RUN SELL: {qty} × {symbol} @ ~{ltp:.2f}"
            logger.info(msg)
            return OrderResult(success=True, broker_order_id=None, message=msg,
                               filled_qty=qty, avg_price=ltp)
        try:
            req = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=qty,
                               order_type=OrderType.MARKET, timestamp=datetime.now())
            return self._zerodha.place_equity_order(req)
        except Exception as e:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"SELL crash {symbol}: {e}", filled_qty=0)

    # ── SHORT entry ───────────────────────────────────────────────────────────
    def execute_short_sell(
        self,
        symbol: str,
        ltp: float,
        qty: int,
    ) -> OrderResult:
        """
        Enter a SHORT position by selling shares we don't own (sell-to-open).
        Requires the broker to support intraday shorting (Kite supports MIS orders).
        """
        if ltp <= 0 or qty <= 0:
            return OrderResult(
                success=False, broker_order_id=None,
                message=f"Invalid ltp={ltp} or qty={qty} for SHORT {symbol}", filled_qty=0
            )

        # SHORT-6/7: Ban list + T2T gate
        if not is_safe_to_short(symbol):
            msg = f"SHORT blocked by ban list / T2T restriction: {symbol}"
            logger.warning(msg)
            return OrderResult(success=False, broker_order_id=None, message=msg, filled_qty=0)

        if not self.risk.live_mode or self._zerodha is None:
            msg = f"DRY_RUN SHORT_SELL: {qty} × {symbol} @ ~{ltp:.2f}"
            logger.info(msg)
            return OrderResult(success=True, broker_order_id=None, message=msg,
                               filled_qty=qty, avg_price=ltp)
        try:
            # SHORT SELL = place a SELL order on a stock without holding it (MIS intraday)
            req = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=qty,
                               order_type=OrderType.MARKET, timestamp=datetime.now())
            return self._zerodha.place_equity_order(req)
        except Exception as e:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"SHORT_SELL crash {symbol}: {e}", filled_qty=0)

    # ── SHORT exit (buy-to-cover) ─────────────────────────────────────────────
    def execute_short_cover(self, symbol: str, qty: int, ltp: float) -> OrderResult:
        """
        Close a SHORT position by buying back (buy-to-cover).
        """
        if qty <= 0 or ltp <= 0:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"Invalid qty={qty} or ltp={ltp} for COVER {symbol}",
                               filled_qty=0)

        if not self.risk.live_mode or self._zerodha is None:
            msg = f"DRY_RUN SHORT_COVER: {qty} × {symbol} @ ~{ltp:.2f}"
            logger.info(msg)
            return OrderResult(success=True, broker_order_id=None, message=msg,
                               filled_qty=qty, avg_price=ltp)
        try:
            req = OrderRequest(symbol=symbol, side=OrderSide.BUY, quantity=qty,
                               order_type=OrderType.MARKET, timestamp=datetime.now())
            return self._zerodha.place_equity_order(req)
        except Exception as e:
            return OrderResult(success=False, broker_order_id=None,
                               message=f"SHORT_COVER crash {symbol}: {e}", filled_qty=0)
