from typing import Optional, Dict
import math
from sources.nse_prices import fetch_daily_ohlcv, compute_atr

def plan_trade(
    symbol: str,
    equity: float,
    risk_pct: float = 0.02,
    atr_period: int = 14,
    atr_mult: float = 1.5,
    rr: float = 2.0,
) -> Optional[Dict]:
    """
    Build a simple trade plan for an NSE symbol using daily ATR.
    Returns a dict with keys:
    - symbol, entry, stop_loss, target, atr, risk_per_share, qty,
      risk_amount, reward_amount, rr_effective
    or None if price data / ATR are unavailable.
    """
    df = fetch_daily_ohlcv(symbol, days=atr_period * 4)
    if df is None or df.empty:
        return None

    high = df['high']
    low = df['low']
    close = df['close']

    atr = compute_atr(high, low, close, period=atr_period)
    
    if atr is None or atr <= 0:
        return None

    entry = float(close.iloc[-1])
    
    stop_dist = atr_mult * atr
    stop_loss = entry - stop_dist
    
    risk_per_share = entry - stop_loss
    
    if risk_per_share <= 0:
        return None
        
    risk_amount = equity * risk_pct
    qty = math.floor(risk_amount / risk_per_share)
    
    if qty <= 0:
        return None
        
    target = entry + rr * risk_per_share
    reward_amount = (target - entry) * qty
    rr_effective = reward_amount / risk_amount if risk_amount > 0 else 0.0

    target1 = target
    target1_qty = qty // 2
    runner_qty = qty - target1_qty

    return {
        "symbol": symbol,
        "entry": float(entry),
        "stop_loss": float(stop_loss),
        "target": float(target),
        "target1": float(target1),
        "target1_qty": int(target1_qty),
        "runner_qty": int(runner_qty),
        "breakeven_price": float(entry),
        "trail_method": "20EMA_or_ATR",
        "atr": float(atr),
        "risk_per_share": float(risk_per_share),
        "qty": int(qty),
        "risk_amount": float(risk_amount),
        "reward_amount": float(reward_amount),
        "rr_effective": float(rr_effective)
    }

def main():
    print("--- VoltEdgeAI Trade Planner ---")
    symbol = input("Enter NSE symbol (e.g., ESAFSFB): ").strip()
    
    try:
        equity_input = input("Enter account equity in INR (e.g., 100000): ").strip()
        equity = float(equity_input)
    except ValueError:
        print("Invalid equity amount. Please enter a number.")
        return
        
    print(f"\nPlanning trade for {symbol} with ₹{equity:,.2f} equity (2% risk)...\n")
    
    plan = plan_trade(symbol=symbol, equity=equity)
    
    if plan is None:
        print(f"Could not generate trade plan for {symbol}. Insufficient data or logic error.")
    else:
        print("--- Trade Plan Summary ---")
        print(f"Symbol:         {plan['symbol']}")
        print(f"Entry Price:    ₹{plan['entry']:.2f}")
        print(f"Stop Loss:      ₹{plan['stop_loss']:.2f} (Dist: ₹{plan['risk_per_share']:.2f})")
        print(f"Target:         ₹{plan['target']:.2f}")
        print(f"ATR (14):       ₹{plan['atr']:.2f}")
        print("--------------------------")
        print(f"Quantity:       {plan['qty']} shares")
        print(f"Target 1 Qty:   {plan['target1_qty']} shares (at ₹{plan['target1']:.2f})")
        print(f"Runner Qty:     {plan['runner_qty']} shares")
        print(f"Trail Method:   {plan['trail_method']} (Breakeven: ₹{plan['breakeven_price']:.2f})")
        print("--------------------------")
        print(f"Risk Amount:    ₹{plan['risk_amount']:.2f}")
        print(f"Reward Amount:  ₹{plan['reward_amount']:.2f}")
        print(f"Effective R:R:  {plan['rr_effective']:.2f}")

if __name__ == "__main__":
    main()
