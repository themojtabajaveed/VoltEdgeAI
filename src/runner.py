import os
from dotenv import load_dotenv
load_dotenv()

import time
import subprocess
import logging
from datetime import datetime, time as dt_time
import zoneinfo
import json

from sniper.antigravity_watcher import AntigravityWatcher
import daily_decision_engine
from config.risk import load_risk_config
from trading.daily_risk_state import DailyRiskState
from trading.executor import TradeExecutor
from trading.execution_logger import get_executions_logger, log_execution
from trading.positions import PositionBook
from trading.exit_engine import ExitEngine, ExitSignal
from trading.sizing import MarketRegime, SymbolStats, allow_new_long
from data_ingestion.market_live import make_default_live_client, BarBuilder
from data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
import sys

# Constants
try:
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except zoneinfo.ZoneInfoNotFoundError:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

MARKET_START = dt_time(9, 15)   # 09:15 IST
MARKET_END = dt_time(15, 30)    # 15:30 IST
INTRADAY_INTERVAL_MIN = 15      # configurable cadence
ACTIVE_UNIVERSE = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL"]

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "runner.log")

# Ensure logs directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# Configure logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def run_script(script_name: str):
    """Run a Python script via subprocess and log the outcome."""
    script_path = os.path.join("src", script_name)
    now_str = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
    
    print(f"[{now_str}] Starting job: {script_name}")
    logging.info(f"Starting job: {script_name}")
    
    try:
        # We invoke the script using python3 and pass the module path or direct script execution.
        # Running from project root -> python3 src/script_name.py
        result = subprocess.run(
            ["python3", script_path],
            check=True,
            capture_output=True,
            text=True
        )
        print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] Successfully finished job: {script_name}")
        logging.info(f"Successfully finished job: {script_name}")
        
    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] Job failed: {script_name} (Exit {e.returncode})")
        logging.error(f"Job failed: {script_name} with exit code {e.returncode}")
        logging.error(f"Error Output:\n{e.stderr}")
    except Exception as e:
        print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] Failed to execute: {script_name} ({e})")
        logging.error(f"Failed to execute {script_name}: {e}")

