import os
import time
import subprocess
import logging
from datetime import datetime, time as dt_time
import zoneinfo
import json

from src.sniper.antigravity_watcher import AntigravityWatcher
from src.sniper.momentum_scanner import fetch_top_movers
from src.sniper.stock_discovery import StockDiscovery
from src.sniper.technical_scorer import TechnicalScorer, meets_entry_threshold
from src.juror.catalyst_analyzer import CatalystAnalyzer
import src.daily_decision_engine as daily_decision_engine
from src.config.risk import load_risk_config
from src.trading.daily_risk_state import DailyRiskState
from src.trading.executor import TradeExecutor
from src.trading.execution_logger import get_executions_logger, log_execution
from src.trading.positions import PositionBook
from src.trading.exit_engine import ExitEngine, ExitSignal
from src.trading.position_monitor import PositionMonitor
from src.trading.sizing import MarketRegime, SymbolStats, allow_new_long
from src.trading.atr import compute_atr, compute_stop_distance, compute_atr_position_size
from src.trading.trading_costs import compute_breakeven_move_pct, is_trade_viable
from src.trading.circuit_limits import is_safe_to_enter_long, is_safe_to_enter_short
from src.trading.time_of_day import should_allow_new_entry, adjust_score_for_time, get_expiry_risk_factor
from src.trading.sector_guard import check_sector_concentration
from src.data_ingestion.intraday_context import get_intraday_bars_for_symbol
from src.data_ingestion.market_live import make_default_live_client, BarBuilder
from src.data_ingestion.instruments import load_instruments_csv, build_symbol_token_map
from src.data_ingestion.market_sentiment import compute_index_sentiment
from src.data_ingestion.macro_context import refresh_macro_context, get_cached_context
from src.data_ingestion.nse_scraper import get_deal_signal
from src.data_ingestion.pcr_tracker import compute_pcr, get_pcr_score_modifier
from src.trading.depth_analyzer import analyze_depth, get_depth_score_modifier, should_skip_illiquid
from src.tools.auto_login import auto_refresh_access_token
from src.data_ingestion.news_context import NewsClient
import sys

# Constants
try:
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except zoneinfo.ZoneInfoNotFoundError:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

def load_daily_regime() -> MarketRegime:
    regime_file = "data/daily_regime.json"
    if os.path.exists(regime_file):
        try:
            with open(regime_file, "r") as f:
                data = json.load(f)
                return MarketRegime(trend=data.get("trend", "sideways"), strength=float(data.get("strength", 0.0)))
        except:
            pass
    return MarketRegime(trend="sideways", strength=0.0)

MARKET_START  = dt_time(9, 15)   # 09:15 IST
MARKET_END    = dt_time(15, 30)  # 15:30 IST
SCANNER_TIME  = dt_time(9, 30)   # 09:30 — scanner runs once after open
PREMARKET_TIME= dt_time(8, 30)   # 08:30 — pre-market macro check
INTRADAY_INTERVAL_MIN = 15
# Active universe is now dynamic (loaded by momentum_scanner). Fallback if scanner fails:
FALLBACK_UNIVERSE = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL"]

LOG_DIR = "/tmp/voltedge_logs"
LOG_FILE = os.path.join(LOG_DIR, "runner.log")

