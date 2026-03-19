import time
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import sys
from src.config.zerodha import load_zerodha_config
from src.data_ingestion.market_live import make_default_live_client, BarBuilder
from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map

def main():
    # Only internal logging processes the raw trace, we only print what we explicitly want
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    cfg = load_zerodha_config()
    
    try:
        df = load_instruments_csv()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Please download the complete instruments list from Zerodha Kite and save it to data/zerodha_instruments.csv")
        sys.exit(1)
        
    full_symbol_to_token = build_symbol_token_map(df)
    
    test_symbols = ["RELIANCE", "INFY", "TCS"]
    symbol_to_token = {}
    for s in test_symbols:
        if s in full_symbol_to_token:
            symbol_to_token[s] = full_symbol_to_token[s]
        else:
            print(f"Warning: Symbol {s} not found in instruments map. Skipping.")
            
    if not symbol_to_token:
        print("No valid test symbols found. Exiting.")
        sys.exit(1)
    
    print("--- Starting Kite Ingestion & Bar Building ---")
    
    # Passing symbol map during initialization as per requirements
    client = make_default_live_client(symbol_to_token=symbol_to_token)
    
    # We want 1-minute bars
    builder = BarBuilder(interval="1m")
    
    # Fire up the background WebSocket thread
    client.start_websocket()
    
    time.sleep(2)  # Give WS a moment to establish handshake
    
    symbols = list(symbol_to_token.keys())
    client.subscribe_symbols(symbols, mode="full")
    
    print(f"Subscribed to {symbols}. Listening for 10 minutes (Ctrl+C to abort)...")
    print()
    
    try:
        # Loop 600 seconds = 10 minutes
        for _ in range(600):
            # Check for the latest tick for each subscribed symbol
            for symbol in symbols:
                tick = client.get_last_tick(symbol)
                if tick:
                    # Pass the tick into the builder. 
                    # If this tick crosses a 1-min boundary, it completes the previous bar
                    completed_bars = builder.on_tick(tick)
                    
                    for bar in completed_bars:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] NEW BAR {bar.symbol}: "
                              f"O={bar.open:.2f} H={bar.high:.2f} L={bar.low:.2f} C={bar.close:.2f} "
                              f"Vol={bar.volume} (Window: {bar.start.strftime('%H:%M')} - {bar.end.strftime('%H:%M')})")
            
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\nInterrupted by user. Shutting down gracefully...")
    
    finally:
        # Guarantee WebSocket closure to avoid zombie threads
        client.stop_websocket()
        print("Live bar ingestion cleanly stopped.")

if __name__ == "__main__":
    main()
