import os
import sys
import logging
from dotenv import load_dotenv


def _startup_warmup(max_wait: int = 60) -> None:
    """Wait for network connectivity + check Kite token before entering main loop."""
    import time
    import json
    import subprocess

    # 1. Network reachability check (ping 8.8.8.8)
    start = time.time()
    network_ok = False
    while time.time() - start < max_wait:
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "3", "8.8.8.8"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                elapsed = time.time() - start
                print(f"[Startup] Network OK ({elapsed:.0f}s)")
                network_ok = True
                break
        except Exception:
            pass
        time.sleep(5)

    if not network_ok:
        print(f"[Startup] WARNING: Network unreachable after {max_wait}s — continuing anyway")

    # 2. Kite token validity check
    try:
        token_path = os.path.join("cache", "token_status.json")
        with open(token_path) as f:
            ts = json.load(f)
        status = ts.get("status", "unknown")
        if status == "valid":
            print(f"[Startup] Kite token valid (refreshed: {ts.get('timestamp', '?')})")
        else:
            print(
                f"[Startup] WARNING: Kite token {status}. "
                f"Brief will run with incomplete data. Manual token refresh required."
            )
    except FileNotFoundError:
        print("[Startup] WARNING: token_status.json not found. Kite data will be unavailable.")
    except Exception as e:
        print(f"[Startup] WARNING: Token check failed: {e}")


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

    # Startup warmup: wait for network + verify Kite token
    _startup_warmup(max_wait=60)

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