def run_script(script_name: str):
    """Run a Python script via subprocess and log the outcome."""
    script_path = os.path.join("src", script_name)
    now_str = datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')
    
    print(f"[{now_str}] Starting job: {script_name}")
    logging.info(f"Starting job: {script_name}")
    
    try:
        env = os.environ.copy()
        # Ensure the repo root is explicitly set on PYTHONPATH so `import src.xxx` natively works
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # We invoke the script using python3 and pass the explicitly bounded environment.
        result = subprocess.run(
            ["python3", script_path],
            check=True,
            capture_output=True,
            text=True,
            env=env
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

def run_loop(live_mode: bool = False, per_trade_capital: int = 300, max_trades_per_day: int = 3):
    # Ensure logs directory exists
    os.makedirs(LOG_DIR, exist_ok=True)
    
    # Configure logging inline
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    risk_cfg = load_risk_config()
    risk_cfg.live_mode = live_mode
    risk_cfg.per_trade_capital_rupees = float(per_trade_capital)
    risk_cfg.max_trades_per_day = max_trades_per_day
    
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
    
    # V2 Core Engines
    discovery = StockDiscovery(top_n=10)
    scorer = TechnicalScorer()
    catalyst_analyzer = CatalystAnalyzer()
    
    # Initialize stateful Daily P&L Tracker
    today = datetime.now(IST).date()
    risk_state = DailyRiskState(trading_date=today)
    executor = TradeExecutor(risk=risk_cfg, daily_state=risk_state)
    
    try:
        df = load_instruments_csv()
        full_map = build_symbol_token_map(df)
        active_map = {s: full_map[s] for s in FALLBACK_UNIVERSE if s in full_map}
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
    pos_monitor = PositionMonitor(live_client=client)
    
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
    last_intraday_run   = None
    last_eod_run_date   = None
    last_report_date    = None
    last_premarket_date = None
    last_scanner_date   = None
    last_feedback_date  = None
    last_discovery_run  = None
    last_regime_update  = None    # V3: live regime updates
    last_autopsy_date   = None    # Phase G: EOD autopsy
    scanner_long_symbols:  list = []
    scanner_short_symbols: list = []
    
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
                discovery.reset()
                logging.info(f"Resetting DailyRiskState for new session: {current_date}")
            
            if is_weekday:
                # -1. Pre-Market Macro Sentiment Check (08:30)
                if current_time >= PREMARKET_TIME and last_premarket_date != current_date:
                    try:
                        logging.info("Running Pre-Market Macro News Check (NewsData.io)...")
                        news_client = NewsClient()
                        all_headlines = []
                        
                        # A) Indian Macro (1 credit)
                        macro_news = news_client.fetch_indian_macro_summary()
                        if macro_news:
                            all_headlines.extend([n.headline for n in macro_news])
                            print(f"\n[08:30 NEWS] Indian Macro: {len(macro_news)} headlines loaded")
                        
                        # B) Sector Rotation Scan (5 credits)
                        SECTOR_QUERIES = [
                            "IT services Infosys TCS Wipro",
                            "banking HDFC SBI ICICI",
                            "pharma Sun Pharma Cipla",
                            "auto Tata Motors Maruti",
                            "energy Reliance ONGC power",
                        ]
                        sector_summaries = {}
                        for sq in SECTOR_QUERIES:
                            sector_news = news_client.fetch_sector_news(sq)
                            sector_name = sq.split()[0]
                            sector_summaries[sector_name] = len(sector_news)
                            all_headlines.extend([n.headline for n in sector_news[:3]])
                        print(f"[08:30 NEWS] Sector Scan: {sector_summaries}")
                        
                        # C) Global Commodity Risk (2 credits)
                        commodity_news = news_client.fetch_global_commodity_news()
                        global_risk_news = news_client.fetch_global_macro_risk()
                        all_headlines.extend([n.headline for n in commodity_news[:3]])
                        all_headlines.extend([n.headline for n in global_risk_news[:3]])
                        print(f"[08:30 NEWS] Global Risk: {len(commodity_news)} commodity, {len(global_risk_news)} macro")
                        
                        # D) Gemini Sentiment Scoring
                        if all_headlines:
                            score = catalyst_analyzer.analyze_premarket_macro(all_headlines[:20])
                            trend = "bullish" if score > 0.2 else "bearish" if score < -0.2 else "sideways"
                            
                            regime_data = {"trend": trend, "strength": score, "date": str(current_date)}
                            os.makedirs("data", exist_ok=True)
                            with open("data/daily_regime.json", "w") as f:
                                json.dump(regime_data, f)
                            
                            print(f"[08:30 PRE-MARKET ORACLE] Overnight Sentiment: {trend.upper()} ({score:+.2f})")
                            logging.info(f"Pre-Market regime updated: {regime_data}")
                    except Exception as e:
                        logging.error(f"Pre-market oracle failed: {e}")
                    last_premarket_date = current_date

                # 0. Run momentum scanner + stock discovery at 09:30
                if current_time >= SCANNER_TIME and last_scanner_date != current_date:
                    try:
                        movers = fetch_top_movers()
                        scanner_long_symbols  = [c.symbol for c in movers["gainers"]]
                        scanner_short_symbols = [c.symbol for c in movers["losers"]]
                        all_scan_symbols = scanner_long_symbols + scanner_short_symbols
                        
                        # V2: Feed scanner results into StockDiscovery
                        discovery.ingest_scanner_results(movers["gainers"], movers["losers"])
                        
                        logging.info(f"Scanner: LONG={scanner_long_symbols}, SHORT={scanner_short_symbols}")
                        print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Scanner → LONG: {scanner_long_symbols}")
                        print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Scanner → SHORT: {scanner_short_symbols}")
                        # Subscribe websocket to scanned symbols
                        if all_scan_symbols:
                            client.subscribe_symbols(all_scan_symbols, mode="full")
                            
                        # V2.1: Fetch delayed NewsData overnight catalyst for the top 5 gainers and losers
                        print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Fetching overnight news context via NewsData.io...")
                        news_client = NewsClient()
                        top_targets = all_scan_symbols[:5] + all_scan_symbols[-5:]
                        for sym in set(top_targets):
                            try:
                                eod_news = news_client.fetch_stock_eod_news(sym)
                                if eod_news:
                                    headline = eod_news[0].headline
                                    direction = "LONG" if sym in scanner_long_symbols else "SHORT"
                                    discovery.ingest_manual(
                                        symbol=sym, direction=direction, source="newsdata_eod", 
                                        headline=headline
                                    )
                                    logging.info(f"NewsContext {sym}: {headline}")
                            except Exception as sub_e:
                                logging.warning(f"Failed EOD news fetch for {sym}: {sub_e}")

                    except Exception as e:
                        logging.error(f"Momentum scanner failed: {e}")
                    last_scanner_date = current_date

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

                        # ── V2: STOCK DISCOVERY → SCORE → ANALYZE → EXECUTE ──────────
                        try:
                            # V3: Update market regime live every 30 minutes
                            if last_regime_update is None or (now - last_regime_update).total_seconds() > 1800:
                                try:
                                    live_sentiment = compute_index_sentiment(client, "NIFTY 50", [])
                                    if live_sentiment.trend != "sideways" or live_sentiment.strength > 0.3:
                                        market_regime = MarketRegime(trend=live_sentiment.trend, strength=live_sentiment.strength)
                                        logging.info(f"Live regime update: {live_sentiment.trend} ({live_sentiment.strength:.2f})")
                                        print(f"  📊 Live regime: {live_sentiment.trend} ({live_sentiment.strength:.2f}) — {live_sentiment.comment}")
                                    else:
                                        market_regime = load_daily_regime()
                                except Exception:
                                    market_regime = load_daily_regime()
                                last_regime_update = now
                            else:
                                market_regime = load_daily_regime()

                            # V4: Macro context refresh (commodity/forex/FII/DII)
                            try:
                                macro_ctx = refresh_macro_context()
                                print(f"  🌍 {macro_ctx.summary}")
                                if macro_ctx.is_risk_off():
                                    print(f"  ⚠️ MACRO RISK-OFF — trade scores will be dampened")
                            except Exception as e:
                                macro_ctx = None
                                logging.warning(f"Macro context failed: {e}")

                            # V3: Time-of-day gate
                            time_ok, time_reason = should_allow_new_entry(datetime.now(IST))
                            if not time_ok:
                                logging.info(f"Time gate blocked: {time_reason}")
                            
                            # V3: F&O expiry risk factor
                            expiry_factor = get_expiry_risk_factor(current_date)
                            if expiry_factor < 1.0:
                                print(f"  ⚠️ F&O expiry day — position sizes reduced to {expiry_factor:.0%}")

                            # V4: PCR update (alongside regime, every 30 min)
                            pcr_data = None
                            try:
                                kite_raw = getattr(client, '_kite', None)
                                if kite_raw:
                                    nifty_tick = client.get_last_tick("NIFTY 50")
                                    spot = nifty_tick.ltp if nifty_tick else None
                                    pcr_data = compute_pcr(kite_raw, spot)
                                    if pcr_data:
                                        print(f"  📉 PCR: {pcr_data.summary}")
                            except Exception:
                                pcr_data = None

                            watchlist = discovery.get_ranked_watchlist()
                            
                            if watchlist:
                                print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] V2 Discovery Watchlist ({len(watchlist)} candidates):")
                                for ds in watchlist:
                                    print(f"  {ds.symbol} [{ds.direction}] Score={ds.total_score:.1f} Sources={ds.sources}")
                                
                                for ds in watchlist:
                                    sym = ds.symbol
                                    if sym in daily_traded_symbols:
                                        continue
                                    if risk_state.trades_taken >= risk_cfg.max_trades_per_day:
                                        print(f"Risk: max_trades_per_day reached. Stopping.")
                                        break
                                    
                                    # Get intraday bars for scoring
                                    bars = get_intraday_bars_for_symbol(sym, lookback_minutes=70)
                                    if not bars or len(bars) < 5:
                                        continue
                                    
                                    # Fetch daily data for scoring
                                    try:
                                        from src.sources.nse_prices import fetch_daily_ohlcv
                                        daily_df = fetch_daily_ohlcv(sym, days=252)
                                    except Exception:
                                        daily_df = None
                                    
                                    # Get Nifty change for relative strength
                                    nifty_pct = 0.0
                                    nifty_tick = client.get_last_tick("NIFTY 50")
                                    if nifty_tick:
                                        nifty_snap = client.get_snapshot(["NIFTY 50"])
                                        if "NIFTY 50" in nifty_snap:
                                            ns = nifty_snap["NIFTY 50"]
                                            if ns.ohlc.get("open", 0) > 0:
                                                nifty_pct = (ns.ltp - ns.ohlc["open"]) / ns.ohlc["open"] * 100
                                    
                                    # Score the candidate
                                    if ds.direction == "LONG":
                                        tech_score = scorer.score_long(sym, daily_df, bars, nifty_pct)
                                    else:
                                        tech_score = scorer.score_short(sym, daily_df, bars, nifty_pct)
                                    
                                    print(f"  → {tech_score.summary}")
                                    logging.info(f"TechScore: {tech_score.summary}")
                                    
                                    # V3: Apply time-of-day adjustment
                                    adjusted_score = adjust_score_for_time(tech_score.total, datetime.now(IST))
                                    tech_score.total = adjusted_score
                                    
                                    # V4: Apply macro score modifier
                                    if macro_ctx:
                                        macro_mod = macro_ctx.get_score_modifier()
                                        tech_score.total = tech_score.total * macro_mod
                                        if macro_mod != 1.0:
                                            print(f"    Macro modifier: {macro_mod:.2f}x → adjusted score={tech_score.total:.0f}")
                                    
                                    # Check threshold
                                    if not meets_entry_threshold(tech_score, market_regime.trend):
                                        logging.info(f"{sym} score {tech_score.total:.0f} below threshold for {market_regime.trend} regime")
                                        continue
                                    
                                    # V3: Time-of-day hard gate
                                    if not time_ok:
                                        print(f"  ⏰ {sym} passed score but blocked by time gate: {time_reason}")
                                        continue
                                    
                                    # Catalyst analysis (LLM)
                                    try:
                                        catalyst = catalyst_analyzer.analyze(
                                            symbol=sym, ltp=ds.ltp, pct_change=ds.pct_change,
                                            volume=ds.volume, direction=ds.direction,
                                            headline=ds.catalyst_headline,
                                        )
                                        print(f"  → Catalyst: {catalyst.catalyst_summary} (Remaining: {catalyst.estimated_remaining_move}%)")
                                        logging.info(f"Catalyst {sym}: {catalyst.catalyst_type} conf={catalyst.confidence}")
                                        
                                        # Skip if remaining move is too small
                                        if catalyst.estimated_remaining_move < 0.5:
                                            print(f"  → Skipping {sym}: estimated remaining move only {catalyst.estimated_remaining_move}%")
                                            continue
                                    except Exception as ce:
                                        logging.warning(f"Catalyst analysis failed for {sym}: {ce}")
                                        catalyst = None
                                    
                                    # Compute ATR-based stop and position size
                                    ltp = bars[-1].close
                                    atr_val = compute_atr(bars, period=14)
                                    stop_dist = compute_stop_distance(atr_val) if atr_val > 0 else ltp * 0.015
                                    # Cap stop at 2.5% max loss
                                    max_stop = ltp * 0.025
                                    stop_dist = min(stop_dist, max_stop)
                                    
                                    if ds.direction == "LONG":
                                        stop_price = round(ltp - stop_dist, 2)
                                    else:
                                        stop_price = round(ltp + stop_dist, 2)
                                    
                                    qty = compute_atr_position_size(risk_cfg.per_trade_capital_rupees, stop_dist) if stop_dist > 0 else 1
                                    
                                    # V3: Apply F&O expiry factor
                                    qty = max(1, int(qty * expiry_factor))
                                    
                                    # V3: Trading costs viability check
                                    breakeven_pct = compute_breakeven_move_pct(qty, ltp)
                                    expected_move = catalyst.estimated_remaining_move if catalyst else 1.0
                                    if not is_trade_viable(qty, ltp, expected_move):
                                        print(f"  💰 {sym}: breakeven={breakeven_pct:.3f}%, expected={expected_move:.1f}% — cost ratio too high, skipping")
                                        continue
                                    
                                    # V4: Order book depth check
                                    tick_raw = client.get_last_tick(sym)
                                    if tick_raw and hasattr(tick_raw, 'bid') and hasattr(tick_raw, 'ask'):
                                        depth_data = {"buy": [{"price": tick_raw.bid or 0, "quantity": 0, "orders": 0}],
                                                      "sell": [{"price": tick_raw.ask or 0, "quantity": 0, "orders": 0}]}
                                        depth = analyze_depth(sym, depth_data, ltp)
                                        if should_skip_illiquid(depth):
                                            print(f"  🚨 {sym}: ILLIQUID (spread {depth.bid_ask_spread_pct:.3f}%) — skipping")
                                            continue
                                        depth_mod = get_depth_score_modifier(depth, ds.direction)
                                        if depth_mod != 1.0:
                                            tech_score.total *= depth_mod
                                            print(f"    Depth modifier: {depth_mod:.2f}x ({depth.signal})")
                                    
                                    # V4: PCR modifier for LONG/SHORT
                                    pcr_mod = get_pcr_score_modifier(pcr_data)
                                    if pcr_mod != 1.0:
                                        tech_score.total *= pcr_mod
                                        pcr_label = pcr_data.signal if pcr_data else "n/a"
                                        print(f"    PCR modifier: {pcr_mod:.2f}x (PCR={pcr_label})")
                                    
                                    # V3: Circuit limit check
                                    prev_close = ds.prev_close if ds.prev_close > 0 else ltp
                                    if ds.direction == "LONG":
                                        circuit_ok, circuit_reason = is_safe_to_enter_long(sym, prev_close, ltp)
                                    else:
                                        circuit_ok, circuit_reason = is_safe_to_enter_short(sym, prev_close, ltp)
                                    if not circuit_ok:
                                        print(f"  🚫 {sym}: {circuit_reason}")
                                        continue
                                    
                                    # V3: Sector concentration check
                                    open_symbols = [p.symbol for p in positions.get_open_positions()]
                                    sector_ok, sector_reason = check_sector_concentration(sym, open_symbols)
                                    if not sector_ok:
                                        print(f"  🏭 {sym}: {sector_reason}")
                                        continue
                                    
                                    # V4: Bulk/block deal check (institutional interest)
                                    deal_sig = get_deal_signal(sym)
                                    if deal_sig == "INSTITUTIONAL_SELL" and ds.direction == "LONG":
                                        print(f"  🏦 {sym}: Institutional SELL deal detected — skipping LONG")
                                        continue
                                    elif deal_sig == "INSTITUTIONAL_BUY" and ds.direction == "SHORT":
                                        print(f"  🏦 {sym}: Institutional BUY deal detected — skipping SHORT")
                                        continue
                                    elif deal_sig == "INSTITUTIONAL_BUY" and ds.direction == "LONG":
                                        print(f"  🏦 {sym}: Institutional BUY confirmed — extra conviction")
                                    
                                    # Execute trade
                                    if ds.direction == "LONG":
                                        sym_stats = SymbolStats(symbol=sym, last_price=ltp, avg_daily_turnover_rupees=ds.volume * ltp if ds.volume > 0 else 5_000_000.0)
                                        if not allow_new_long(sym_stats, market_regime, risk_cfg):
                                            continue
                                        result = executor.execute_buy(symbol=sym, ltp=ltp, qty=qty, market_regime=market_regime)
                                        if result.success:
                                            risk_state.trades_taken += 1
                                            daily_traded_symbols.add(sym)
                                            positions.on_buy_fill(
                                                symbol=sym, qty=result.filled_qty, price=result.avg_price or ltp,
                                                mode="INTRADAY", strategy="V2_DISCOVERY_LONG",
                                                initial_stop_price=stop_price, atr=atr_val,
                                            )
                                            print(f"  ✅ EXECUTED LONG: {qty}x {sym} @ {ltp:.2f} SL={stop_price:.2f}")
                                    else:  # SHORT
                                        result = executor.execute_short_sell(symbol=sym, ltp=ltp, qty=qty)
                                        if result.success:
                                            risk_state.trades_taken += 1
                                            daily_traded_symbols.add(sym)
                                            positions.on_short_fill(
                                                symbol=sym, qty=result.filled_qty, price=result.avg_price or ltp,
                                                mode="INTRADAY", strategy="V2_DISCOVERY_SHORT",
                                                initial_stop_price=stop_price, atr=atr_val,
                                            )
                                            print(f"  ✅ EXECUTED SHORT: {qty}x {sym} @ {ltp:.2f} SL={stop_price:.2f}")
                                    
                                    cat_meta = {"catalyst": catalyst.catalyst_type, "remaining": catalyst.estimated_remaining_move} if catalyst else {}
                                    log_execution(exec_logger, symbol=sym, ltp=ltp, result=result,
                                                  meta={"strategy": f"V2_{ds.direction}", "tech_score": tech_score.total, "stop": stop_price, "atr": atr_val, **cat_meta})
                        except Exception as e:
                            logging.error(f"V2 Discovery pipeline error: {e}")
                            import traceback; traceback.print_exc()

                        # ── Antigravity state machine (legacy, still runs in parallel) ──
                        try:
                            signals = watcher.tick(
                                bars_provider=get_intraday_bars_for_symbol,
                                now=datetime.now(IST),
                            )
                            if signals:
                                signals_log_path = os.path.join(LOG_DIR, "antigravity_signals.csv")
                                file_exists = os.path.isfile(signals_log_path)
                                with open(signals_log_path, "a", encoding="utf-8") as f:
                                    if not file_exists:
                                        f.write("timestamp,symbol,vwap,ltp,z_score,volume,event,note\n")
                                    for sig in signals:
                                        fmt = f"{sig['timestamp'].isoformat()},{sig['symbol']},{sig['vwap']},{sig['ltp']},{sig['z_score']},{sig['volume']},{sig['event']},\"{sig['note']}\"\n"
                                        f.write(fmt)
                                        print(f"\n*** ANTIGRAVITY BUY SIGNAL: {sig['symbol']} ***")
                                        
                                        sym = sig.get('symbol')
                                        ltp = sig.get('ltp')
                                        if not sym or ltp is None or sym in daily_traded_symbols: continue
                                        if risk_state.trades_taken >= risk_cfg.max_trades_per_day: continue

                                        bars = get_intraday_bars_for_symbol(sym, lookback_minutes=70)
                                        atr_val = compute_atr(bars, period=14)
                                        stop_dist = compute_stop_distance(atr_val) if atr_val > 0 else ltp * 0.015
                                        stop_dist = min(stop_dist, ltp * 0.025)
                                        stop_price = round(ltp - stop_dist, 2)
                                        qty = compute_atr_position_size(risk_cfg.per_trade_capital_rupees, stop_dist) if atr_val > 0 else 1

                                        market_regime = load_daily_regime()
                                        result = executor.execute_buy(symbol=sym, ltp=ltp, qty=qty, market_regime=market_regime)
                                        if result.success:
                                            risk_state.trades_taken += 1
                                            daily_traded_symbols.add(sym)
                                            positions.on_buy_fill(
                                                symbol=sym, qty=result.filled_qty, price=result.avg_price or ltp,
                                                mode="INTRADAY", strategy="ANTIGRAVITY",
                                                initial_stop_price=stop_price, atr=atr_val,
                                            )
                                        log_execution(exec_logger, symbol=sym, ltp=ltp, result=result,
                                                      meta={"strategy": "ANTIGRAVITY", "z_score": sig.get("z_score"), "vwap": sig.get("vwap"), "stop": stop_price, "atr": atr_val})

                        except Exception as e:
                            logging.error(f"Antigravity tick error: {e}")

                        # ── Position Monitoring (V2) ──
                        try:
                            for pos in positions.get_open_positions():
                                tick = client.get_last_tick(pos.symbol)
                                if not tick: continue
                                bars = get_intraday_bars_for_symbol(pos.symbol, lookback_minutes=30)
                                alerts = pos_monitor.check_position(
                                    symbol=pos.symbol, entry_price=pos.avg_price,
                                    side=pos.side, ltp=tick.ltp, intraday_bars=bars,
                                )
                                for alert in alerts:
                                    print(f"  ⚠️ [{alert.severity}] {alert.symbol}: {alert.message}")
                                    logging.info(f"PosMonitor [{alert.severity}] {alert.symbol}: {alert.message}")
                        except Exception as e:
                            logging.error(f"Position monitor error: {e}")
                            
                        # Process Exits — branches correctly on LONG vs SHORT
                        try:
                            exit_signals = exit_engine.tick(now=now)
                            for es in exit_signals:
                                pos = positions.get_position(es.symbol)
                                if not pos or pos.total_qty <= 0:
                                    continue

                                exit_qty   = pos.total_qty
                                entry_price = pos.avg_price
                                entry_time  = pos.entry_time
                                direction   = pos.side  # "LONG" or "SHORT"

                                # ── Route to correct executor method ──────────────────
                                if es.side == "COVER":  # Closing a SHORT position
                                    result = executor.execute_short_cover(symbol=es.symbol, qty=exit_qty, ltp=es.ltp)
                                else:                   # Closing a LONG position
                                    result = executor.execute_sell(symbol=es.symbol, qty=exit_qty, ltp=es.ltp)

                                if result.success:
                                    price = result.avg_price if result.avg_price else es.ltp

                                    if es.side == "COVER":
                                        closed_pos = positions.on_cover_fill(symbol=es.symbol, qty=exit_qty, price=price)
                                    else:
                                        closed_pos = positions.on_sell_fill(symbol=es.symbol, qty=exit_qty, price=price)

                                    if closed_pos is not None:
                                        # ── Update daily P&L so loss-cap fires correctly ──
                                        risk_state.daily_pnl = getattr(risk_state, 'daily_pnl', 0.0) + closed_pos.realized_pnl

                                        # ── Persist TradeRecord to SQLite ─────────────────
                                        try:
                                            from src.db import SessionLocal, TradeRecord
                                            with SessionLocal() as session:
                                                record = TradeRecord(
                                                    symbol=es.symbol,
                                                    direction=direction,
                                                    qty=exit_qty,
                                                    entry_price=entry_price,
                                                    exit_price=price,
                                                    pnl=closed_pos.realized_pnl,
                                                    entry_time=entry_time,
                                                    exit_time=datetime.now(IST),
                                                    mode=es.mode,
                                                    strategy=es.strategy,
                                                    exit_reason=es.reason
                                                )
                                                session.add(record)
                                                session.commit()
                                        except Exception as db_e:
                                            logging.error(f"Failed to log TradeRecord to SQLite: {db_e}")

                                meta = {"reason": es.reason, "mode": es.mode,
                                        "strategy": es.strategy, "stop_price": es.stop_price,
                                        "direction": direction}
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
                        
                # 3. 18:00 — Market Chronicle (replaces old daily_summary.py)
                if current_time >= dt_time(18, 0):
                    if last_report_date != current_date:
                        run_script("reports/market_chronicle.py")
                        last_report_date = current_date

                # 2b. 16:00 — EOD Market Autopsy (pattern learning)
                if current_time >= dt_time(16, 0) and current_time < dt_time(18, 0):
                    if last_autopsy_date != current_date:
                        try:
                            from src.reports.eod_autopsy import run_eod_autopsy
                            from kiteconnect import KiteConnect
                            kite = KiteConnect(api_key=os.getenv("ZERODHA_API_KEY"))
                            kite.set_access_token(os.getenv("ZERODHA_ACCESS_TOKEN"))
                            run_eod_autopsy(kite=kite)
                        except Exception as e:
                            logging.error(f"EOD Autopsy failed: {e}")
                        last_autopsy_date = current_date

                # 4. 18:01 — Prediction Feedback Loop (score morning's calls)
                if current_time >= dt_time(18, 1):
                    if last_feedback_date != current_date:
                        run_script("reports/feedback_loop.py")
                        last_feedback_date = current_date

                # 5. 06:00 — Morning Global Intelligence Brief
                if current_time >= dt_time(6, 0):
                    if last_premarket_date != current_date:
                        run_script("reports/pre_market_brief.py")
                        last_premarket_date = current_date
            
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
    import os
    from dotenv import load_dotenv

    # Load .env — try project root first, then /tmp shadow
    for _env_path in [".env", "/tmp/voltedge.env"]:
        if os.path.exists(_env_path):
            load_dotenv(_env_path)
            break
    else:
        _env_path = ".env"

    # Auto-login: refresh access token headlessly (requires TOTP secret)
    print("\n🔑 Attempting auto-login...")
    _token = auto_refresh_access_token(env_file=_env_path)
    if _token:
        print(f"🔑 Token refreshed: {_token[:8]}...")
    else:
        existing = os.getenv("ZERODHA_ACCESS_TOKEN", "")
        if existing:
            print(f"🔑 Using existing token: {existing[:8]}...")
        else:
            print("⚠️ No access token available. Set ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET for auto-login.")

    run_loop(
        live_mode=os.getenv("VOLTEDGE_LIVE_MODE", "0") == "1",
        per_trade_capital=int(float(os.getenv("VOLTEDGE_PER_TRADE_CAPITAL", "5000"))),
        max_trades_per_day=int(os.getenv("VOLTEDGE_MAX_TRADES_PER_DAY", "5"))
    )
