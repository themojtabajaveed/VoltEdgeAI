import os
from dotenv import load_dotenv
load_dotenv()

import time
from datetime import datetime, timedelta
import logging

# Inject dummy test constraints quickly to enable dry-running without actual keys natively
if "ZERODHA_API_KEY" not in os.environ:
    os.environ["ZERODHA_API_KEY"] = "stub_key"
if "ZERODHA_ACCESS_TOKEN" not in os.environ:
    os.environ["ZERODHA_ACCESS_TOKEN"] = "stub_token"

from src.data_ingestion.market_live import KiteLiveClient, BarBuilder, Tick
from src.data_ingestion.market_history import get_ohlcv
from src.data_ingestion.corporate_actions import NSEAnnouncementsClient
from src.data_ingestion.market_sentiment import compute_index_sentiment

logging.basicConfig(level=logging.ERROR) # Suppress debug chatter for clean demo output

def main():
    print("=== VoltEdgeAI Data Ingestion Examples ===")
    
    # 1. Corporate Announcements
    print("\n--- 1. Corporate Announcements ---")
    nse_client = NSEAnnouncementsClient()
    announcements = nse_client.fetch_recent(limit=3)
    if announcements:
        for a in announcements:
            print(f"[{a.announced_at}] {a.symbol}: {a.headline[:60]}... (URL: {a.url})")
    else:
        print("No announcements fetched (or operating in disabled stub mode).")

    # 2. Market Live (WebSocket & Quote API)
    print("\n--- 2. Market Live (WebSocket & Quote API) ---")
    live_client = KiteLiveClient()
    
    # Inject an example mapping: "RELIANCE" -> 738561
    live_client.set_instrument_mapping({"RELIANCE": 738561})
    
    # Start stream (in background thread)
    live_client.start_websocket()
    time.sleep(1) # wait a moment for connection logs
    
    # Subscribe explicitly
    live_client.subscribe_symbols(["RELIANCE"])
    
    # Get Snapshot via Quote API
    snaps = live_client.get_snapshot(["RELIANCE"])
    if "RELIANCE" in snaps:
        print(f"Snapshot RELIANCE: LTP {snaps['RELIANCE'].ltp}, Vol {snaps['RELIANCE'].volume}")
    else:
        # Expected behavior during stub execution!
        print("Could not fetch Snapshot (running perfectly in safe dry-run/stub mode without real Zerodha credentials).")
    
    # 3. BarBuilder Usage
    print("\n--- 3. BarBuilder Usage (Simulated Ticks) ---")
    builder = BarBuilder(interval="1m")
    
    # Simulate an incoming tick dynamically
    sim_tick = Tick(
        symbol="RELIANCE", 
        instrument_token=738561, 
        ltp=2500.0, 
        volume=100, 
        timestamp=datetime.now()
    )
    bars = builder.on_tick(sim_tick)
    current_bar = builder.get_current_bar("RELIANCE")
    print(f"Current dynamically accumulated Bar for RELIANCE: {current_bar}")
    
    # 4. Market Sentiment
    print("\n--- 4. Index Sentiment ---")
    sent = compute_index_sentiment(live_client, "NIFTY 50", ["NIFTY24DEC21000CE", "NIFTY24DEC21000PE"])
    print(f"Index Sentiment: {sent.trend} (Strength {sent.strength:.2f}) - {sent.comment}")

    # 5. Market History
    print("\n--- 5. Market History (SQLite Cache + Kite Backfill) ---")
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=5)
    
    try:
        # in this example, `.kite` is None if credentials are stubbed, returning empty explicitly efficiently protecting constraints.
        df_hist = get_ohlcv(symbol="RELIANCE", instrument_token=738561, interval="15m", start=start_dt, end=end_dt, kite_client=live_client.kite)
        if not df_hist.empty:
            print(f"Fetched {len(df_hist)} rows of OHLCV history.")
            print(df_hist.head(2))
        else:
            print("History returned cleanly empty (expected when running strict stub API limit testing!).")
    except Exception as e:
        print(f"History fetch error: {e}")

    # Cleanup
    live_client.stop_websocket()
    print("\nExamples beautifully completed.")

if __name__ == "__main__":
    main()
