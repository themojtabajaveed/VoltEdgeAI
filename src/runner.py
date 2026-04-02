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
from src.trading.sizing import MarketRegime, SymbolStats, allow_new_long, allow_new_short
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
from src.strategies.hydra import HydraStrategy
from src.strategies.viper import ViperStrategy
from src.strategies.slot_manager import SlotManager, CONFLUENCE_BONUS
from src.strategies.technical_body import TechnicalBody, reset_streaming_state, get_or_create_streaming_state, _streaming_states
from src.trading.exit_monitor import ExitMonitorThread
from src.db.db_writer import get_db_writer
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
HYDRA_SCAN_TIME = dt_time(9, 0)  # 09:00 — HYDRA pre-market event scan
INTRADAY_INTERVAL_MIN = 15
# VIPER re-scan times (after initial 09:30 scan)
VIPER_RESCAN_TIMES = [dt_time(10, 0), dt_time(10, 30), dt_time(11, 0), dt_time(12, 0)]

# ── Grok 4.20 Portfolio Orchestrator schedule (v2) ──
# Aligned with NSE high-volatility windows. No calls after 11:45 for new entries.
GROK_OPTIMIZER_TIMES = [
    dt_time(9, 17),   # Post-open gap assessment (spreads settled)
    dt_time(9, 30),   # ORB complete — highest-probability entry window
    dt_time(10, 0),   # First hour complete — fades/reversals set up here
    dt_time(10, 45),  # Pre-lunch — last clean moves before midday lull
    dt_time(11, 45),  # Final assessment — manage positions before afternoon
]
GROK_EOD_TIME = dt_time(15, 40)  # Post-market review
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
        if result.stdout.strip():
            logging.info(f"[{script_name}] stdout:\n{result.stdout.strip()}")
        if result.stderr.strip():
            logging.warning(f"[{script_name}] stderr:\n{result.stderr.strip()}")

    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] Job failed: {script_name} (Exit {e.returncode})")
        if e.stderr:
            print(f"--- STDERR for {script_name} ---")
            print(e.stderr[-2000:])  # Last 2000 chars to avoid flooding
            print(f"--- END STDERR ---")
        logging.error(f"Job failed: {script_name} with exit code {e.returncode}")
        logging.error(f"Error Output:\n{e.stderr}")
    except Exception as e:
        print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] Failed to execute: {script_name} ({e})")
        logging.error(f"Failed to execute {script_name}: {e}")


