import os
import sys
import logging
from dotenv import load_dotenv

def main():
    load_dotenv()
    
    live_mode = os.getenv("VOLTEDGE_LIVE_MODE", "0") == "1"
    
    try:
        per_trade_capital = int(float(os.getenv("VOLTEDGE_PER_TRADE_CAPITAL", "300")))
    except ValueError:
        per_trade_capital = 300
        
    try:
        max_trades = int(os.getenv("VOLTEDGE_MAX_TRADES_PER_DAY", "3"))
    except ValueError:
        max_trades = 3

    print(f"Starting VoltEdge (LIVE={live_mode}, CAPITAL={per_trade_capital}, MAX_TRADES={max_trades})")
    
    from src.runner import run_loop

    try:
        run_loop(live_mode=live_mode, per_trade_capital=per_trade_capital, max_trades_per_day=max_trades)
    except KeyboardInterrupt:
        print("\n[VoltEdge] Stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logging.exception(f"Fatal error in main loop: {e}")
        print(f"\n[VoltEdge] Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
