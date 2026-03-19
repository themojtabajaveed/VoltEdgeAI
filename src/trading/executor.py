import math
from datetime import datetime
from typing import Optional

from src.config.risk import RiskConfig
from src.trading.orders import OrderRequest, OrderResult, OrderSide, OrderType
from src.trading.daily_risk_state import DailyRiskState
from src.trading.sizing import calculate_position_size, SymbolStats, MarketRegime

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

    def execute_buy(
        self, 
        symbol: str, 
        ltp: float, 
        market_regime: Optional[MarketRegime] = None,
        symbol_stats: Optional[SymbolStats] = None,
    ) -> OrderResult:
        """
        Core entry point to execute a BUY according to risk config.
        - In DRY_RUN: simulate quantity based on per_trade_capital_rupees and log.
        - In LIVE_MODE: actually place a MARKET order with Zerodha.
        """
        # Calculate quantity based on capital risk rules and scaling bounds
        if ltp <= 0:
            return OrderResult(success=False, broker_order_id=None, message=f"Invalid LTP {ltp} for {symbol}", filled_qty=0)
            
        mr = market_regime or MarketRegime(trend="sideways", strength=0.0)
        ss = symbol_stats or SymbolStats(symbol=symbol, last_price=ltp, avg_daily_turnover_rupees=0.0)
        
        qty = calculate_position_size(ss, mr, self.risk)
        
        if qty <= 0:
            return OrderResult(success=False, broker_order_id=None, message=f"position size zero after sizing rules, skipping {symbol}", filled_qty=0)
        
        # Dry-run Execution Block
        if not self.risk.live_mode or self._zerodha is None:
            msg = f"DRY_RUN: would buy {qty} shares of {symbol} at LTP ~{ltp:.2f}"
            return OrderResult(
                success=True,
                broker_order_id=None,
                message=msg,
                filled_qty=qty,
                avg_price=ltp
            )
            
        # Live Execution Block
        try:
            req = OrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=qty,
                order_type=OrderType.MARKET,
                timestamp=datetime.now()
            )
            return self._zerodha.place_equity_order(req)
        except Exception as e:
            return OrderResult(
                success=False,
                broker_order_id=None,
                message=f"Executor crash handling {symbol}: {str(e)}",
                filled_qty=0
            )

    def execute_sell(self, symbol: str, qty: int, ltp: float) -> OrderResult:
        """
        Execute a SELL order (to close or reduce position).
        - In DRY_RUN: simulate order at LTP.
        """
        if qty <= 0 or ltp <= 0:
            return OrderResult(success=False, broker_order_id=None, message=f"Invalid qty {qty} or LTP {ltp}", filled_qty=0)
            
        # Dry-run Execution Block
        if not self.risk.live_mode or self._zerodha is None:
            msg = f"DRY_RUN: would sell {qty} shares of {symbol} at LTP ~{ltp:.2f}"
            return OrderResult(
                success=True,
                broker_order_id=None,
                message=msg,
                filled_qty=qty,
                avg_price=ltp
            )
            
        # Live Execution Block
        try:
            req = OrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                quantity=qty,
                order_type=OrderType.MARKET, # market order for fast exit for now
                timestamp=datetime.now()
            )
            return self._zerodha.place_equity_order(req)
        except Exception as e:
            return OrderResult(
                success=False,
                broker_order_id=None,
                message=f"Executor crash handling SELL {symbol}: {str(e)}",
                filled_qty=0
            )
