# MODULE: runner.py | PURPOSE: Main scheduler + job dispatcher | RISK: HIGH — timing math, IST/UTC, job sequencing
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
from src.data_ingestion.macro_context import refresh_macro_context, get_cached_context, MacroRiskTier
from src.data_ingestion.nse_scraper import get_deal_signal
from src.data_ingestion.short_ban_list import refresh_ban_lists, get_ban_summary
from src.data_ingestion.pcr_tracker import compute_pcr, get_pcr_score_modifier
from src.trading.depth_analyzer import analyze_depth, get_depth_score_modifier, should_skip_illiquid
from src.tools.auto_login import auto_refresh_access_token
from src.data_ingestion.news_context import NewsClient
from src.strategies.hydra import HydraStrategy
from src.strategies.viper import ViperStrategy
from src.strategies.dawn import DawnStrategy, execute_dawn_dryrun, update_dawn_dryrun, select_dawn_candidates
from src.strategies.slot_manager import SlotManager, CONFLUENCE_BONUS
from src.strategies.technical_body import TechnicalBody, reset_streaming_state, get_or_create_streaming_state, _streaming_states
from src.trading.exit_monitor import ExitMonitorThread
from src.trading.conviction_engine import ConvictionEngine, ActiveSignal
from src.trading.market_phase import fetch_market_snapshot, MarketPhase
from src.db.db_writer import get_db_writer
from src.data_ingestion.candle_cache_writer import write_candle_cache
import sys
from src.utils.market_calendar import is_market_open_today, get_today_holiday_name

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
BAN_LIST_TIME = dt_time(8, 0)    # 08:00 — refresh F&O ban + T2T lists
PREMARKET_TIME= dt_time(8, 30)   # 08:30 — pre-market macro check
HYDRA_SCAN_TIME = dt_time(8, 15)  # 08:15 — HYDRA pre-market event scan
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

    # ── Phase 4: load config.yaml and override runtime params ──────────
    from src.config_loader import (
        get_conviction_threshold, get_max_open_positions, get_max_trades,
        get_per_trade_risk, validate_config,
    )
    try:
        validate_config()
    except ValueError as _cfg_err:
        logging.error(f"[config] validate_config failed: {_cfg_err}")
        print(f"  ❌ config.yaml invalid: {_cfg_err}")
        raise

    cfg_conviction = get_conviction_threshold()
    cfg_max_trades = get_max_trades()
    cfg_per_trade_risk = get_per_trade_risk()
    cfg_max_open_positions = get_max_open_positions()

    risk_cfg = load_risk_config()
    # Env var LIVE_MODE=true wins for live_mode only; all other params: config.yaml wins.
    risk_cfg.live_mode = live_mode
    risk_cfg.per_trade_capital_rupees = float(cfg_per_trade_risk)
    risk_cfg.max_trades_per_day = cfg_max_trades
    risk_cfg.max_open_positions = cfg_max_open_positions
    max_trades_per_day = cfg_max_trades

    print("--- VoltEdgeAI Automated Runner ---")
    print(f"Market Hours: {MARKET_START} to {MARKET_END} IST")
    print(f"Intraday interval: {INTRADAY_INTERVAL_MIN} minutes")
    print("\n--- Risk & Execution Framework ---")
    if risk_cfg.live_mode:
        print("VoltEdge LIVE_MODE = True (LIVE BROKER ORDERS ENABLED)")
    else:
        print("VoltEdge LIVE_MODE = False (DRY_RUN only)")
    print(f"Max Trades / Day: {risk_cfg.max_trades_per_day}")
    print(f"Per-Trade Risk : ₹{risk_cfg.per_trade_capital_rupees:,.2f}")
    print(f"Open Positions : {risk_cfg.max_open_positions}")
    print(
        f"Config: config.yaml (conviction={cfg_conviction}, "
        f"max_trades={cfg_max_trades}, risk=₹{cfg_per_trade_risk:,.0f})"
    )

    # ── Phase 7: live-mode startup confirmation ────────────────────────
    from src.config_loader import get_dry_run, get_live_startup_countdown
    if not get_dry_run():
        countdown = get_live_startup_countdown()
        print("\n" + "=" * 60)
        print("⚠️  LIVE MODE ACTIVE — REAL ORDERS WILL BE PLACED")
        print(f"Conviction threshold : {cfg_conviction}")
        print(f"Max trades/day       : {cfg_max_trades}")
        print(f"Per-trade risk       : ₹{cfg_per_trade_risk:,.0f}")
        print(f"Max open positions   : {cfg_max_open_positions}")
        print(f"Press Ctrl+C within {countdown} seconds to abort...")
        print("=" * 60)
        logging.warning(
            f"LIVE MODE ACTIVE — real orders enabled (conviction={cfg_conviction}, "
            f"max_trades={cfg_max_trades}, risk=₹{cfg_per_trade_risk:,.0f})"
        )
        try:
            time.sleep(countdown)
        except KeyboardInterrupt:
            print("\n[LIVE MODE ABORTED BY USER]")
            logging.warning("Live mode aborted by user during countdown")
            raise
        print("Live mode confirmed — proceeding")
        logging.info("Live mode confirmed — proceeding")
    else:
        print("DRY RUN MODE — no real orders will be placed")

    # P0-B: Email config validation at startup
    from src.reports.email_sender import validate_email_config
    email_status = validate_email_config()
    print(f"  📧 {email_status}")
    logging.info(f"Email config: {email_status}")

    print("\nPress Ctrl+C to stop.\n")

    # ── Holiday Guard: evaluate once at startup ──────────────────────────────
    _market_open_today = is_market_open_today()
    _holiday_name = get_today_holiday_name()
    if not _market_open_today:
        if _holiday_name:
            logging.info(
                f"[Holiday Guard] Market CLOSED today — {_holiday_name}. Market jobs suppressed."
            )
            print(f"  🚫 [Holiday Guard] Market closed — {_holiday_name}. Market jobs suppressed.")
        else:
            logging.info(
                "[Holiday Guard] Market CLOSED today (weekend). Market jobs suppressed."
            )
            print("  🚫 [Holiday Guard] Market closed (weekend). Market jobs suppressed.")

    exec_logger = get_executions_logger()
    exec_logger.info(f"VoltEdge LIVE_MODE = {risk_cfg.live_mode}")
    
    logging.info(f"Runner started. LIVE_MODE: {risk_cfg.live_mode}, MaxTrades: {risk_cfg.max_trades_per_day}")
    
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

    # ── TimesFM cache: initial 5-min candle population ───────────────────────
    try:
        write_candle_cache()
    except Exception as _cwe:
        logging.warning(f"[CacheWriter] Startup candle cache write failed (non-fatal): {_cwe}")

    positions = PositionBook()
    exit_engine = ExitEngine(positions=positions, live_client=client, risk=risk_cfg)
    # v4: Give ExitEngine read access to streaming TA states for RSI divergence detection
    exit_engine.set_streaming_states(_streaming_states)

    # Phase 8h: shadow book for dry-run SL effectiveness validation
    try:
        from src.trading.shadow_book import ShadowBook
        shadow_book: Optional["ShadowBook"] = ShadowBook()
        logging.info("[SHADOW] Shadow PositionBook initialised for dry-run validation")
    except Exception as _sb_e:
        shadow_book = None
        logging.warning(f"[SHADOW] ShadowBook init failed (non-fatal): {_sb_e}")
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
    last_mid_session_date = None  # Mid-session pulse at 12:00
    pre_market_ran      = False   # Track if pre-market brief succeeded today
    last_regime_update  = None    # V3: live regime updates
    last_autopsy_date        = None    # Phase G: EOD autopsy
    last_squareoff_date      = None    # Phase 7: 15:15 live EOD square-off
    runner_start_time        = datetime.now(IST).time()  # Phase I: cascade prevention
    last_candle_cache_write  = None    # TimesFM: periodic 5-min candle refresh
    scanner_long_symbols:  list = []

    # ── Phase K: Dragon Architecture — HYDRA + VIPER Strategies ──
    hydra = HydraStrategy()
    viper = ViperStrategy()
    slot_manager = SlotManager(max_trades=max_trades_per_day)
    last_hydra_scan_date = None
    last_viper_scan_time = None  # Track VIPER re-scans
    viper_rescan_index = 0       # Which re-scan slot are we at
    scanner_short_symbols: list = []

    # ── DAWN: Pre-Market Catalyst Strategy (dry-run) ──
    dawn = DawnStrategy()
    last_dawn_scan_date = None
    last_dawn_pre_scan_date = None  # Prevents pre_market_scan from re-firing every 60s
    last_dawn_manage_time = None
    last_dawn_dryrun_entry_date = None   # HYDRA dryrun entries recorded once at 09:15
    last_dawn_dryrun_update_time = None  # HYDRA dryrun update cadence (15-min)

    # ── DawnHydraRouter output (populated at 08:15, consumed at 09:15) ──
    dawn_candidates_today: list = []
    hydra_shadows_today: list = []

    # ── Mid-session bearish discovery (SHORT-4) ──
    last_neg_pulse_date = None

    # ── SHORT-6/7: Ban list + T2T refresh ──
    last_ban_refresh_date = None

    # ── Pre-market intelligence (v3) ──
    pre_market_intel = None  # PreMarketIntelligence result, cached daily

    # ── Dynamic Conviction Engine (v4) ──
    conviction_engine = ConvictionEngine()
    last_market_snapshot = None  # MarketSnapshot from previous cycle

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
                dawn.reset_daily()
                last_dawn_scan_date = None
                last_dawn_pre_scan_date = None
                last_dawn_manage_time = None
                last_dawn_dryrun_entry_date = None
                last_dawn_dryrun_update_time = None
                slot_manager.reset_daily()
                last_hydra_scan_date = None
                grok_call_count = 0
                grok_morning_plan = None
                grok_optimizer_index = 0
                grok_last_actions = []
                pre_market_intel = None
                try:
                    conviction_engine.persist_watchboard_to_json()
                except Exception as _persist_e:
                    logging.error(f"[ConvEng] persist_watchboard_to_json failed: {_persist_e}")
                conviction_engine.reset_daily()
                last_market_snapshot = None
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

                # ── Holiday Guard: refresh for the new calendar day ──────────
                _market_open_today = is_market_open_today()
                _holiday_name = get_today_holiday_name()
                if not _market_open_today:
                    if _holiday_name:
                        logging.info(
                            f"[Holiday Guard] Market CLOSED today — {_holiday_name}. Market jobs suppressed."
                        )
                    else:
                        logging.info(
                            "[Holiday Guard] Market CLOSED today (weekend). Market jobs suppressed."
                        )

            if is_weekday:
                # -2. Ban List Refresh (08:00 IST) — before pre-market
                if _market_open_today and current_time >= BAN_LIST_TIME and last_ban_refresh_date != current_date:
                    try:
                        refresh_ban_lists()
                        print(f"  🚫 {get_ban_summary()}")
                    except Exception as ban_e:
                        logging.error(f"Ban list refresh failed (fail-open): {ban_e}")
                    last_ban_refresh_date = current_date

                # ── DAWN: Pre-market catalyst scan at 08:30 (runs ONCE) ──
                if _market_open_today and current_time >= dt_time(8, 30) and last_dawn_pre_scan_date != current_date:
                    try:
                        print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] 🌅 DAWN: Pre-market catalyst scan...")
                        dawn_candidates = dawn.pre_market_scan()
                        if dawn_candidates:
                            print(f"  🌅 DAWN: {len(dawn_candidates)} candidates from independent scan")
                        else:
                            print(f"  🌅 DAWN: No candidates from independent scan")
                    except Exception as dawn_scan_e:
                        logging.error(f"DAWN pre-market scan failed: {dawn_scan_e}")
                        print(f"  ❌ DAWN scan error: {dawn_scan_e}")
                    last_dawn_pre_scan_date = current_date  # Prevent re-firing

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
                                    morning_bias = grok_morning_plan.get('morning_regime_bias', 0)
                                    conviction_engine.set_morning_regime_bias(morning_bias)
                                    print(f"  🧠 Grok Morning Strategist: regime={regime_from_grok} bias={morning_bias:+d}")
                                    wl = grok_morning_plan.get('watchlist', [])
                                    for w in wl:
                                        print(f"     #{w.get('priority',0)}: {w.get('symbol','?')} {w.get('bias','?')} — {w.get('catalyst','')[:60]}")
                                    avoids = grok_morning_plan.get('avoid', [])
                                    if avoids:
                                        print(f"     ⚠️ Avoid: {avoids}")
                                    # Inject Grok urgency adjustments into HYDRA watchlist
                                    for w in wl:
                                        delta = w.get('urgency_delta', 0)
                                        if delta != 0:
                                            for he in hydra.watchlist:
                                                if he.symbol == w.get('symbol'):
                                                    he.urgency = max(1.0, min(10.0, he.urgency + delta))
                                                    logger.info(f"[Grok] {he.symbol} urgency adjusted by {delta:+d} → {he.urgency:.0f}")
                                else:
                                    print(f"  🧠 Grok Morning Strategist: returned empty (will use mechanical rules)")
                        except Exception as grok_e:
                            logging.error(f"Grok morning strategist failed: {grok_e}")
                            print(f"  ⚠️ Grok morning call failed: {grok_e} (continuing with mechanical rules)")

                    except Exception as e:
                        logging.error(f"Pre-market oracle failed: {e}")
                    last_premarket_date = current_date

                # 0a. HYDRA pre-market event scan at 08:15
                if _market_open_today and current_time >= HYDRA_SCAN_TIME and last_hydra_scan_date != current_date:
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
                            # Add HYDRA signals to ConvictionEngine watchboard
                            for entry in hydra_entries:
                                conviction_engine.add_signal(ActiveSignal(
                                    symbol=entry.symbol,
                                    direction=entry.direction,
                                    strategy="HYDRA",
                                    layer_c_score=min(100.0, entry.urgency * 10.0),
                                    event_summary=entry.event_summary,
                                    metadata={"urgency": entry.urgency},
                                ))
                        else:
                            print(f"  HYDRA: No hot events found since last close")
                    except Exception as e:
                        logging.error(f"HYDRA pre-market scan failed: {e}")
                        print(f"  ❌ HYDRA scan error: {e}")

                    # ── DawnHydraRouter: decide DAWN vs HYDRA per candidate ──
                    dawn_candidates_today = []
                    hydra_shadows_today = []
                    try:
                        from src.strategies.router import route_candidate, create_hydra_shadow
                        from src.data_ingestion.pre_market_data import (
                            fetch_all_premarket_data, get_scan_universe,
                        )
                        if hydra.watchlist:
                            _router_cache = fetch_all_premarket_data(get_scan_universe())
                            for _entry in hydra.watchlist:
                                _pm = _router_cache.get(_entry.symbol)
                                _decision = route_candidate(_entry, _pm, pattern_db=None)
                                if _decision.route == "DAWN":
                                    dawn_candidates_today.append(_entry)
                                    hydra_shadows_today.append(create_hydra_shadow(_entry))
                                    print(
                                        f"  🌅 Router: {_entry.symbol} → DAWN | {_decision.reasoning}"
                                    )
                                else:
                                    print(
                                        f"  🔥 Router: {_entry.symbol} → HYDRA | {_decision.reasoning}"
                                    )
                        else:
                            logging.info("[Router] HYDRA watchlist empty — no routing needed")
                    except Exception as _route_e:
                        logging.error(f"DawnHydraRouter failed: {_route_e}")
                        print(f"  ❌ Router error: {_route_e}")

                    # ── Phase 3B: persist hydra_shadows to disk for counterfactual tracking ──
                    try:
                        from src.config_loader import get_shadows_dir, get_shadows_persist
                        if not get_shadows_persist():
                            logging.info("[Router] shadows_persist=False via config.yaml — skipping persistence")
                        else:
                            import json as _json
                            from pathlib import Path as _Path
                            _shadow_path = _Path(get_shadows_dir()) / f"hydra_shadows_{current_date.isoformat()}.json"
                            _shadow_payload = []
                            for _sh, _ent in zip(hydra_shadows_today, dawn_candidates_today):
                                _shadow_payload.append({
                                    "symbol": _sh.get("symbol"),
                                    "confidence": float(getattr(_ent, "confidence", 0.0) or 0.0),
                                    "route": getattr(_ent, "route", "") or "DAWN",
                                    "routed_at": getattr(_ent, "routed_at", None),
                                    "momentum_score": float(_ent.metadata.get("momentum_score", 0.0)) if isinstance(getattr(_ent, "metadata", None), dict) else 0.0,
                                    "avg_volume_20d": int(getattr(_ent, "avg_volume_20d", 0) or 0),
                                    "filing_category": getattr(_ent, "filing_category", "") or "",
                                    "filing_urgency": float(getattr(_ent, "filing_urgency", 0.0) or 0.0),
                                    "direction": _sh.get("direction"),
                                    "gap_pct": _sh.get("gap_pct"),
                                    "dawn_entry_time": _sh.get("dawn_entry_time"),
                                    "event_summary": _sh.get("event_summary"),
                                })
                            _shadow_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(_shadow_path, "w") as _f:
                                _json.dump(_shadow_payload, _f, indent=2, default=str)
                            logging.info(f"[Router] Persisted {len(_shadow_payload)} hydra_shadows to {_shadow_path}")
                            print(f"  💾 Persisted {len(_shadow_payload)} HYDRA shadows to {_shadow_path.name}")
                    except Exception as _persist_e:
                        logging.error(f"hydra_shadows persistence failed: {_persist_e}")

                    last_hydra_scan_date = current_date

                # 0b. Run momentum scanner + stock discovery at 09:30
                if _market_open_today and current_time >= SCANNER_TIME and last_scanner_date != current_date:
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
                            viper_entries = viper.scan(access_token=access_token, movers=movers)
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

                                # Add VIPER signals to ConvictionEngine watchboard
                                # STRIKE signals = live candidates; COIL signals = dry-run observation only
                                _coil_added_today = sum(
                                    1 for s in conviction_engine._watchboard.values()
                                    if getattr(s, "is_dry_run", False)
                                )
                                for ve in viper_entries:
                                    meta = getattr(ve, 'metadata', {}) or {}
                                    if meta.get('trade_mode') == 'COIL':
                                        # Add COIL as dry-run observation (cap at 5 per day)
                                        if _coil_added_today < 5:
                                            conviction_engine.add_signal(ActiveSignal(
                                                symbol=ve.symbol,
                                                direction=ve.direction,
                                                strategy="VIPER-COIL",
                                                layer_c_score=min(100.0, ve.urgency * 10.0),
                                                event_summary=ve.event_summary,
                                                metadata=meta,
                                                is_dry_run=True,
                                            ))
                                            _coil_added_today += 1
                                        continue
                                    conviction_engine.add_signal(ActiveSignal(
                                        symbol=ve.symbol,
                                        direction=ve.direction,
                                        strategy="VIPER",
                                        layer_c_score=min(100.0, ve.urgency * 10.0),
                                        event_summary=ve.event_summary,
                                        metadata=meta,
                                    ))
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

                    # ── Phase 8g: arm ORB stop for DAWN positions after 09:30 ──
                    try:
                        for _pos in positions.get_open_positions():
                            if (
                                _pos.strategy == "DAWN"
                                and _pos.side == "LONG"
                                and _pos.orb_stop_low is None
                                and bool((_pos.strategy_config or {}).get("orb_stop_enabled", False))
                            ):
                                _orb_bars = get_intraday_bars_for_symbol(_pos.symbol, lookback_minutes=20)
                                if _orb_bars:
                                    _orb_low = min(b.low for b in _orb_bars)
                                    _pos.orb_stop_low = round(_orb_low, 2)
                                    logging.info(
                                        f"[ORB-ARM] {_pos.symbol} ORB low armed @ {_pos.orb_stop_low:.2f}"
                                    )
                    except Exception as _orb_e:
                        logging.warning(f"[ORB-ARM] failed: {_orb_e}")

                # 0c. VIPER re-scans at 10:00, 10:30, 11:00, 12:00
                if (_market_open_today
                        and viper_rescan_index < len(VIPER_RESCAN_TIMES)
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
                if (_market_open_today
                        and current_time >= dt_time(12, 0) and current_time <= dt_time(12, 5)
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

                # 0e. 12:22 IST — Mid-Session Pulse Report
                if _market_open_today and _should_fire_scheduled_job(dt_time(12, 22), runner_start_time, current_time):
                    if last_mid_session_date != current_date:
                        try:
                            from src.reports.mid_session_pulse import generate_mid_session_pulse
                            generate_mid_session_pulse(
                                conviction_engine=conviction_engine,
                                market_snapshot=last_market_snapshot,
                                positions=positions,
                                slot_manager=slot_manager,
                                risk_state=risk_state,
                                risk_cfg=risk_cfg,
                                live_client=client,
                            )
                            print(f"  📋 Mid-session pulse report sent")
                        except Exception as mid_e:
                            logging.error(f"Mid-session pulse failed: {mid_e}")
                            print(f"  ❌ Mid-session pulse failed: {mid_e}")
                        last_mid_session_date = current_date

                # ── TimesFM cache: refresh 5-min candles every 15 minutes ──────
                if _market_open_today and MARKET_START <= current_time <= MARKET_END:
                    if (last_candle_cache_write is None or
                            (now - last_candle_cache_write).total_seconds() >= 900):
                        try:
                            write_candle_cache()
                            last_candle_cache_write = now
                        except Exception as _cwe:
                            logging.warning(f"[CacheWriter] Periodic candle cache write failed: {_cwe}")

                # 1. Intraday tasks between 9:15 and 15:30
                if _market_open_today and MARKET_START <= current_time <= MARKET_END:

                    # ── Phase 8f: Daily loss circuit breaker ─────────────────
                    try:
                        _should_flatten, _cb_reason = risk_state.check_halt(
                            per_trade_risk=risk_cfg.per_trade_capital_rupees
                        )
                        if _should_flatten and _cb_reason:
                            logging.critical(
                                f"[CIRCUIT BREAKER] {_cb_reason} — flattening all positions"
                            )
                            print(f"  🚨 CIRCUIT BREAKER: {_cb_reason}")
                            try:
                                _flatten_signals = exit_engine.flatten_all(_cb_reason)
                                for _fs in _flatten_signals:
                                    if _fs.side == "SELL":
                                        executor.execute_sell(
                                            symbol=_fs.symbol, qty=_fs.qty or 9999,
                                            ltp=_fs.ltp,
                                        )
                                    elif _fs.side == "COVER":
                                        executor.execute_short_cover(
                                            symbol=_fs.symbol, qty=_fs.qty or 9999,
                                            ltp=_fs.ltp,
                                        )
                                    positions.on_sell_fill(_fs.symbol, _fs.qty or 9999, _fs.ltp)
                                try:
                                    from src.reports.email_sender import send_alert_email
                                    send_alert_email(
                                        subject=f"VoltEdgeAI CIRCUIT BREAKER: {_cb_reason[:80]}",
                                        body=f"Daily P&L: ₹{risk_state.daily_pnl:.0f}\n{_cb_reason}",
                                    )
                                except Exception as _email_e:
                                    logging.warning(f"[CB] alert email failed: {_email_e}")
                            except Exception as _flat_e:
                                logging.error(f"[CIRCUIT BREAKER] flatten failed: {_flat_e}")
                        elif _cb_reason and risk_state.is_halted:
                            logging.warning(f"[CIRCUIT BREAKER] {_cb_reason} — new entries blocked")
                    except Exception as _cb_e:
                        logging.error(f"[CIRCUIT BREAKER] check failed: {_cb_e}")

                    # ── DAWN: Record virtual entries at market open ──
                    if dawn._signals and dt_time(9, 15) <= current_time <= dt_time(9, 20):
                        if any(s.status == "PENDING" for s in dawn._signals):
                            try:
                                dawn.record_entries(live_client=client)
                                active = sum(1 for s in dawn._signals if s.status == "ACTIVE")
                                print(f"  🌅 DAWN: {active} virtual entries recorded at open")
                            except Exception as dawn_entry_e:
                                logging.error(f"DAWN entry recording failed: {dawn_entry_e}")

                    # ── DAWN: entries at 09:15 (once per day) ──────────────
                    if dt_time(9, 15) <= current_time <= dt_time(9, 20) and last_dawn_dryrun_entry_date != current_date:
                        try:
                            hydra_dawn_picks = select_dawn_candidates(
                                min_conviction=50,
                                router_filter=dawn_candidates_today,
                            )
                            execute_dawn_dryrun(hydra_dawn_picks, {})
                            if hydra_dawn_picks:
                                print(f"  🌅 DAWN DRYRUN: {len(hydra_dawn_picks)} HYDRA entries recorded")

                            # ── Phase 8e: Live execution path for DAWN ────────────
                            if risk_cfg.live_mode and hydra_dawn_picks and not risk_state.is_halted:
                                from src.config_loader import (
                                    get_stoploss_config, apply_conviction_modifier, get_max_stop_pct,
                                    get_conviction_threshold,
                                )
                                from src.trading.atr import compute_daily_atr_from_cache
                                dawn_sl_cfg = get_stoploss_config("DAWN")
                                for pick in hydra_dawn_picks:
                                    sym = pick.get("symbol", "")
                                    conviction_score = float(pick.get("dawn_conviction", 0.0))
                                    if not sym or conviction_score < get_conviction_threshold():
                                        continue
                                    if sym in daily_traded_symbols:
                                        continue
                                    if risk_state.trades_taken >= risk_cfg.max_trades_per_day:
                                        break
                                    if risk_state.is_halted:
                                        break
                                    allowed, _ = slot_manager.can_trade(sym, "BUY")
                                    if not allowed:
                                        continue
                                    tick = client.get_last_tick(sym)
                                    ltp = tick.ltp if tick else 0.0
                                    if ltp <= 0:
                                        continue

                                    # ATR: best-effort from daily cache; fallback to max_stop_pct
                                    atr_val = compute_daily_atr_from_cache(sym, ltp=ltp) or ltp * (get_max_stop_pct() / 100.0)

                                    # Conviction-weighted stop distance
                                    raw_mult = float(dawn_sl_cfg.get("initial_atr_multiplier", 1.5))
                                    adj_mult = apply_conviction_modifier(raw_mult, conviction_score)
                                    stop_dist = min(
                                        atr_val * adj_mult,
                                        ltp * (get_max_stop_pct() / 100.0),
                                    )
                                    stop_price = round(ltp - stop_dist, 2)
                                    qty = max(1, int(risk_cfg.per_trade_capital_rupees / stop_dist)) if stop_dist > 0 else 1

                                    result = executor.execute_buy(
                                        symbol=sym, ltp=ltp, qty=qty,
                                        route="DAWN", confidence=conviction_score / 100.0,
                                        sl=stop_price, conviction=conviction_score,
                                        atr=atr_val, strategy_config=dawn_sl_cfg,
                                    )
                                    if result.success:
                                        risk_state.trades_taken += 1
                                        daily_traded_symbols.add(sym)
                                        slot_manager.allocate("DAWN", sym, "BUY", conviction_score)
                                        positions.on_buy_fill(
                                            symbol=sym,
                                            qty=result.filled_qty,
                                            price=result.avg_price or ltp,
                                            mode="INTRADAY",
                                            strategy="DAWN",
                                            initial_stop_price=stop_price,
                                            atr=atr_val,
                                            strategy_config=dawn_sl_cfg,
                                            time_stop_ist=dawn_sl_cfg.get("time_stop_ist"),
                                            conviction_at_entry=conviction_score,
                                        )
                                        print(
                                            f"  🌅 DAWN LIVE: {qty}x {sym} @ {ltp:.2f} "
                                            f"SL={stop_price:.2f} conviction={conviction_score:.0f}"
                                        )
                                        logging.info(
                                            f"[DAWN LIVE] {sym} | qty={qty} | entry={ltp:.2f} | "
                                            f"sl={stop_price:.2f} | atr={atr_val:.2f} | "
                                            f"conviction={conviction_score:.0f}"
                                        )

                        except Exception as dryrun_e:
                            logging.error(f"DAWN entry failed: {dryrun_e}")
                        last_dawn_dryrun_entry_date = current_date

                    # ── DAWN: HYDRA dryrun update every 15 min ──
                    if (last_dawn_dryrun_update_time is None or
                            (now - last_dawn_dryrun_update_time).total_seconds() >= 900):
                        try:
                            update_dawn_dryrun()
                            last_dawn_dryrun_update_time = now
                        except Exception as dryrun_upd_e:
                            logging.error(f"DAWN dryrun update failed: {dryrun_upd_e}")

                    # ── DAWN: Manage virtual positions every 5 min ──
                    if dawn._signals and any(s.status == "ACTIVE" for s in dawn._signals):
                        if (last_dawn_manage_time is None or
                                (now - last_dawn_manage_time).total_seconds() >= 300):
                            try:
                                dawn.manage_positions(live_client=client, technical_body=TechnicalBody)
                                last_dawn_manage_time = now
                            except Exception as dawn_mgmt_e:
                                logging.error(f"DAWN position management failed: {dawn_mgmt_e}")

                    # ── DAWN: Time-based exit at 15:20 ──
                    if current_time >= dt_time(15, 20) and dawn._signals:
                        if any(s.status == "ACTIVE" for s in dawn._signals):
                            try:
                                dawn.close_all(reason="TIME_EXIT_15:20", live_client=client)
                                print(f"  🌅 DAWN: All positions closed at 15:20")
                            except Exception as dawn_close_e:
                                logging.error(f"DAWN time exit failed: {dawn_close_e}")

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
                                    result = executor.execute_buy(symbol=entry.symbol, ltp=ltp, qty=qty, market_regime=load_daily_regime(), conviction=conviction.total, route="HYDRA")
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
                                    result = executor.execute_short_sell(symbol=entry.symbol, ltp=ltp, qty=qty, conviction=conviction.total, route="HYDRA")
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
                                # 4-layer gate (L1 freshness, L3 quality, L4 confirmation).
                                # Returns the surviving subset, tagged with filing_freshness,
                                # event_quality_score, market_confirmation, conviction_modifier.
                                gated: list = []
                                if hot:
                                    from src.data_ingestion.event_scanner import (
                                        apply_event_evaluation_gate,
                                    )
                                    gated = apply_event_evaluation_gate(
                                        hot,
                                        seen_headlines=hydra.event_scanner._seen_headlines,
                                    )
                                    print(
                                        f"  🔥 HYDRA: {len(hot)} hot events → "
                                        f"{len(gated)} survived 4-layer gate"
                                    )
                                if gated:
                                    from src.strategies.base import WatchlistEntry
                                    for evt in gated:
                                        hydra.watchlist.append(WatchlistEntry(
                                            symbol=evt.symbol, direction=evt.direction,
                                            event_summary=evt.summary or evt.headline, urgency=evt.urgency,
                                            filing_subject=evt.headline,
                                            deal_size_inr=evt.deal_size_inr,
                                            market_cap_inr=evt.market_cap_inr,
                                            price_at_filing_time=evt.price_at_filing,
                                            filed_at=(evt.filed_at_dt.isoformat() if evt.filed_at_dt else None),
                                            filing_freshness=evt.filing_freshness,
                                            event_quality_score=evt.event_quality_score,
                                            conviction_modifier=evt.conviction_modifier,
                                        ))
                                        # Also add to conviction engine watchboard.
                                        # Stash filing metadata in .metadata so
                                        # apply_filing_metadata_adjustment() can read it.
                                        conviction_engine.add_signal(ActiveSignal(
                                            symbol=evt.symbol,
                                            direction=evt.direction,
                                            strategy="HYDRA",
                                            layer_c_score=min(100.0, evt.urgency * 10.0),
                                            event_summary=evt.summary or evt.headline,
                                            metadata={
                                                "urgency": evt.urgency,
                                                "filing_freshness": evt.filing_freshness,
                                                "event_quality_score": evt.event_quality_score,
                                                "conviction_modifier": evt.conviction_modifier,
                                                "market_confirmation": evt.market_confirmation,
                                            },
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
                                        result = executor.execute_buy(symbol=entry.symbol, ltp=ltp, qty=qty, market_regime=load_daily_regime(), conviction=conviction.total, route="VIPER")
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
                                        result = executor.execute_short_sell(symbol=entry.symbol, ltp=ltp, qty=qty, conviction=conviction.total, route="VIPER")
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
                        last_intraday_run = now

                        # ── V4: Dynamic Conviction Engine — phase + watchboard tick ──
                        try:
                            kite_raw = getattr(client, '_kite', None)
                            mkt_snap = fetch_market_snapshot(kite_raw, last_market_snapshot)
                            last_market_snapshot = mkt_snap

                            # Build tech snapshots for all watchboard symbols
                            import pandas as pd
                            ce_tech_snaps = {}
                            for sig in conviction_engine.get_active_signals():
                                try:
                                    bars = get_intraday_bars_for_symbol(sig.symbol, lookback_minutes=70)
                                    if bars and len(bars) >= 5:
                                        bars_df = pd.DataFrame([{
                                            'date': b.timestamp, 'open': b.open, 'high': b.high,
                                            'low': b.low, 'close': b.close, 'volume': b.volume
                                        } for b in bars])
                                        ce_tech_snaps[sig.symbol] = TechnicalBody.compute_or_stream(
                                            sig.symbol, bars_df, latest_bar=bars[-1]
                                        )
                                except Exception:
                                    pass

                            triggered = conviction_engine.tick(mkt_snap, ce_tech_snaps)
                            phase = conviction_engine.phase
                            print(f"  📈 Phase: {phase.value.upper()} | Nifty={mkt_snap.nifty_pct:+.1f}% | A/D={mkt_snap.ad_ratio:.2f} | VIX={mkt_snap.vix:.1f} | {conviction_engine.get_watchboard_summary()}")
                            logging.info(f"[Phase] {phase.value} | Nifty={mkt_snap.nifty_pct:+.1f}% | A/D={mkt_snap.ad_ratio:.2f}")

                            # Update window statuses using latest prices from tech snapshots
                            try:
                                _price_map = {
                                    sym: float(snap.close) if hasattr(snap, "close") else 0.0
                                    for sym, snap in ce_tech_snaps.items()
                                    if snap is not None
                                }
                                conviction_engine.update_window_statuses(_price_map)
                            except Exception as _ws_e:
                                logging.warning(f"[ConvEng] update_window_statuses failed: {_ws_e}")

                            # Execute triggered signals through existing risk stack
                            for sig in triggered:
                                sym = sig.symbol
                                if sym in daily_traded_symbols:
                                    continue
                                if risk_state.trades_taken >= risk_cfg.max_trades_per_day:
                                    break
                                allowed, _ = slot_manager.can_trade(sym, sig.direction)
                                if not allowed:
                                    continue
                                time_ok, _ = should_allow_new_entry(datetime.now(IST))
                                if not time_ok:
                                    continue

                                # Phase 8f: halt gate
                                if risk_state.is_halted:
                                    break

                                # Get fresh price data
                                tick = client.get_last_tick(sym)
                                ltp = tick.ltp if tick else 0
                                if ltp <= 0:
                                    continue

                                tech_snap = ce_tech_snaps.get(sym)
                                atr_val = tech_snap.atr14 if tech_snap and tech_snap.atr14 > 0 else ltp * 0.015

                                # Phase 8: load strategy config and apply conviction modifier
                                from src.config_loader import get_stoploss_config, apply_conviction_modifier
                                _ce_strat = (sig.strategy or "HYDRA").upper().split("_")[0]
                                _sl_cfg = get_stoploss_config(_ce_strat)
                                _raw_mult = float(_sl_cfg.get("initial_atr_multiplier", 1.5))
                                _adj_mult = apply_conviction_modifier(_raw_mult, float(sig.last_conviction or 0.0))
                                from src.config_loader import get_max_stop_pct
                                stop_dist = min(atr_val * _adj_mult, ltp * (get_max_stop_pct() / 100.0))
                                if stop_dist <= 0:
                                    stop_dist = ltp * 0.015

                                capital_pct = slot_manager.get_capital_allocation(sig.last_conviction, sym)
                                qty = compute_atr_position_size(
                                    risk_cfg.per_trade_capital_rupees * capital_pct, stop_dist
                                ) if stop_dist > 0 else 1

                                if sig.direction == "BUY":
                                    stop_price = round(ltp - stop_dist, 2)
                                    result = executor.execute_buy(symbol=sym, ltp=ltp, qty=qty, market_regime=load_daily_regime(), conviction=float(sig.last_conviction or 0.0), route=sig.strategy, sl=stop_price, atr=atr_val, strategy_config=_sl_cfg)
                                    if result.success:
                                        risk_state.trades_taken += 1
                                        daily_traded_symbols.add(sym)
                                        slot_manager.allocate(sig.strategy, sym, "BUY", sig.last_conviction)
                                        positions.on_buy_fill(
                                            symbol=sym, qty=result.filled_qty, price=result.avg_price or ltp,
                                            mode="INTRADAY", strategy=f"{sig.strategy}_CONV",
                                            initial_stop_price=stop_price, atr=atr_val,
                                            strategy_config=_sl_cfg,
                                            time_stop_ist=_sl_cfg.get("time_stop_ist"),
                                            conviction_at_entry=float(sig.last_conviction or 0.0),
                                        )
                                        # Phase 8h: shadow entry for dry-run SL validation
                                        if shadow_book is not None:
                                            try:
                                                shadow_book.on_entry(
                                                    symbol=sym, qty=result.filled_qty,
                                                    price=result.avg_price or ltp,
                                                    strategy=f"{sig.strategy}_CONV",
                                                    initial_stop_price=stop_price, atr=atr_val,
                                                    strategy_config=_sl_cfg,
                                                    time_stop_ist=_sl_cfg.get("time_stop_ist"),
                                                    conviction_at_entry=float(sig.last_conviction or 0.0),
                                                )
                                            except Exception as _sh_e:
                                                logging.warning(f"[SHADOW] entry failed: {_sh_e}")
                                        print(f"  ⚡ CONVICTION BUY: {qty}x {sym} @ {ltp:.2f} SL={stop_price:.2f} conviction={sig.last_conviction:.0f} [{sig.strategy}]")
                                        log_execution(exec_logger, symbol=sym, ltp=ltp, result=result,
                                                      meta={"strategy": f"{sig.strategy}_CONV", "conviction": sig.last_conviction, "stop": stop_price, "event": sig.event_summary})
                                elif sig.direction == "SHORT":
                                    stop_price = round(ltp + stop_dist, 2)
                                    result = executor.execute_short_sell(symbol=sym, ltp=ltp, qty=qty, conviction=float(sig.last_conviction or 0.0), route=sig.strategy, sl=stop_price, atr=atr_val, strategy_config=_sl_cfg)
                                    if result.success:
                                        risk_state.trades_taken += 1
                                        daily_traded_symbols.add(sym)
                                        slot_manager.allocate(sig.strategy, sym, "SHORT", sig.last_conviction)
                                        positions.on_short_fill(
                                            symbol=sym, qty=result.filled_qty, price=result.avg_price or ltp,
                                            mode="INTRADAY", strategy=f"{sig.strategy}_CONV",
                                            initial_stop_price=stop_price, atr=atr_val,
                                            strategy_config=_sl_cfg,
                                            time_stop_ist=_sl_cfg.get("time_stop_ist"),
                                            conviction_at_entry=float(sig.last_conviction or 0.0),
                                        )
                                        print(f"  ⚡ CONVICTION SHORT: {qty}x {sym} @ {ltp:.2f} SL={stop_price:.2f} conviction={sig.last_conviction:.0f} [{sig.strategy}]")
                                        log_execution(exec_logger, symbol=sym, ltp=ltp, result=result,
                                                      meta={"strategy": f"{sig.strategy}_CONV", "conviction": sig.last_conviction, "stop": stop_price, "event": sig.event_summary})
                        except Exception as ce_e:
                            logging.error(f"Conviction engine tick failed: {ce_e}")
                            import traceback; traceback.print_exc()

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
                                    # V4.1: Catalyst-driven signals get reduced CAUTION dampener
                                    if macro_ctx:
                                        dampener_pts, min_conviction = macro_ctx.get_direction_dampener(ds.direction)

                                        # Catalyst-aware override for CAUTION tier LONG only
                                        catalyst_driven = (
                                            "newsdata_eod" in ds.sources
                                            or "event_scanner" in ds.sources
                                            or bool(ds.catalyst_headline)
                                        )
                                        tier = macro_ctx.get_risk_tier()
                                        if catalyst_driven and tier == MacroRiskTier.CAUTION and ds.direction == "LONG":
                                            dampener_pts = -5
                                            min_conviction = 62
                                            print(f"    [V2] {sym} — catalyst signal, reduced CAUTION dampener (-5pts instead of -10pts)")
                                            logging.info(f"[V2] {sym} catalyst override: CAUTION dampener -5pts (min 62) sources={ds.sources}")

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

                                        # Phase 8h: shadow exit record
                                        if shadow_book is not None:
                                            try:
                                                shadow_book.on_exit(
                                                    symbol=es.symbol, qty=exit_qty,
                                                    exit_price=price,
                                                    exit_reason=es.reason,
                                                    exit_ts=datetime.now(IST),
                                                )
                                            except Exception as _sh_e:
                                                logging.warning(f"[SHADOW] exit failed: {_sh_e}")

                                meta = {"reason": es.reason, "mode": es.mode,
                                        "strategy": es.strategy, "stop_price": es.stop_price,
                                        "direction": direction}
                                log_execution(exec_logger, symbol=es.symbol, ltp=es.ltp, result=result, meta=meta)
                        except Exception as e:
                            logging.error(f"Failed to process exit signals: {e}")
                            print(f"\nExit processing error: {e}")

                        last_intraday_run = now
                
                # ── Phase 7: 15:15 IST — live EOD square-off (live-mode only) ──
                try:
                    from src.config_loader import get_dry_run as _cfg_dry_run, get_eod_squareoff_time
                    if not _cfg_dry_run() and _market_open_today:
                        _sq_str = get_eod_squareoff_time()
                        _sq_h, _sq_m = [int(x) for x in _sq_str.split(":")]
                        _sq_time = dt_time(_sq_h, _sq_m)
                        if (_should_fire_scheduled_job(_sq_time, runner_start_time, current_time)
                                and last_squareoff_date != current_date):
                            from src.trading.live_trade_tracker import (
                                get_open_positions as _lv_open,
                                get_open_trades_today as _lv_open_trades,
                                update_trade_status as _lv_update,
                            )
                            _open_count = _lv_open()
                            if _open_count > 0:
                                msg = f"[EOD] {_open_count} open positions detected at 15:15 — initiating square-off check"
                                print(f"  ⏰ {msg}")
                                logging.info(msg)
                                for _otr in _lv_open_trades():
                                    _sym = _otr.get("symbol", "")
                                    _oqty = int(_otr.get("quantity", 0) or 0)
                                    _tick = client.get_last_tick(_sym)
                                    _ltp = _tick.ltp if _tick else float(_otr.get("entry_price", 0) or 0)
                                    try:
                                        _exit = executor.execute_sell(
                                            symbol=_sym, qty=_oqty, ltp=_ltp,
                                            route="EOD_SQUAREOFF",
                                        )
                                        if _exit.success:
                                            _eid = _exit.broker_order_id or ""
                                            _lv_update(order_id=_otr.get("order_id", ""),
                                                       exit_price=_ltp,
                                                       exit_at=datetime.now(IST).isoformat())
                                            # Phase 8g: close in PositionBook so ExitEngine
                                            # 15:20 TIME_EXIT doesn't fire a duplicate order
                                            try:
                                                positions.on_sell_fill(_sym, _oqty, _ltp)
                                            except Exception as _pb_e:
                                                logging.warning(
                                                    f"[EOD] PositionBook close failed for {_sym}: {_pb_e}"
                                                )
                                            logging.info(
                                                f"[EOD SQUARE-OFF] {_sym} | qty={_oqty} | order_id={_eid}"
                                            )
                                            print(f"  ✅ [EOD SQUARE-OFF] {_sym} | qty={_oqty} | order_id={_eid}")
                                        else:
                                            _err = f"[EOD SQUARE-OFF FAILED] {_sym} — MANUAL ACTION REQUIRED ({_exit.message})"
                                            logging.error(_err)
                                            print(f"  ❌ {_err}")
                                            try:
                                                from src.reports.email_sender import send_report_email
                                                send_report_email(
                                                    subject=f"VoltEdgeAI URGENT: EOD Square-Off FAILED for {_sym}",
                                                    body_md=f"# URGENT\n\nEOD square-off failed for {_sym} (qty={_oqty}).\n\nReason: {_exit.message}\n\n**Manual action required on Kite now.**",
                                                )
                                            except Exception as _eem:
                                                logging.error(f"[EOD] failure-alert email crashed: {_eem}")
                                    except Exception as _se:
                                        _err = f"[EOD SQUARE-OFF FAILED] {_sym} — MANUAL ACTION REQUIRED ({_se})"
                                        logging.error(_err)
                                        print(f"  ❌ {_err}")
                            else:
                                logging.info("[EOD] 15:15 check — no open live positions")
                            last_squareoff_date = current_date
                except Exception as _sq_e:
                    logging.error(f"[EOD] Phase 7 square-off block failed: {_sq_e}")

                # 2. End of Day tasks after 15:30
                if _market_open_today and current_time > MARKET_END:
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

                        # ── DAWN: EOD report + metrics ──
                        try:
                            dawn_csv = dawn.save_daily_report()
                            dawn_metrics = dawn.compute_metrics()
                            if dawn_csv:
                                print(f"  🌅 DAWN: Report saved to {dawn_csv}")
                                print(f"  🌅 DAWN: {dawn_metrics.total_signals} signals, "
                                      f"W/L={dawn_metrics.win_count}/{dawn_metrics.loss_count}, "
                                      f"PnL={dawn_metrics.total_pnl_rupees:+.0f}")
                        except Exception as dawn_eod_e:
                            logging.error(f"DAWN EOD cleanup failed: {dawn_eod_e}")

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
                        
                # 3. 16:00 — Unified Post-Market Report (v2)
                if _market_open_today and _should_fire_scheduled_job(dt_time(16, 0), runner_start_time, current_time):
                    if last_report_date != current_date:
                        try:
                            from src.reports.post_market_report import generate_post_market_report

                            kite_live = getattr(client, '_kite', None)
                            if kite_live is None:
                                try:
                                    from kiteconnect import KiteConnect
                                    kite_live = KiteConnect(api_key=os.getenv("ZERODHA_API_KEY"))
                                    kite_live.set_access_token(os.getenv("ZERODHA_ACCESS_TOKEN"))
                                except Exception:
                                    kite_live = None

                            # Record pattern outcomes BEFORE generating report
                            # (so report can reference Layer E updates)
                            try:
                                trade_records = []
                                from src.db import SessionLocal, TradeRecord, init_db
                                init_db()
                                with SessionLocal() as sess:
                                    trades = sess.query(TradeRecord).filter(
                                        TradeRecord.exit_time >= datetime.combine(current_date, datetime.min.time()),
                                        TradeRecord.exit_time <= datetime.combine(current_date, datetime.max.time()),
                                    ).all()
                                    for t in trades:
                                        trade_records.append({
                                            "symbol": t.symbol,
                                            "direction": t.direction,
                                            "pnl": t.pnl or 0,
                                            "entry_price": t.entry_price or 0,
                                            "qty": t.qty or 0,
                                        })
                                # Build EOD price map for all watchboard symbols
                                eod_price_map = {}
                                if kite_live:
                                    wb_symbols = [
                                        s.symbol for s in conviction_engine._watchboard.values()
                                    ]
                                    if wb_symbols:
                                        try:
                                            nse_keys = [f"NSE:{sym}" for sym in wb_symbols]
                                            # Kite LTP supports up to 500 symbols per call
                                            for i in range(0, len(nse_keys), 500):
                                                batch = nse_keys[i:i+500]
                                                ltp_data = kite_live.ltp(batch)
                                                for nse_key, val in ltp_data.items():
                                                    sym = nse_key.replace("NSE:", "")
                                                    eod_price_map[sym] = val.get("last_price", 0)
                                            logging.info(
                                                f"[EOD] Built price_map for {len(eod_price_map)}/{len(wb_symbols)} watchboard symbols"
                                            )
                                        except Exception as ltp_e:
                                            logging.warning(f"[EOD] LTP fetch for price_map failed: {ltp_e}")

                                conviction_engine.record_eod_outcomes(trade_records, price_map=eod_price_map)
                            except Exception as eod_e:
                                logging.error(f"Pattern DB EOD recording failed: {eod_e}")

                            viper_health_str = getattr(viper, 'scan_health_summary', '')
                            generate_post_market_report(
                                kite_client=kite_live,
                                traded_symbols=set(daily_traded_symbols),
                                conviction_engine=conviction_engine,
                                viper_health=viper_health_str,
                                pre_market_ran=pre_market_ran,
                                grok_call_count=grok_call_count,
                            )

                            # Phase 8h: append Section 14 (SL shadow) to post-market log
                            try:
                                from src.trading.shadow_book import generate_shadow_report_section
                                _sec14 = generate_shadow_report_section(current_date)
                                if _sec14:
                                    logging.info(f"\n{_sec14}")
                                    print(f"\n{_sec14}")
                            except Exception as _s14_e:
                                logging.warning(f"[SHADOW] Section 14 failed: {_s14_e}")

                        except Exception as chron_e:
                            logging.error(f"Post-Market Report failed: {chron_e}")
                            print(f"  ❌ Post-Market Report error: {chron_e}")

                        # ── Phase 5: Counterfactual analysis (router quality feedback) ──
                        try:
                            from src.config_loader import get_cf_auto_run
                            if get_cf_auto_run():
                                from src.analysis.counterfactual import (
                                    run_counterfactual, persist_counterfactual, load_shadows,
                                )
                                _cf_date = current_date.isoformat()
                                if load_shadows(_cf_date):
                                    cf_result = run_counterfactual(_cf_date)
                                    persist_counterfactual(cf_result)
                                    logging.info(
                                        f"[COUNTERFACTUAL] {cf_result['total_shadows']} shadows analyzed | "
                                        f"router_accuracy={cf_result['router_accuracy']:.1%} | "
                                        f"missed={cf_result['missed_opportunities']}"
                                    )
                                    print(
                                        f"  📐 Counterfactual: {cf_result['total_shadows']} shadows | "
                                        f"accuracy={cf_result['router_accuracy']:.1%} | "
                                        f"missed={cf_result['missed_opportunities']}"
                                    )
                                else:
                                    logging.info(f"[COUNTERFACTUAL] No shadows for {_cf_date} — skipping")
                        except Exception as cf_e:
                            logging.error(f"Counterfactual analysis failed: {cf_e}")
                            print(f"  ❌ Counterfactual failed: {cf_e}")

                        # ── Phase 6: Weekly backtest auto-run (Fridays only) ──
                        try:
                            from src.config_loader import (
                                get_backtest_auto_run_weekly, get_backtest_days,
                            )
                            if get_backtest_auto_run_weekly() and weekday == 4:
                                from src.backtest.engine import run_backtest
                                _bt_days = get_backtest_days()
                                _bt_result = run_backtest(days=_bt_days)
                                logging.info(
                                    f"[BACKTEST] Weekly backtest complete — "
                                    f"see data/backtest_{current_date}_{_bt_days}d.json"
                                )
                                print(
                                    f"  📈 Backtest ({_bt_days}d): "
                                    f"{_bt_result['trades_taken']} trades | "
                                    f"WR={_bt_result['win_rate']:.1%} | "
                                    f"P&L={_bt_result['total_pnl_pct']:+.2f}%"
                                )
                        except Exception as bt_e:
                            logging.error(f"Weekly backtest failed: {bt_e}")
                            print(f"  ❌ Weekly backtest failed: {bt_e}")

                        last_report_date = current_date
                        last_autopsy_date = current_date

                # 4. 16:30 — Prediction Feedback Loop (inline, after post-market)
                if _market_open_today and _should_fire_scheduled_job(dt_time(16, 30), runner_start_time, current_time):
                    if last_feedback_date != current_date:
                        try:
                            from src.reports.feedback_loop import run_feedback_loop
                            run_feedback_loop()
                            print(f"  📊 Feedback loop completed")
                        except Exception as fb_e:
                            logging.error(f"Feedback loop failed: {fb_e}")
                            print(f"  ❌ Feedback loop failed: {fb_e}")
                        last_feedback_date = current_date

                # 5. 09:00 IST — Morning brief (trading day) OR holiday digest
                if _should_fire_scheduled_job(dt_time(9, 0), runner_start_time, current_time):
                    if last_premarket_date != current_date:
                        if _market_open_today:
                            try:
                                from src.reports.pre_market_brief import generate_pre_market_brief
                                generate_pre_market_brief()
                                pre_market_ran = True
                                print(f"  📰 Pre-market brief generated successfully")
                            except Exception as brief_e:
                                logging.error(f"Pre-market brief failed: {brief_e}")
                                print(f"  ❌ Pre-market brief failed: {brief_e}")
                                pre_market_ran = False
                        else:
                            # ── Holiday Guard: send digest instead of morning brief ──
                            holiday_label = _holiday_name or "Weekend"
                            logging.info(
                                f"[Holiday Guard] Sending holiday digest instead of morning brief — {holiday_label}"
                            )
                            try:
                                from src.reports.email_sender import send_report_email
                                send_report_email(
                                    subject=f"VoltEdge | Market Holiday — {holiday_label} | {current_date}",
                                    body_md=(
                                        f"Market is closed today ({holiday_label}). "
                                        f"No trading activity. "
                                        f"Global markets and macro monitoring continues."
                                    ),
                                )
                                print(f"  📧 [Holiday Guard] Holiday digest sent — {holiday_label}")
                            except Exception as holiday_email_e:
                                logging.error(
                                    f"[Holiday Guard] Holiday digest email failed: {holiday_email_e}"
                                )
                        last_premarket_date = current_date

                # 5b. 09:05 IST — Retry if pre-market brief failed (trading day only)
                if _market_open_today and _should_fire_scheduled_job(dt_time(9, 5), runner_start_time, current_time):
                    if not pre_market_ran and last_premarket_date == current_date:
                        try:
                            from src.reports.pre_market_brief import generate_pre_market_brief
                            # Remove duplicate guard file so retry can proceed
                            import os as _os
                            _retry_path = _os.path.join("logs", "daily_reports", f"{current_date}_morning_brief.md")
                            if _os.path.exists(_retry_path):
                                _os.remove(_retry_path)
                            generate_pre_market_brief()
                            pre_market_ran = True
                            print(f"  📰 Pre-market brief RETRY succeeded")
                        except Exception as retry_e:
                            logging.error(f"Pre-market brief RETRY also failed: {retry_e}")
                            print(f"  ❌ Pre-market brief RETRY failed: {retry_e}")

                # ── DAWN: Merge brief + own scan, generate signals + email at 08:52 ──
                if _market_open_today and _should_fire_scheduled_job(dt_time(8, 52), runner_start_time, current_time):
                    if last_dawn_scan_date != current_date:
                        try:
                            dawn_signals = dawn.merge_brief_and_generate(str(current_date))
                            if dawn_signals:
                                print(f"  🌅 DAWN: {len(dawn_signals)} qualified signals, email sent")
                                dawn_syms = [s.symbol for s in dawn_signals]
                                try:
                                    client.subscribe_symbols(dawn_syms, mode="full")
                                    print(f"  📡 Subscribed {len(dawn_syms)} DAWN symbols to websocket")
                                except Exception as ws_e:
                                    logging.warning(f"DAWN websocket subscribe failed: {ws_e}")
                            else:
                                print(f"  🌅 DAWN: No qualified signals today")
                        except Exception as dawn_merge_e:
                            logging.error(f"DAWN merge failed: {dawn_merge_e}")
                            print(f"  ❌ DAWN merge error: {dawn_merge_e}")
                        last_dawn_scan_date = current_date

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

    # Phase 4: config.yaml is the source of truth. Env LIVE_MODE=1 still wins
    # (forces live mode even if config.dry_run=true). All other params from config.
    from src.config_loader import get_dry_run, get_max_trades, get_per_trade_risk
    _env_live = os.getenv("VOLTEDGE_LIVE_MODE", "0") == "1" or os.getenv("LIVE_MODE", "").lower() == "true"
    _cfg_live = not get_dry_run()
    run_loop(
        live_mode=_env_live or _cfg_live,
        per_trade_capital=int(get_per_trade_risk()),
        max_trades_per_day=int(get_max_trades()),
    )
