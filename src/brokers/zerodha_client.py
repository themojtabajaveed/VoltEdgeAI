import os
from typing import Optional
import logging

import requests
from requests.adapters import HTTPAdapter
from kiteconnect import KiteConnect, exceptions
from src.trading.orders import OrderRequest, OrderResult, OrderSide, OrderType


def _make_kite_session() -> requests.Session:
    """
    Optimisation 2 — Persistent HTTP session for Zerodha order execution.

    A fresh TCP+TLS handshake costs 15-30ms per order. By reusing an existing
    connection (HTTP keep-alive + connection pooling) we eliminate this overhead
    on every subsequent call.

    HTTPAdapter settings:
      pool_connections=1  — single connection pool (we only call one host)
      pool_maxsize=4      — up to 4 concurrent sockets (enough for burst orders)
      max_retries=1       — one automatic retry on transient network errors
    """
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=1,
        pool_maxsize=4,
        max_retries=1,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # Conservative timeouts: 10s connect, 15s read (order MUST complete in 15s)
    session.timeout = (10, 15)
    return session


class ZerodhaClient:
    def __init__(self, api_key: str, access_token: str):
        self._kite = KiteConnect(api_key=api_key)
        self._kite.set_access_token(access_token)
        # Optimisation 2 — reuse TCP/TLS connection across order calls (saves 15-30ms each)
        self._kite.reqsession = _make_kite_session()
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
                product=self._kite.PRODUCT_MIS,   # MIS = Margin Intraday Square-off (required for shorting + auto-squareoff)
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