def main():
    risk_cfg = load_risk_config()
    
    print("--- VoltEdgeAI Automated Runner ---")
    print(f"Market Hours: {MARKET_START} to {MARKET_END} IST")
    print(f"Intraday interval: {INTRADAY_INTERVAL_MIN} minutes")
    print("\n--- Risk & Execution Framework ---")
    if risk_cfg.live_mode:
        print("VoltEdge LIVE_MODE = True (LIVE BROKER ORDERS ENABLED)")
    else:
        print("VoltEdge LIVE_MODE = False (DRY_RUN only)")
    print(f"Max Trades / Day: {risk_cfg.max_trades_per_day}")
    print(f"Max Daily Loss : ₹{risk_cfg.max_daily_loss_rupees:,.2f}")
    print(f"Per-Trade Risk : ₹{risk_cfg.per_trade_capital_rupees:,.2f}")
    print(f"Open Positions : {risk_cfg.max_open_positions}")
    print("\nPress Ctrl+C to stop.\n")
    
    exec_logger = get_executions_logger()
    exec_logger.info(f"VoltEdge LIVE_MODE = {risk_cfg.live_mode}")
    
    logging.info(f"Runner started. LIVE_MODE: {risk_cfg.live_mode}, MaxTrades: {risk_cfg.max_trades_per_day}, DailyLossCap: {risk_cfg.max_daily_loss_rupees}")
    
    watcher = AntigravityWatcher()
    
    # Initialize stateful Daily P&L Tracker
    today = datetime.now(IST).date()
    risk_state = DailyRiskState(trading_date=today)
    executor = TradeExecutor(risk=risk_cfg, daily_state=risk_state)
    
    try:
        df = load_instruments_csv()
        full_map = build_symbol_token_map(df)
        active_map = {s: full_map[s] for s in ACTIVE_UNIVERSE if s in full_map}
    except Exception as e:
        logging.error(f"Failed to load instruments CSV: {e}")
        print("Error: Could not load data/zerodha_instruments.csv")
        sys.exit(1)
        
    client = make_default_live_client(symbol_to_token=active_map)
    builder = BarBuilder(interval="1m")
    client.start_websocket()
    
    # Wait for connection to establish
    time.sleep(2)
    client.subscribe_symbols(list(active_map.keys()), mode="full")
    
    positions = PositionBook()
    exit_engine = ExitEngine(positions=positions, live_client=client, risk=risk_cfg)
    
    import threading
    def bar_builder_worker():
        while True:
            for symbol in active_map.keys():
                tick = client.get_last_tick(symbol)
                if tick:
                    builder.on_tick(tick)
            time.sleep(1)
            
    t = threading.Thread(target=bar_builder_worker, daemon=True)
    t.start()
    
    daily_traded_symbols = set()
    
    last_intraday_run = None
    last_eod_run_date = None
    
    while True:
        try:
            now = datetime.now(IST)
            current_time = now.time()
            current_date = now.date()
            weekday = now.weekday()  # Monday is 0, Sunday is 6
            
            is_weekday = weekday < 5  # Mon-Fri
            
            # Reset daily risk state globally if the date has changed over midnight
            if current_date != risk_state.trading_date:
                risk_state.reset_for_new_day(current_date)
                daily_traded_symbols.clear()
                logging.info(f"Resetting DailyRiskState for new session: {current_date}")
            
            if is_weekday:
                # 1. Intraday tasks between 9:15 and 15:30
                if MARKET_START <= current_time <= MARKET_END:
                    should_run_intraday = False
                    
                    if last_intraday_run is None:
                        should_run_intraday = True
                    else:
                        minutes_since_last_run = (now - last_intraday_run).total_seconds() / 60.0
                        if minutes_since_last_run >= INTRADAY_INTERVAL_MIN:
                            should_run_intraday = True
                            
                    if should_run_intraday:
                        run_script("run_juror_nse_live.py")
                        
                        try:
                            now_str = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
                            print(f"[{now_str}] Starting job: daily_decision_engine (native)")
                            logging.info("Starting job: daily_decision_engine (native with watcher)")
                            daily_decision_engine.main(watcher=watcher)
                            print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] Successfully finished job: daily_decision_engine")
                        except Exception as e:
                            print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] Failed to execute daily_decision_engine: {e}")
                            logging.error(f"Failed to execute daily_decision_engine natively: {e}")
                        
                        last_intraday_run = now

                        # Advance Antigravity state machine
                        try:
                            signals = watcher.tick(now=datetime.now(IST))
                            if signals:
                                # Example placeholder: treat everything as neutral for now
                                market_regime = MarketRegime(trend="sideways", strength=0.0)
                                signals_log_path = os.path.join(LOG_DIR, "antigravity_signals.csv")
                                file_exists = os.path.isfile(signals_log_path)
                                with open(signals_log_path, "a", encoding="utf-8") as f:
                                    if not file_exists:
                                        f.write("timestamp,symbol,vwap,ltp,z_score,volume,event,note\n")
                                    for sig in signals:
                                        fmt = f"{sig['timestamp'].isoformat()},{sig['symbol']},{sig['vwap']},{sig['ltp']},{sig['z_score']},{sig['volume']},{sig['event']},\"{sig['note']}\"\n"
                                        f.write(fmt)
                                        print(f"\n*** NEW ANTIGRAVITY BUY SIGNAL EMITTED: {sig['symbol']} ***\n")
                                        
                                        sym = sig.get('symbol')
                                        ltp = sig.get('ltp')
                                        if not sym or ltp is None: continue
                                        
                                        if sym in daily_traded_symbols:
                                            logging.info(f"Skipping executor for {sym}: already traded today.")
                                            continue
                                            
                                        # Risk guard: max trades per day
                                        if risk_state.trades_taken >= risk_cfg.max_trades_per_day:
                                            exec_logger.info("SKIP_EXECUTION: max_trades_per_day reached (%s), skipping %s", risk_state.trades_taken, sym)
                                            print(f"Risk constraint hit: trades_taken >= max_trades_per_day ({risk_cfg.max_trades_per_day}).")
                                            continue
                                            
                                        sym_stats = SymbolStats(
                                            symbol=sym,
                                            last_price=ltp,
                                            avg_daily_turnover_rupees=0.0,
                                        )
                                        
                                        if not allow_new_long(sym_stats, market_regime, risk_cfg):
                                            exec_logger.info(
                                                "SKIP_EXECUTION: allow_new_long rejected symbol %s in regime %s/%s",
                                                sym, market_regime.trend, market_regime.strength,
                                            )
                                            print(f"Risk constraint hit: allow_new_long sizing rejected {sym}.")
                                            continue
                                            
                                        # Execute (DRY_RUN or LIVE depending on risk_config.live_mode)
                                        result = executor.execute_buy(
                                            symbol=sym, 
                                            ltp=ltp,
                                            market_regime=market_regime,
                                            symbol_stats=sym_stats,
                                        )
                                        
                                        if not result.success:
                                            exec_logger.info("EXECUTION_FAILED: executor rejected symbol %s: %s", sym, result.message)
                                        
                                        # Update daily state
                                        if result.success:
                                            risk_state.trades_taken += 1
                                            daily_traded_symbols.add(sym)
                                            positions.on_buy_fill(
                                                symbol=sym,
                                                qty=result.filled_qty,
                                                price=result.avg_price,
                                                mode="INTRADAY",
                                                strategy=sig.get("strategy", "ANTIGRAVITY"),
                                                initial_stop_price=sig.get("initial_stop_price")
                                            )
                                            
                                        # Add some meta info (strategy, z_score, vwap)
                                        meta = {
                                            "strategy": sig.get("strategy", "ANTIGRAVITY"),
                                            "z_score": sig.get("z_score"),
                                            "vwap": sig.get("vwap"),
                                        }
                                        log_execution(exec_logger, symbol=sym, ltp=ltp, result=result, meta=meta)

                        except Exception as e:
                            logging.error(f"Failed to execute Antigravity Watcher tick: {e}")
                            print(f"\nWatcher tick error: {e}")
                            
                        # Process Exits
                        try:
                            exit_signals = exit_engine.tick(now=now)
                            for es in exit_signals:
                                pos = positions.get_position(es.symbol)
                                if not pos or pos.total_qty <= 0:
                                    continue
                                
                                qty = pos.total_qty
                                result = executor.execute_sell(symbol=es.symbol, qty=qty, ltp=es.ltp)
                                
                                if result.success:
                                    # Fallback to ltp if avg_price not populated in DRY_RUN mock
                                    price = result.avg_price if result.avg_price else es.ltp
                                    positions.on_sell_fill(symbol=es.symbol, qty=qty, price=price)
                                    
                                meta = {
                                    "reason": es.reason,
                                    "mode": es.mode,
                                    "strategy": es.strategy,
                                    "stop_price": es.stop_price,
                                }
                                log_execution(exec_logger, symbol=es.symbol, ltp=es.ltp, result=result, meta=meta)
                        except Exception as e:
                            logging.error(f"Failed to process ExitEngine tick: {e}")
                            print(f"\nExitEngine tick error: {e}")

                        last_intraday_run = now
                
                # 2. End of Day tasks after 15:30
                elif current_time > MARKET_END:
                    if last_eod_run_date != current_date:
                        run_script("log_daily_performance.py")
                        last_eod_run_date = current_date
            
            # Sleep for 60 seconds before checking time again
            time.sleep(60)
            
        except KeyboardInterrupt:
            print("\nShutting down runner...")
            logging.info("Runner stopped by user.")
            client.stop_websocket()
            break
        except Exception as e:
            print(f"Unexpected error in runner loop: {e}")
            logging.error(f"Unexpected error in runner loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
