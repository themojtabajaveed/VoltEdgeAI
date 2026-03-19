import os
from typing import Optional
import logging

from kiteconnect import KiteConnect, exceptions
from trading.orders import OrderRequest, OrderResult, OrderSide, OrderType

class ZerodhaClient:
    def __init__(self, api_key: str, access_token: str):
        self._kite = KiteConnect(api_key=api_key)
        self._kite.set_access_token(access_token)
        self.logger = logging.getLogger(__name__)

    @classmethod
    def from_env(cls) -> "ZerodhaClient":
        api_key = os.getenv("ZERODHA_API_KEY", "")
        access_token = os.getenv("ZERODHA_ACCESS_TOKEN", "")
        if not api_key or not access_token:
            raise ValueError("ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN must be set in environment")
        return cls(api_key=api_key, access_token=access_token)

    def place_equity_order(self, req: OrderRequest) -> OrderResult:
        """
        Place a simple NSE equity order as MARKET / CNC.
        """
        try:
            # Map our internal OrderSide to Kite's
            side_map = {
                OrderSide.BUY: self._kite.TRANSACTION_TYPE_BUY,
                OrderSide.SELL: self._kite.TRANSACTION_TYPE_SELL
            }
            transaction_type = side_map.get(req.side)
            if not transaction_type:
                return OrderResult(success=False, broker_order_id=None, message=f"Invalid OrderSide: {req.side}")

            # Note: For intraday you might use PRODUCT_MIS. CNC is for delivery (cash n carry)
            order_id = self._kite.place_order(
                variety=self._kite.VARIETY_REGULAR,
                exchange=self._kite.EXCHANGE_NSE,
                tradingsymbol=req.symbol,
                transaction_type=transaction_type,
                quantity=req.quantity,
                product=self._kite.PRODUCT_CNC,
                order_type=self._kite.ORDER_TYPE_MARKET
            )
            return OrderResult(
                success=True,
                broker_order_id=str(order_id),
                message="Order placed successfully",
                filled_qty=0, # Assuming async fill; polling needed for exact fill
                avg_price=None 
            )

        except exceptions.KiteException as e:
            self.logger.error(f"Kite execution error for {req.symbol}: {str(e)}")
            return OrderResult(success=False, broker_order_id=None, message=f"Kite API error: {str(e)}")
        except Exception as e:
            self.logger.error(f"Unexpected execution error for {req.symbol}: {str(e)}")
            return OrderResult(success=False, broker_order_id=None, message=f"Unexpected error: {str(e)}")