def _should_fire_scheduled_job(scheduled_time: dt_time, runner_start_time: dt_time, current_time: dt_time) -> bool:
    """
    Prevent cascade-firing of past-due jobs on runner restart.
    A job should fire only if:
      1. current_time >= scheduled_time (it's past the scheduled time)
      2. runner started BEFORE the scheduled time (so it was running when the job was due)
         OR current_time is within 30 minutes of scheduled_time (grace window for restarts)
    """
    if current_time < scheduled_time:
        return False
    # If runner started before the scheduled time, fire normally
    if runner_start_time <= scheduled_time:
        return True
    # Grace window: if we restarted within 30 min of the scheduled time, still fire
    sched_minutes = scheduled_time.hour * 60 + scheduled_time.minute
    current_minutes = current_time.hour * 60 + current_time.minute
    return (current_minutes - sched_minutes) <= 30


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
    # v4: Give ExitEngine read access to streaming TA states for RSI divergence detection
    exit_engine.set_streaming_states(_streaming_states)
    pos_monitor = PositionMonitor(live_client=client)

    # ── P1: Push tick pipeline — replaces bar_builder_worker polling loop ──
    # Ticks flow: Kite WebSocket → _on_ticks (O(1)) → SimpleQueue → BarBuilderThread
    # Data-to-bar latency: was ~1000ms (poll), now sub-millisecond (push).
    #
    # Optimisation 1 — event-driven stop-loss detection:
    # The BarBuilderThread now also calls exit_engine.check_tick() on every tick.
    # Detected signals are queued in tick_exit_queue for the main thread to drain.
    # This reduces stop-loss exit latency from ~1000ms → <1ms.
    import queue as _queue
    tick_exit_queue: _queue.SimpleQueue = _queue.SimpleQueue()
    client.start_bar_builder_thread(builder, exit_engine=exit_engine, exit_signal_queue=tick_exit_queue)

    # ── P2: Dedicated exit monitor — decoupled from 60s main loop ──────────
    # Evaluates stop-loss / take-profit every 1 second regardless of how long
    # the strategy scanning loop takes. Signals are queued and drained below.
    db_writer = get_db_writer()
    exit_monitor = ExitMonitorThread(exit_engine, interval_seconds=1.0)
    exit_monitor.start()
    
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
    runner_start_time   = datetime.now(IST).time()  # Phase I: cascade prevention
    scanner_long_symbols:  list = []

    # ── Phase K: Dragon Architecture — HYDRA + VIPER Strategies ──
    hydra = HydraStrategy()
    viper = ViperStrategy()
    slot_manager = SlotManager(max_trades=max_trades_per_day)
    last_hydra_scan_date = None
    last_viper_scan_time = None  # Track VIPER re-scans
    viper_rescan_index = 0       # Which re-scan slot are we at
    scanner_short_symbols: list = []

    # ── Mid-session bearish discovery (SHORT-4) ──
    last_neg_pulse_date = None

    # ── Pre-market intelligence (v3) ──
    pre_market_intel = None  # PreMarketIntelligence result, cached daily

    # ── Grok 4.20 Portfolio Orchestrator state ──
    grok_call_count = 0          # Global daily Grok call counter
    grok_morning_plan = None     # Output of grok_morning_strategist()
    grok_optimizer_index = 0     # Which GROK_OPTIMIZER_TIMES slot we're at
    grok_last_actions = []       # Last orchestrator actions for logging
    
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
                hydra.reset_daily()
                slot_manager.reset_daily()
                last_hydra_scan_date = None
                grok_call_count = 0
                grok_morning_plan = None
                grok_optimizer_index = 0
                grok_last_actions = []
                pre_market_intel = None
                try:
                    from src.llm.grok_client import reset_conviction_history
                    reset_conviction_history()
                except Exception:
                    pass
                reset_streaming_state()
                exit_engine._divergence_warned.clear()
                
                # ── Fix 24/7 Autonomous Token Rollover ──
                logging.info(f"Generating fresh access token for {current_date}")
                print(f"🔑 Attempting daily auto-login for new session: {current_date}...")
                new_token = auto_refresh_access_token(env_file=".env")
                if new_token:
                    os.environ["ZERODHA_ACCESS_TOKEN"] = new_token
                    print(f"✅ Token refreshed successfully for new day: {new_token[:8]}...")
                    # Rebuild Live Client to swap in the new WebSocket ticket
                    try:
                        client.stop()
                    except Exception:
                        pass
                    client = make_default_live_client(symbol_to_token=active_map)
                else:
                    logging.critical("DAILY AUTO-LOGIN FAILED! Engine will not be able to trade today.")
                    print("❌ DAILY AUTO-LOGIN FAILED.")

                logging.info(f"Resetting DailyRiskState + HYDRA + Grok orchestrator for new session: {current_date}")
            
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

                        # ── Pre-Market Intelligence (v3) ──
                        # Composite forward-looking score: US markets, crude, DXY, FII, VIX, PCR
                        try:
                            from src.data_ingestion.pre_market_intelligence import fetch_and_compute
                            # Get Kite client for India VIX fetch
                            kite_raw = getattr(client, '_kite', None)
                            # Get existing macro context for FII/DII data
                            existing_macro = get_cached_context() if get_cached_context().fii_net_cr != 0 else None
                            pre_market_intel = fetch_and_compute(
                                kite_client=kite_raw,
                                macro_context=existing_macro,
                            )
                            if pre_market_intel:
                                print(f"  📊 {pre_market_intel.format_log_line()}")
                                logging.info(pre_market_intel.format_log_line())
                            else:
                                print(f"  ⚠️ Pre-market intelligence: no signals available")
                        except Exception as pmi_e:
                            logging.warning(f"Pre-market intelligence failed: {pmi_e}")
                            print(f"  ⚠️ Pre-market intelligence failed: {pmi_e}")

                        # ── Grok Morning Strategist (v2) ──
                        # Full portfolio-level pre-market call: macro + events + movers → daily plan
                        try:
                            from src.llm.grok_client import grok_morning_strategist, GROK_DAILY_BUDGET
                            if grok_call_count < GROK_DAILY_BUDGET:
                                hydra_events = hydra.get_top_candidates(max_n=8) if hydra.watchlist else []
                                viper_movers_pre = []  # VIPER hasn't scanned yet at 08:30
                                macro_ctx = {
                                    "trend": trend if 'trend' in dir() else "unknown",
                                    "strength": score if 'score' in dir() else 0.0,
                                    "date": str(current_date),
                                }
                                risk_budget_info = {
                                    "daily_loss_cap": risk_cfg.max_daily_loss_rupees,
                                    "per_trade_capital": risk_cfg.per_trade_capital_rupees,
                                    "max_trades": max_trades_per_day,
                                    "slots_available": slot_manager.remaining,
                                }
                                grok_morning_plan = grok_morning_strategist(
                                    macro_context=macro_ctx,
                                    hydra_events=hydra_events,
                                    viper_movers=viper_movers_pre,
                                    previous_day_pnl=0.0,
                                    risk_budget=risk_budget_info,
                                )
                                grok_call_count += 1
                                if grok_morning_plan:
                                    regime_from_grok = grok_morning_plan.get('regime', '?')
                                    risk_stance = grok_morning_plan.get('risk_stance', '')
                                    print(f"  🧠 Grok Morning Strategist: regime={regime_from_grok}")
                                    print(f"     Risk stance: {risk_stance}")
                                    wl = grok_morning_plan.get('watchlist', [])
                                    for w in wl:
                                        print(f"     #{w.get('priority',0)}: {w.get('symbol','?')} {w.get('bias','?')} — {w.get('catalyst','')[:60]}")
                                    avoids = grok_morning_plan.get('avoid', [])
                                    if avoids:
                                        print(f"     ⚠️ Avoid: {avoids}")
                                else:
                                    print(f"  🧠 Grok Morning Strategist: returned empty (will use mechanical rules)")
                        except Exception as grok_e:
                            logging.error(f"Grok morning strategist failed: {grok_e}")
                            print(f"  ⚠️ Grok morning call failed: {grok_e} (continuing with mechanical rules)")

                    except Exception as e:
                        logging.error(f"Pre-market oracle failed: {e}")
                    last_premarket_date = current_date

                # 0a. HYDRA pre-market event scan at 09:00
                if current_time >= HYDRA_SCAN_TIME and last_hydra_scan_date != current_date:
                    try:
                        print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] 🔥 HYDRA: Pre-market event scan...")
                        hydra_entries = hydra.scan()
                        if hydra_entries:
                            print(f"  HYDRA watchlist ({len(hydra_entries)} events):")
                            for entry in hydra_entries:
                                print(f"    {entry.symbol} [{entry.direction}] urgency={entry.urgency:.0f}/10 — {entry.event_summary}")
                            # IMP-1: Subscribe watchlist symbols for live ticks
                            hydra_syms = [e.symbol for e in hydra_entries]
                            try:
                                client.subscribe_symbols(hydra_syms, mode="full")
                                print(f"  📡 Subscribed {len(hydra_syms)} HYDRA symbols to websocket")
                            except Exception as ws_e:
                                logging.warning(f"HYDRA websocket subscribe failed: {ws_e}")
                            # NOTE: Grok ranking removed (v2) — orchestrator handles this centrally
                        else:
                            print(f"  HYDRA: No hot events found since last close")
                    except Exception as e:
                        logging.error(f"HYDRA pre-market scan failed: {e}")
                        print(f"  ❌ HYDRA scan error: {e}")
                    last_hydra_scan_date = current_date

                # 0b. Run momentum scanner + stock discovery at 09:30
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

                        # ── VIPER: Initial scan using momentum scanner results ──
                        try:
                            print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] 🐍 VIPER: Initial top mover scan...")
                            access_token = os.getenv("ZERODHA_ACCESS_TOKEN", "")
                            viper_entries = viper.scan(access_token=access_token)
                            if viper_entries:
                                print(f"  VIPER watchlist ({len(viper_entries)} movers):")
                                for ve in viper_entries:
                                    meta = getattr(ve, 'metadata', {}) or {}
                                    print(f"    {ve.symbol} [{ve.direction}] {meta.get('move_type','?')} → {meta.get('trade_mode','?')}")
                                # Subscribe VIPER symbols to websocket
                                viper_syms = [e.symbol for e in viper_entries]
                                try:
                                    client.subscribe_symbols(viper_syms, mode="full")
                                    print(f"  📡 Subscribed {len(viper_syms)} VIPER symbols to websocket")
                                except Exception as ws_e:
                                    logging.warning(f"VIPER websocket subscribe failed: {ws_e}")

                                # ── CONFLUENCE DETECTION ──
                                # Check if HYDRA and VIPER watchlists overlap
                                hydra_syms = [e.symbol for e in hydra.watchlist] if hydra.watchlist else []
                                confluence_symbols = viper.check_confluence(hydra_syms)
                                if confluence_symbols:
                                    slot_manager.register_confluence(confluence_symbols)
                                    print(f"  🐉 DRAGON CONFLUENCE: {confluence_symbols} found in BOTH HYDRA + VIPER!")

                                # NOTE: Grok ranking removed (v2) — orchestrator handles this centrally
                            else:
                                print(f"  VIPER: No tradeable movers found")
                        except Exception as viper_e:
                            logging.error(f"VIPER initial scan failed: {viper_e}")
                            print(f"  ❌ VIPER scan error: {viper_e}")
                        last_viper_scan_time = now
                        viper_rescan_index = 0

                    except Exception as e:
                        logging.error(f"Momentum scanner failed: {e}")
                    last_scanner_date = current_date

                # 0c. VIPER re-scans at 10:00, 10:30, 11:00, 12:00
                if (viper_rescan_index < len(VIPER_RESCAN_TIMES)
                        and current_time >= VIPER_RESCAN_TIMES[viper_rescan_index]
                        and MARKET_START <= current_time <= MARKET_END):
                    try:
                        print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] 🐍 VIPER: Re-scan #{viper_rescan_index + 1}...")
                        access_token = os.getenv("ZERODHA_ACCESS_TOKEN", "")
                        viper_entries = viper.scan(access_token=access_token)
                        if viper_entries:
                            # Re-check confluence with current HYDRA watchlist
                            hydra_syms = [e.symbol for e in hydra.watchlist] if hydra.watchlist else []
                            confluence_symbols = viper.check_confluence(hydra_syms)
                            if confluence_symbols:
                                slot_manager.register_confluence(confluence_symbols)
                                print(f"  🐉 New CONFLUENCE: {confluence_symbols}")
                            print(f"  VIPER re-scan: {len(viper_entries)} movers on watchlist")
                    except Exception as viper_e:
                        logging.error(f"VIPER re-scan #{viper_rescan_index + 1} failed: {viper_e}")
                    last_viper_scan_time = now
                    viper_rescan_index += 1

                # 0d. 12:00 IST — Mid-session negative news pulse (SHORT-4)
                if (current_time >= dt_time(12, 0) and current_time <= dt_time(12, 5)
                        and last_neg_pulse_date != current_date):
                    try:
                        news_client = NewsClient()
                        neg_news = news_client.fetch_negative_market_pulse()
                        if neg_news:
                            print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] 📉 Negative market pulse: {len(neg_news)} bearish headlines")
                            for item in neg_news[:5]:
                                headline = getattr(item, 'title', '') or getattr(item, 'headline', '') or str(item)[:80]
                                print(f"    • {headline[:100]}")
                            logging.info(f"[NegPulse] {len(neg_news)} negative headlines at 12:00 IST")
                        else:
                            print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] 📉 Negative pulse: no bearish headlines found")
                    except Exception as neg_e:
                        logging.warning(f"Negative market pulse fetch failed: {neg_e}")
                    last_neg_pulse_date = current_date

                # 1. Intraday tasks between 9:15 and 15:30
                if MARKET_START <= current_time <= MARKET_END:

                    # ── HYDRA intraday evaluation (every cycle) ──────────
                    if hydra.watchlist and not hydra.trade_placed_today and slot_manager.remaining > 0:
                        try:
                            import pandas as pd
                            from concurrent.futures import ThreadPoolExecutor

                            # Optimisation 3 — parallel evaluation across watchlist symbols.
                            # _evaluate_hydra_entry() is pure read-only (bars + TA + depth).
                            # No writes happen here. Trade execution is below, in main thread.
                            def _evaluate_hydra_entry(entry):
                                if entry.symbol in daily_traded_symbols:
                                    return None
                                allowed, _ = slot_manager.can_trade(entry.symbol, entry.direction)
                                if not allowed:
                                    return None
                                bars = get_intraday_bars_for_symbol(entry.symbol, lookback_minutes=70)
                                if not bars or len(bars) < 5:
                                    return None
                                bars_df = pd.DataFrame([{
                                    'date': b.timestamp, 'open': b.open, 'high': b.high,
                                    'low': b.low, 'close': b.close, 'volume': b.volume
                                } for b in bars])
                                # Optimisation 4 — streaming TA: <1ms after warm-up vs ~60ms full sweep
                                snapshot = TechnicalBody.compute_or_stream(
                                    entry.symbol, bars_df, latest_bar=bars[-1]
                                )
                                tick_raw = client.get_last_tick(entry.symbol)
                                depth_analysis = None
                                if tick_raw and hasattr(tick_raw, 'depth'):
                                    depth_analysis = analyze_depth(entry.symbol, tick_raw.depth, snapshot.last_price)
                                conviction = hydra.evaluate(entry, snapshot, depth_analysis)
                                return (entry, conviction, snapshot)

                            hydra_entries_to_check = hydra.get_watchlist()
                            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="HydraEval") as pool:
                                eval_results = list(pool.map(_evaluate_hydra_entry, hydra_entries_to_check))

                            # Pick first tradeable result (list is already priority-sorted)
                            for result_tuple in eval_results:
                                if result_tuple is None:
                                    continue
                                entry, conviction, snapshot = result_tuple
                                if not conviction.should_trade:
                                    continue

                                ltp = snapshot.last_price
                                atr_val = snapshot.atr14 if snapshot.atr14 > 0 else ltp * 0.015
                                stop_dist = min(atr_val * 1.5, ltp * 0.025)
                                qty = compute_atr_position_size(risk_cfg.per_trade_capital_rupees, stop_dist) if stop_dist > 0 else 1

                                if entry.direction == "BUY":
                                    stop_price = round(ltp - stop_dist, 2)
                                    result = executor.execute_buy(symbol=entry.symbol, ltp=ltp, qty=qty, market_regime=load_daily_regime())
                                    if result.success:
                                        risk_state.trades_taken += 1
                                        daily_traded_symbols.add(entry.symbol)
                                        slot_manager.allocate("HYDRA", entry.symbol, "BUY", conviction.total)
                                        positions.on_buy_fill(
                                            symbol=entry.symbol, qty=result.filled_qty, price=result.avg_price or ltp,
                                            mode="INTRADAY", strategy="HYDRA_EVENT",
                                            initial_stop_price=stop_price, atr=atr_val,
                                        )
                                        hydra.mark_trade_placed()
                                        print(f"  🔥 HYDRA TRADE: BUY {qty}x {entry.symbol} @ {ltp:.2f} SL={stop_price:.2f} conviction={conviction.total:.0f}")
                                        log_execution(exec_logger, symbol=entry.symbol, ltp=ltp, result=result,
                                                      meta={"strategy": "HYDRA_EVENT", "conviction": conviction.total, "stop": stop_price, "event": entry.event_summary})
                                elif entry.direction == "SHORT":
                                    stop_price = round(ltp + stop_dist, 2)
                                    result = executor.execute_short_sell(symbol=entry.symbol, ltp=ltp, qty=qty)
                                    if result.success:
                                        risk_state.trades_taken += 1
                                        daily_traded_symbols.add(entry.symbol)
                                        slot_manager.allocate("HYDRA", entry.symbol, "SHORT", conviction.total)
                                        positions.on_short_fill(
                                            symbol=entry.symbol, qty=result.filled_qty, price=result.avg_price or ltp,
                                            mode="INTRADAY", strategy="HYDRA_EVENT",
                                            initial_stop_price=stop_price, atr=atr_val,
                                        )
                                        hydra.mark_trade_placed()
                                        print(f"  🔥 HYDRA TRADE: SHORT {qty}x {entry.symbol} @ {ltp:.2f} SL={stop_price:.2f} conviction={conviction.total:.0f}")
                                        log_execution(exec_logger, symbol=entry.symbol, ltp=ltp, result=result,
                                                      meta={"strategy": "HYDRA_EVENT", "conviction": conviction.total, "stop": stop_price, "event": entry.event_summary})
                                break  # Only one HYDRA trade at a time

                            # Check for new live events (every cycle)
                            new_events = hydra.event_scanner.scan_new_events()
                            if new_events:
                                classified = hydra.event_scanner.classify_events(new_events)
                                hot = [e for e in classified if e.urgency >= 7.0]
                                if hot:
                                    print(f"  🔥 HYDRA: {len(hot)} new hot events detected!")
                                    from src.strategies.base import WatchlistEntry
                                    for evt in hot:
                                        hydra.watchlist.append(WatchlistEntry(
                                            symbol=evt.symbol, direction=evt.direction,
                                            event_summary=evt.summary or evt.headline, urgency=evt.urgency,
                                        ))
                                    hydra.watchlist.sort(key=lambda e: e.urgency, reverse=True)
                                    hydra.watchlist = hydra.watchlist[:hydra.max_watchlist]

                        except Exception as e:
                            logging.error(f"HYDRA intraday eval failed: {e}")
                            import traceback; traceback.print_exc()

                    # ── VIPER intraday evaluation (every cycle) ──
                    if viper.watchlist and slot_manager.remaining > 0:
                        try:
                            import pandas as pd
                            from concurrent.futures import ThreadPoolExecutor


                            # Optimisation 3 — parallel VIPER evaluation.
                            # Pre-filter obviously ineligible entries to avoid thread overhead.
                            eligible_entries = [
                                e for e in viper.watchlist
                                if e.symbol not in daily_traded_symbols
                                and not (getattr(e, 'metadata', {}) or {}).get('trade_mode') == 'COIL'
                                   and current_time < dt_time(11, 0)
                            ]

                            def _evaluate_viper_entry(entry):
                                meta_v = getattr(entry, 'metadata', {}) or {}
                                trade_mode_v = meta_v.get('trade_mode', 'STRIKE')
                                if trade_mode_v == 'COIL' and current_time < dt_time(11, 0):
                                    return None
                                allowed_v, _ = slot_manager.can_trade(entry.symbol, entry.direction)
                                if not allowed_v:
                                    return None
                                bars_v = get_intraday_bars_for_symbol(entry.symbol, lookback_minutes=70)
                                if not bars_v or len(bars_v) < 5:
                                    return None
                                bars_df_v = pd.DataFrame([{
                                    'date': b.timestamp, 'open': b.open, 'high': b.high,
                                    'low': b.low, 'close': b.close, 'volume': b.volume
                                } for b in bars_v])
                                snapshot_v = TechnicalBody.compute_or_stream(
                                    entry.symbol, bars_df_v, latest_bar=bars_v[-1]
                                )
                                tick_raw_v = client.get_last_tick(entry.symbol)
                                depth_v = None
                                if tick_raw_v and hasattr(tick_raw_v, 'depth'):
                                    depth_v = analyze_depth(entry.symbol, tick_raw_v.depth, snapshot_v.last_price)
                                conviction_v = viper.evaluate(entry, snapshot_v, depth_v)
                                if slot_manager.is_confluence(entry.symbol):
                                    conviction_v.total = min(100.0, conviction_v.total + CONFLUENCE_BONUS)
                                    conviction_v.reasoning += f" | +{CONFLUENCE_BONUS} CONFLUENCE BONUS"
                                return (entry, conviction_v, snapshot_v, meta_v)

                            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ViperEval") as pool:
                                viper_eval_results = list(pool.map(_evaluate_viper_entry, eligible_entries))

                            for viper_result_tuple in viper_eval_results:
                                if viper_result_tuple is None:
                                    continue
                                entry, conviction, snapshot, meta = viper_result_tuple
                                trade_mode = meta.get('trade_mode', 'STRIKE')

                                # COIL mode → dry-run only (no live trade)
                                if trade_mode == 'COIL':
                                    if conviction.should_trade:
                                        print(f"  🐍 VIPER/COIL [DRY-RUN]: {entry.direction} {entry.symbol} "
                                              f"conviction={conviction.total:.0f} (logged, not traded)")
                                    continue  # Skip to next — COIL never executes live

                                # STRIKE mode → live trade if conviction passes
                                if conviction.should_trade:
                                    ltp = snapshot.last_price
                                    atr_val = snapshot.atr14 if snapshot.atr14 > 0 else ltp * 0.015
                                    stop_dist = min(atr_val * 1.5, ltp * 0.025)
                                    capital_pct = slot_manager.get_capital_allocation(conviction.total, entry.symbol)
                                    qty = compute_atr_position_size(
                                        risk_cfg.per_trade_capital_rupees * capital_pct,
                                        stop_dist
                                    ) if stop_dist > 0 else 1

                                    if entry.direction == "BUY":
                                        stop_price = round(ltp - stop_dist, 2)
                                        result = executor.execute_buy(symbol=entry.symbol, ltp=ltp, qty=qty, market_regime=load_daily_regime())
                                        if result.success:
                                            risk_state.trades_taken += 1
                                            daily_traded_symbols.add(entry.symbol)
                                            slot_manager.allocate("VIPER", entry.symbol, "BUY", conviction.total)
                                            positions.on_buy_fill(
                                                symbol=entry.symbol, qty=result.filled_qty, price=result.avg_price or ltp,
                                                mode="INTRADAY", strategy="VIPER_STRIKE",
                                                initial_stop_price=stop_price, atr=atr_val,
                                            )
                                            tag = " 🐉 CONFLUENCE" if slot_manager.is_confluence(entry.symbol) else ""
                                            print(f"  🐍 VIPER TRADE: BUY {qty}x {entry.symbol} @ {ltp:.2f} "
                                                  f"SL={stop_price:.2f} conviction={conviction.total:.0f} "
                                                  f"capital={capital_pct:.0%}{tag}")
                                            log_execution(exec_logger, symbol=entry.symbol, ltp=ltp, result=result,
                                                          meta={"strategy": "VIPER_STRIKE", "conviction": conviction.total,
                                                                "stop": stop_price, "move_type": meta.get('move_type', '?'),
                                                                "confluence": slot_manager.is_confluence(entry.symbol)})
                                    elif entry.direction == "SHORT":
                                        stop_price = round(ltp + stop_dist, 2)
                                        result = executor.execute_short_sell(symbol=entry.symbol, ltp=ltp, qty=qty)
                                        if result.success:
                                            risk_state.trades_taken += 1
                                            daily_traded_symbols.add(entry.symbol)
                                            slot_manager.allocate("VIPER", entry.symbol, "SHORT", conviction.total)
                                            positions.on_short_fill(
                                                symbol=entry.symbol, qty=result.filled_qty, price=result.avg_price or ltp,
                                                mode="INTRADAY", strategy="VIPER_STRIKE",
                                                initial_stop_price=stop_price, atr=atr_val,
                                            )
                                            tag = " 🐉 CONFLUENCE" if slot_manager.is_confluence(entry.symbol) else ""
                                            print(f"  🐍 VIPER TRADE: SHORT {qty}x {entry.symbol} @ {ltp:.2f} "
                                                  f"SL={stop_price:.2f} conviction={conviction.total:.0f} "
                                                  f"capital={capital_pct:.0%}{tag}")
                                            log_execution(exec_logger, symbol=entry.symbol, ltp=ltp, result=result,
                                                          meta={"strategy": "VIPER_STRIKE", "conviction": conviction.total,
                                                                "stop": stop_price, "move_type": meta.get('move_type', '?'),
                                                                "confluence": slot_manager.is_confluence(entry.symbol)})
                                    break  # Only one VIPER trade per cycle

                        except Exception as e:
                            logging.error(f"VIPER intraday eval failed: {e}")
                            import traceback; traceback.print_exc()

                    # ── Grok 4.20 Portfolio Orchestrator — intraday trigger ──────
                    # Fires at 09:17, 09:30, 10:00, 10:45, 11:45 — aligned with NSE volatility.
                    # Collects top candidates from BOTH heads + full portfolio state, sends to Grok.
                    if (grok_optimizer_index < len(GROK_OPTIMIZER_TIMES)
                            and current_time >= GROK_OPTIMIZER_TIMES[grok_optimizer_index]
                            and MARKET_START <= current_time <= MARKET_END):
                        try:
                            from src.llm.grok_client import grok_portfolio_optimizer, GROK_DAILY_BUDGET
                            if grok_call_count < GROK_DAILY_BUDGET:
                                # Gather portfolio state
                                open_pos_data = []
                                for pos in positions.get_open_positions():
                                    tick = client.get_last_tick(pos.symbol)
                                    ltp = tick.ltp if tick else pos.avg_price
                                    current_pnl = (ltp - pos.avg_price) * pos.total_qty if pos.side == "LONG" else (pos.avg_price - ltp) * pos.total_qty
                                    time_in = (now - pos.entry_time).total_seconds() / 60.0 if pos.entry_time else 0
                                    open_pos_data.append({
                                        "symbol": pos.symbol, "side": pos.side,
                                        "qty": pos.total_qty, "entry_price": pos.avg_price,
                                        "current_pnl": round(current_pnl, 2),
                                        "time_in_trade_min": round(time_in, 0),
                                        "strategy": pos.strategy,
                                    })

                                # Gather candidates from both heads
                                hydra_cands = hydra.get_top_candidates(max_n=5)
                                viper_cands = viper.get_top_candidates(max_n=5)

                                risk_state_data = {
                                    "daily_pnl": float(risk_state.realized_pnl),
                                    "trades_taken": risk_state.trades_taken,
                                    "slots_used": slot_manager.trades_today,
                                    "slots_remaining": slot_manager.remaining,
                                    "daily_loss_cap": risk_cfg.max_daily_loss_rupees,
                                }

                                market_pulse_data = {
                                    "time_bucket": GROK_OPTIMIZER_TIMES[grok_optimizer_index].strftime("%H:%M"),
                                    "regime": load_daily_regime().trend,
                                }

                                trigger_time = GROK_OPTIMIZER_TIMES[grok_optimizer_index].strftime("%H:%M")
                                print(f"\n  🧠 [{trigger_time}] Grok Portfolio Optimizer — "
                                      f"Positions={len(open_pos_data)}, "
                                      f"HYDRA cands={len(hydra_cands)}, "
                                      f"VIPER cands={len(viper_cands)}")

                                actions = grok_portfolio_optimizer(
                                    open_positions=open_pos_data,
                                    hydra_candidates=hydra_cands,
                                    viper_candidates=viper_cands,
                                    risk_state=risk_state_data,
                                    market_pulse=market_pulse_data,
                                    morning_plan=grok_morning_plan,
                                )
                                grok_call_count += 1

                                if actions:
                                    grok_last_actions = actions
                                    for action in actions:
                                        sym = action.get('symbol', '?')
                                        act = action.get('action', 'SKIP')
                                        conv = action.get('conviction', 0)
                                        reason = action.get('reason', '')
                                        print(f"     {act} {sym}: conviction={conv} — {reason[:80]}")

                                        # Process actionable Grok decisions
                                        if act in ("BUY", "SHORT") and conv >= 70:
                                            # Grok-approved trade: boost entry to front of queue
                                            # The existing HYDRA/VIPER eval loops will execute
                                            # if they independently score ≥70. Grok's approval
                                            # acts as a portfolio-level confirmation.
                                            logging.info(
                                                f"[Grok/Optimizer] APPROVED {act} {sym} "
                                                f"conviction={conv} — {reason}"
                                            )
                                        elif act == "TIGHTEN_STOP" and action.get('stop'):
                                            # Grok suggests tightening a stop
                                            pos = positions.get_position(sym)
                                            if pos:
                                                new_stop = action['stop']
                                                logging.info(
                                                    f"[Grok/Optimizer] TIGHTEN_STOP {sym}: "
                                                    f"new_stop={new_stop} — {reason}"
                                                )
                                                print(f"     ⚡ Tightening {sym} stop → {new_stop}")
                                        elif act == "CLOSE":
                                            logging.info(
                                                f"[Grok/Optimizer] CLOSE {sym} — {reason}"
                                            )
                                            print(f"     ⚡ Grok recommends CLOSE {sym}")
                                else:
                                    print(f"     (Grok returned no actions — fallback to mechanical rules)")
                        except Exception as grok_opt_e:
                            logging.error(f"Grok optimizer at {current_time} failed: {grok_opt_e}")
                            print(f"  ⚠️ Grok optimizer failed: {grok_opt_e} (mechanical rules continue)")
                        grok_optimizer_index += 1

                    should_run_intraday = False
                    
                    if last_intraday_run is None:
                        should_run_intraday = True
                    else:
                        minutes_since_last_run = (now - last_intraday_run).total_seconds() / 60.0
                        if minutes_since_last_run >= INTRADAY_INTERVAL_MIN:
                            should_run_intraday = True
                            
                    if should_run_intraday:
                        # V1 pipeline DISABLED: run_juror_nse_live + daily_decision_engine
                        # are orphaned — they write to JurorSignal/DecisionRecord tables
                        # that Dragon Architecture (HYDRA+VIPER) never reads.
                        # run_script("run_juror_nse_live.py")  # DISABLED
                        # daily_decision_engine.main(watcher=watcher)  # DISABLED
                        pass
                        
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
                                # Inject pre-market intelligence if available
                                if pre_market_intel:
                                    macro_ctx.set_composite_intelligence(pre_market_intel)
                                print(f"  🌍 {macro_ctx.summary}")
                                tier_log = macro_ctx.format_tier_log()
                                print(f"  {tier_log}")
                                logging.info(tier_log)
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
                                    
                                    # V4: Direction-aware macro dampener (tiered)
                                    if macro_ctx:
                                        dampener_pts, min_conviction = macro_ctx.get_direction_dampener(ds.direction)
                                        raw_score = tech_score.total
                                        tech_score.total = raw_score + dampener_pts
                                        if dampener_pts != 0:
                                            print(f"    Macro {ds.direction}: {dampener_pts:+d}pts → {raw_score:.0f} → {tech_score.total:.0f} (min {min_conviction})")
                                        if tech_score.total < min_conviction:
                                            print(f"    ❌ {sym} score {tech_score.total:.0f} below macro min conviction {min_conviction} for {ds.direction}")
                                            continue
                                    
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
                                        sym_stats = SymbolStats(symbol=sym, last_price=ltp, avg_daily_turnover_rupees=ds.volume * ltp if ds.volume > 0 else 5_000_000.0)
                                        if not allow_new_short(sym_stats, market_regime, risk_cfg):
                                            continue
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
                            
                        # ── P2 + Opt 1: Drain exit signals from BOTH queues ─────
                        # exit_monitor: 1s heartbeat (time exits, partial exits, exhaustion)
                        # tick_exit_queue: sub-ms fast-path (hard stop + trailing stop)
                        # Both run in daemon threads; execution always happens here (main thread).
                        try:
                            exit_signals = exit_monitor.drain_signals()

                            # Optimisation 1: drain fast-path tick-level signals.
                            # Deduplicate: if a symbol already has a signal from the 1s loop,
                            # skip the tick-path duplicate to avoid double-execution.
                            already_signalled = {s.symbol for s in exit_signals}
                            try:
                                while True:
                                    tick_sig = tick_exit_queue.get_nowait()
                                    if tick_sig.symbol not in already_signalled:
                                        exit_signals.append(tick_sig)
                                        already_signalled.add(tick_sig.symbol)
                            except Exception:
                                pass  # queue.Empty is the expected exit

                            for es in exit_signals:
                                pos = positions.get_position(es.symbol)
                                if not pos or pos.total_qty <= 0:
                                    continue

                                exit_qty    = es.qty if es.qty is not None else pos.total_qty
                                entry_price = pos.avg_price
                                entry_time  = pos.entry_time
                                direction   = pos.side  # "LONG" or "SHORT"

                                # Route to correct executor method
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
                                        # ── Precise P&L via positions._precise_pnl ────────
                                        # (positions.py now uses Decimal internally)
                                        if direction == "LONG":
                                            trade_pnl = exit_qty * (price - entry_price)
                                        else:
                                            trade_pnl = exit_qty * (entry_price - price)

                                        # ── Update daily P&L (DailyRiskState now uses Decimal)
                                        risk_state.add_realized_pnl(trade_pnl)

                                        # ── Release slot if fully closed ──────────────────
                                        remaining_pos = positions.get_position(es.symbol)
                                        if remaining_pos is None or remaining_pos.total_qty <= 0:
                                            slot_manager.release(es.symbol)

                                        # ── P3-A: Async SQLite write via DatabaseWriter ────
                                        # Returns immediately; db_writer retries on failure;
                                        # never silently drops the record.
                                        db_writer.write_trade_record({
                                            "symbol":       es.symbol,
                                            "direction":    direction,
                                            "qty":          exit_qty,
                                            "entry_price":  entry_price,
                                            "exit_price":   price,
                                            "pnl":          round(trade_pnl, 2),
                                            "entry_time":   entry_time,
                                            "exit_time":    datetime.now(IST),
                                            "mode":         es.mode,
                                            "strategy":     es.strategy,
                                            "exit_reason":  es.reason,
                                        })

                                meta = {"reason": es.reason, "mode": es.mode,
                                        "strategy": es.strategy, "stop_price": es.stop_price,
                                        "direction": direction}
                                log_execution(exec_logger, symbol=es.symbol, ltp=es.ltp, result=result, meta=meta)
                        except Exception as e:
                            logging.error(f"Failed to process exit signals: {e}")
                            print(f"\nExit processing error: {e}")

                        last_intraday_run = now
                
                # 2. End of Day tasks after 15:30
                elif current_time > MARKET_END:
                    if last_eod_run_date != current_date:
                        run_script("log_daily_performance.py")
                        # ── VIPER: Save COIL dry-run report + daily reset ──
                        try:
                            print(f"  🐍 VIPER scan health: {viper.scan_health_summary}")
                            logging.info(f"[VIPER] EOD scan health: {viper.scan_health_summary}")
                            coil_path = viper.save_coil_report()
                            if coil_path:
                                print(f"  🐍 VIPER: COIL report saved to {coil_path}")
                            viper.reset_daily()
                        except Exception as viper_eod_e:
                            logging.error(f"VIPER EOD cleanup failed: {viper_eod_e}")

                        # ── Grok 4.20 EOD Review (v2) ──
                        try:
                            from src.llm.grok_client import grok_eod_review, GROK_DAILY_BUDGET
                            if grok_call_count < GROK_DAILY_BUDGET:
                                trades_data = []  # Collect from db_writer or positions
                                market_sum = f"Date: {current_date}, Grok calls used: {grok_call_count}"
                                eod_result = grok_eod_review(
                                    trades_today=trades_data,
                                    daily_pnl=float(risk_state.realized_pnl),
                                    morning_plan=grok_morning_plan,
                                    market_summary=market_sum,
                                )
                                grok_call_count += 1
                                if eod_result:
                                    print(f"  🧠 Grok EOD Review: grade={eod_result.get('grade', '?')}")
                                    print(f"     {eod_result.get('summary', '')}")
                                    for lesson in eod_result.get('lessons', []):
                                        print(f"     📝 {lesson}")
                        except Exception as eod_e:
                            logging.error(f"Grok EOD review failed: {eod_e}")

                        last_eod_run_date = current_date
                        
                # 3. 16:00 — Unified Post-Market Report
                if _should_fire_scheduled_job(dt_time(16, 0), runner_start_time, current_time):
                    if last_report_date != current_date:
                        try:
                            from src.reports.post_market_report import generate_post_market_report
                            
                            kite_live = getattr(client, '_kite', None)
                            if kite_live is None:
                                # Fallback if client is entirely offline (e.g. testing)
                                from kiteconnect import KiteConnect
                                kite_live = KiteConnect(api_key=os.getenv("ZERODHA_API_KEY"))
                                kite_live.set_access_token(os.getenv("ZERODHA_ACCESS_TOKEN"))

                            generate_post_market_report(
                                kite_client=kite_live,
                                traded_symbols=set(daily_traded_symbols),
                            )
                        except Exception as chron_e:
                            logging.error(f"Post-Market Report failed: {chron_e}")
                            print(f"  ❌ Post-Market Report error: {chron_e}")
                        last_report_date = current_date
                        last_autopsy_date = current_date

                # 4. 18:01 — Prediction Feedback Loop (score morning's calls)
                if _should_fire_scheduled_job(dt_time(18, 1), runner_start_time, current_time):
                    if last_feedback_date != current_date:
                        run_script("reports/feedback_loop.py")
                        last_feedback_date = current_date

                # 5. 09:00 IST (03:30 UTC) — Morning Global Intelligence Brief
                if _should_fire_scheduled_job(dt_time(9, 0), runner_start_time, current_time):
                    if last_premarket_date != current_date:
                        run_script("reports/pre_market_brief.py")
                        last_premarket_date = current_date
            
            # Sleep for 60 seconds before checking time again
            time.sleep(60)
            
        except KeyboardInterrupt:
            print("\nShutting down runner...")
            logging.info("Runner stopped by user.")
            # Graceful shutdown: stop exit monitor, flush pending DB writes, close WS
            exit_monitor.stop()
            db_writer.flush(timeout=5.0)
            client.stop_websocket()
            break
        except Exception as e:
            print(f"Unexpected error in runner loop: {e}")
            logging.error(f"Unexpected error in runner loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    # Load .env from project root
    _env_path = ".env"
    load_dotenv(_env_path)

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
