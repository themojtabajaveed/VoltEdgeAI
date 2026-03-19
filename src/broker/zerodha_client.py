import os
from typing import Dict, Optional
from kiteconnect import KiteConnect

def get_kite_client(access_token: Optional[str] = None) -> KiteConnect:
    """
    Create and return a KiteConnect client using KITE_API_KEY and an access token.
    Assumes I have already obtained a valid access token manually.
    """
    api_key = os.environ["KITE_API_KEY"]
    kite = KiteConnect(api_key=api_key)
    
    if access_token is None:
        access_token = os.environ["KITE_ACCESS_TOKEN"]
        
    kite.set_access_token(access_token)
    return kite

def format_order_from_plan(plan: Dict, side: str = "BUY") -> Dict:
    """Convert a trade plan dict into a Zerodha order payload dict."""
    return {
        "tradingsymbol": plan["symbol"],
        "exchange": "NSE",
        "transaction_type": side,
        "quantity": plan["qty"],
        "price": round(plan["entry"], 2),
        "product": "CNC",
        "order_type": "LIMIT",
        "stop_loss": plan["stop_loss"],
        "target": plan["target"]
    }

def place_equity_trade_from_plan(plan: Dict, side: str = "BUY", dry_run: bool = True) -> None:
    """
    In dry_run mode: print the order that WOULD be sent to Zerodha.
    When dry_run=False, actually submit the order using KiteConnect.
    """
    order_params = format_order_from_plan(plan, side)
    
    if dry_run:
        print("\n[DRY-RUN] Zerodha order:")
        for key, value in order_params.items():
            print(f"  {key}: {value}")
    else:
        kite = get_kite_client()
        try:
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=order_params["exchange"],
                tradingsymbol=order_params["tradingsymbol"],
                transaction_type=order_params["transaction_type"],
                quantity=order_params["quantity"],
                order_type=order_params["order_type"],
                product=order_params["product"],
                price=order_params["price"]
            )
            print(f"  [SUCCESS] Order placed. ID: {order_id}")
        except Exception as e:
            print(f"  [ERROR] Failed to place order: {e}")
