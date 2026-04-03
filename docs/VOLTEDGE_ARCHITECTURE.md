# VoltEdgeAI — Complete Architecture Documentation

> Last updated: 2026-04-03 | Based on direct source-code analysis of all modules.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Infrastructure and Deployment](#2-infrastructure-and-deployment)
3. [System Startup and Runner Loop](#3-system-startup-and-runner-loop)
4. [Data Ingestion Layer](#4-data-ingestion-layer)
5. [Strategy Layer](#5-strategy-layer)
6. [Conviction Engine (The Core)](#6-conviction-engine-the-core)
7. [Market Phase Classification](#7-market-phase-classification)
8. [Juror and Signal Scoring](#8-juror-and-signal-scoring)
9. [Risk Management](#9-risk-management)
10. [Broker Integration](#10-broker-integration)
11. [Exit Engine](#11-exit-engine)
12. [LLM Integration](#12-llm-integration)
13. [Reporting System](#13-reporting-system)
14. [Pattern Database and Layer E Learning](#14-pattern-database-and-layer-e-learning)
15. [Database](#15-database)
16. [Known Bugs and Status](#16-known-bugs-and-status)
17. [Deployment and Operations Runbook](#17-deployment-and-operations-runbook)
18. [Going Live Checklist](#18-going-live-checklist)
19. [Future Roadmap](#19-future-roadmap)

---

## 1. Project Overview

VoltEdgeAI is a production-grade AI-driven algorithmic trading engine targeting Indian equity markets (NSE/BSE). It runs 24/7 as a systemd service on a GCP VM, combining event-driven catalyst detection, momentum scanning, multi-layer AI conviction scoring, and automated order execution through Zerodha's Kite Connect broker API.

### What It Does

- Monitors NSE corporate events, top movers, and global macro data every 15 minutes during market hours (09:15-15:30 IST)
- Classifies events and movers using Groq LLaMA (ultra-fast, ~300ms) and Grok 4 (portfolio-level reasoning)
- Builds a 5-layer weighted conviction score for every candidate signal
- Automatically executes LONG or SHORT intraday equity trades when conviction >= 70
- Sends three daily email reports (morning brief, mid-session pulse, post-market debrief)
- Learns from past trades via a pattern database (Layer E) that feeds back into scoring

### Current Status (as of 2026-04-03)

| Parameter | Value | Source |
|-----------|-------|--------|
| Mode | DRY_RUN (LIVE_MODE=0) | `.env` |
| Per-trade capital | ₹10,000 | `.env` VOLTEDGE_PER_TRADE_CAPITAL |
| Max trades/day | 5 | `.env` VOLTEDGE_MAX_TRADES_PER_DAY |
| Daily loss cap | ₹2,500 (default) | `src/config/risk.py` |
| Conviction threshold | 70/100 | `src/strategies/slot_manager.py` |
| Kite token status | EXPIRED (last report) | `logs/daily_reports/2026-04-03_post_market.md` |
| Daily regime | sideways, strength=0.0 | `data/daily_regime.json` |

### Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.12 |
| Broker API | Kite Connect (kiteconnect==5.0.1) |
| LLM — Event Classification | Groq Llama-3.3-70b-versatile (~300ms, 14,400 req/day free) |
| LLM — Portfolio Orchestration | xAI Grok 4 via OpenAI-compatible API (max 10 calls/day) |
| LLM — Reports and Prediction | Google Gemini 2.0 via google-genai |
| Database | SQLite via SQLAlchemy 2.0 |
| Technical Analysis | `ta` library (pandas-based indicators) |
| Market Data | NSE via nsepython, jugaad-data, Kite WebSocket |
| Global Macro | Finnhub API (forex, commodities, US equities) |
| News | NewsData.io (`NEWDATA_API_KEY`) |
| Infrastructure | GCP VM, systemd, Python venv |
| Process Manager | systemd (`voltedge.service`) |

### System ASCII Diagram

```
                      ┌─────────────────────────────┐
                      │    GCP Virtual Machine       │
                      │    (voltedge.service)         │
                      └──────────────┬───────────────┘
                                     │
                          ┌──────────▼──────────┐
                          │      main.py         │
                          │  (bootstrap + env)   │
                          └──────────┬───────────┘
                                     │
                          ┌──────────▼──────────┐
               ┌──────────│     runner.py        │──────────┐
               │          │  (while True loop)   │          │
               │          └──────┬───────┬───────┘          │
               │                 │       │                   │
    ┌──────────▼──┐   ┌─────────▼──┐  ┌─▼──────────────┐  ┌▼──────────────┐
    │ Data Layer   │   │ Strategies │  │ Conviction Eng │  │  Reports       │
    │              │   │            │  │                │  │                │
    │ Finnhub      │   │ HYDRA      │  │ Layer A-E      │  │ Pre-Market     │
    │ NewsData.io  │   │ (events)   │  │ MarketPhase    │  │ Mid-Session    │
    │ NSE Scraper  │   │ VIPER      │  │ PatternDB      │  │ Post-Market    │
    │ PCR Tracker  │   │ (movers)   │  │ Watchboard     │  │ Feedback Loop  │
    │ Short BanList│   │ SlotMgr    │  │                │  │                │
    └──────────────┘   └────────────┘  └────────────────┘  └────────────────┘
               │                 │       │                   │
               └────────┬────────┘       │                   │
                        │                │                   │
              ┌──────────▼──────────┐    │          ┌────────▼───────┐
              │   LLM Integrations  │    │          │   Email Sender  │
              │                     │    │          │  SMTP Gmail     │
              │  Groq LLaMA-3.3-70B │    │          │  → Operator    │
              │  Grok 4 (xAI)       │    │          └────────────────┘
              │  Gemini 2.0         │    │
              └─────────────────────┘    │
                                         │
                        ┌────────────────▼──────┐
                        │     Risk Stack         │
                        │                        │
                        │  DailyRiskState        │
                        │  SlotManager           │
                        │  ATR Sizing            │
                        │  SectorGuard           │
                        │  CircuitLimits         │
                        │  TimeOfDay             │
                        │  TradingCosts          │
                        │  DepthAnalyzer         │
                        └──────────┬─────────────┘
                                   │
                     ┌─────────────▼──────────────┐
                     │      TradeExecutor          │
                     │                            │
                     │  DRY_RUN: logs only        │
                     │  LIVE: Zerodha Kite API    │
                     └─────────────┬──────────────┘
                                   │
                     ┌─────────────▼──────────────┐
                     │     SQLite Database         │
                     │  (voltedgeai.db)            │
                     │                            │
                     │  JurorSignal               │
                     │  TradeRecord               │
                     │  DailyPerformanceSnapshot  │
                     │  DecisionRecord            │
                     │  FundamentalUniverse       │
                     └────────────────────────────┘
```

---

## 2. Infrastructure and Deployment

### GCP VM Setup

The service runs on a Google Cloud Platform VM as user `mujtabasiddiqui`. The working directory is `/home/mujtabasiddiqui/VoltEdgeAI`. The Python virtual environment lives at `.venv/`.

### systemd Service Configuration

**File:** `/etc/systemd/system/voltedge.service`

```ini
[Unit]
Description=VoltEdge AI Trading Engine
After=network.target

[Service]
Type=simple
User=mujtabasiddiqui
WorkingDirectory=/home/mujtabasiddiqui/VoltEdgeAI
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/mujtabasiddiqui/VoltEdgeAI/.env
Environment="PATH=/home/mujtabasiddiqui/VoltEdgeAI/.venv/bin:/usr/bin"
ExecStart=/home/mujtabasiddiqui/VoltEdgeAI/.venv/bin/python /home/mujtabasiddiqui/VoltEdgeAI/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Key points:
- `Restart=always` with `RestartSec=5`: the process auto-restarts within 5 seconds of any crash — the runner is designed to never crash, but systemd provides the final safety net.
- `EnvironmentFile`: all secrets are loaded from `.env` directly into the service environment. The Python code uses `load_dotenv()` in `main.py` for local dev; in production, systemd provides the vars.
- `PYTHONUNBUFFERED=1`: ensures `print()` output appears in `journalctl` immediately without buffering.
- `After=network.target`: the service only starts after the network is ready (required for Kite WebSocket and API calls).

### Environment Variables

All secrets and config live in `/home/mujtabasiddiqui/VoltEdgeAI/.env`:

| Variable | Purpose | Used In |
|----------|---------|---------|
| `ZERODHA_API_KEY` | Kite Connect API key | `src/brokers/zerodha_client.py`, `src/data_ingestion/market_live.py` |
| `ZERODHA_API_SECRET` | Kite Connect secret (for token generation) | `src/tools/auto_login.py` |
| `ZERODHA_ACCESS_TOKEN` | Daily session token (expires nightly) | All Kite API calls |
| `ZERODHA_USER_ID` | Zerodha client ID (e.g. `SBT124`) | `src/tools/auto_login.py` |
| `ZERODHA_PASSWORD` | Zerodha login password | `src/tools/auto_login.py` |
| `ZERODHA_TOTP_SECRET` | TOTP 2FA seed for automated login | `src/tools/auto_login.py` |
| `REPORT_EMAIL_ENABLED` | `1` to enable email reports | `src/reports/email_sender.py` |
| `REPORT_EMAIL_TO` | Recipient address | `src/reports/email_sender.py` |
| `REPORT_SMTP_HOST` | SMTP host (default: `smtp.gmail.com`) | `src/reports/email_sender.py` |
| `REPORT_SMTP_PORT` | SMTP port (default: 587) | `src/reports/email_sender.py` |
| `REPORT_SMTP_USER` | Gmail sender address | `src/reports/email_sender.py` |
| `REPORT_SMTP_PASSWORD` | Gmail App Password | `src/reports/email_sender.py` |
| `GEMINI_API_KEY` | Google Gemini 2.0 key | `src/reports/pre_market_brief.py`, `src/juror/gemini_client.py` |
| `GROK_API_KEY` | xAI Grok 4 key (used as `GROK_API_KEY` or `XAI_API_KEY`) | `src/llm/grok_client.py` |
| `GROQ_API_KEY` | Groq LLaMA key | `src/llm/groq_client.py` |
| `FINNHUB_API_KEY` | Finnhub forex/equity data | `src/data_ingestion/finnhub_client.py` |
| `NEWDATA_API_KEY` | NewsData.io news API | `src/data_ingestion/news_context.py` |
| `VOLTEDGE_PER_TRADE_CAPITAL` | Capital per trade in INR (default: 5000) | `src/config/risk.py`, `main.py` |
| `VOLTEDGE_MAX_TRADES_PER_DAY` | Max daily trade count (default: 5) | `src/config/risk.py`, `main.py` |
| `VOLTEDGE_LIVE_MODE` | `1` = live orders, `0` = dry-run | `main.py`, `src/trading/executor.py` |
| `VOLTEDGE_MAX_DAILY_LOSS` | Daily loss cap in INR (default: 2500) | `src/config/risk.py` |
| `VOLTEDGE_MAX_OPEN_POSITIONS` | Max simultaneous positions (default: 5) | `src/config/risk.py` |
| `VOLTEDGE_INTRADAY_STOP_PCT` | Hard stop loss % (default: 0.025 = 2.5%) | `src/config/risk.py` |
| `VOLTEDGE_INTRADAY_EXIT_TIME` | Force-exit time (default: `15:20`) | `src/config/risk.py` |
| `VOLTEDGE_MIN_SHARES_PER_TRADE` | Min shares per order (default: 1) | `src/config/risk.py` |
| `VOLTEDGE_MAX_SHARES_PER_TRADE` | Max shares per order (default: 200) | `src/config/risk.py` |
| `VOLTEDGE_WEAK_MARKET_SIZE_FACTOR` | Position size multiplier in weak market (default: 0.5) | `src/config/risk.py` |
| `VOLTEDGE_STRONG_MARKET_SIZE_FACTOR` | Position size multiplier in strong market (default: 1.0) | `src/config/risk.py` |
| `VOLTEDGE_MIN_AVG_DAILY_TURNOVER_RUPEES` | Minimum liquidity gate (default: 2,000,000) | `src/config/risk.py` |
| `VOLTEDGE_MIN_PRICE_RUPEES` | Minimum stock price (default: 50) | `src/config/risk.py` |

### Directory Structure

```
VoltEdgeAI/
├── main.py                         # Entrypoint: loads .env, calls run_loop()
├── requirements.txt                # All Python dependencies pinned
├── CLAUDE.md                       # Engineering standing orders for Claude Code
├── .env                            # Secrets (never commit)
├── voltedgeai.db                   # SQLite database
├── data/
│   ├── daily_regime.json           # Pre-market macro regime (trend/strength)
│   ├── pattern_db.json             # Layer E pattern outcomes (learning loop)
│   ├── fii_history.json            # Rolling 30-day FII flow history
│   ├── prediction_log.json         # Morning brief predictions + scores
│   ├── fo_ban_cache.json           # F&O ban list cache
│   ├── history.db                  # OHLCV historical cache (SQLite)
│   └── zerodha_instruments.csv     # NSE instrument token map
├── logs/
│   ├── daily_reports/              # All email reports saved as .md files
│   │   ├── YYYY-MM-DD_morning_brief.md
│   │   ├── YYYY-MM-DD_mid_session.md
│   │   ├── YYYY-MM-DD_post_market.md
│   │   └── YYYY-MM-DD_autopsy.md
│   └── viper_coil/                 # VIPER COIL dry-run logs
│       └── YYYY-MM-DD_coil_report.json
├── src/
│   ├── runner.py                   # Main event loop (1200+ lines)
│   ├── daily_decision_engine.py    # Juror → Sniper decision pipeline
│   ├── config/
│   │   ├── risk.py                 # RiskConfig dataclass + load_risk_config()
│   │   └── zerodha.py              # Zerodha credentials helper
│   ├── data_ingestion/
│   │   ├── pre_market_intelligence.py  # Multi-signal composite score
│   │   ├── macro_context.py            # MacroContext + MacroRiskTier
│   │   ├── finnhub_client.py           # Forex/commodity/US equity quotes
│   │   ├── short_ban_list.py           # F&O ban + T2T restrictions
│   │   ├── market_live.py              # Kite WebSocket + BarBuilder
│   │   ├── market_history.py           # SQLite OHLCV cache (HistoryStore)
│   │   ├── market_sentiment.py         # Index sentiment
│   │   ├── intraday_context.py         # Intraday bar fetching
│   │   ├── news_context.py             # NewsData.io client
│   │   ├── nse_scraper.py              # NSE FII/DII, bulk/block deals
│   │   ├── pcr_tracker.py              # Put-Call Ratio from NSE
│   │   ├── event_scanner.py            # Corporate events (NSE announcements)
│   │   ├── instruments.py              # Zerodha instruments CSV loader
│   │   └── corporate_actions.py        # Dividends, splits
│   ├── strategies/
│   │   ├── base.py                     # StrategyHead ABC, ConvictionScore, WatchlistEntry
│   │   ├── hydra.py                    # HYDRA: event-driven catalyst strategy
│   │   ├── viper.py                    # VIPER: momentum mover strategy
│   │   ├── slot_manager.py             # SlotManager: global trade budget
│   │   ├── technical_body.py           # TechnicalBody: shared TA computation
│   │   ├── move_classifier.py          # VIPER move type classification
│   │   └── viper_rules.py              # VIPER TA confirmation rules
│   ├── trading/
│   │   ├── conviction_engine.py        # 5-layer conviction system
│   │   ├── market_phase.py             # 6-phase market classifier
│   │   ├── pattern_db.py               # Layer E historical pattern DB
│   │   ├── executor.py                 # TradeExecutor (buy/sell/short)
│   │   ├── exit_engine.py              # ExitEngine with adaptive trailing stops
│   │   ├── exit_monitor.py             # ExitMonitorThread (1s polling)
│   │   ├── daily_risk_state.py         # DailyRiskState (thread-safe Decimal P&L)
│   │   ├── orders.py                   # OrderRequest/OrderResult dataclasses
│   │   ├── positions.py                # PositionBook
│   │   ├── sizing.py                   # Position sizing + MarketRegime
│   │   ├── atr.py                      # Wilder's ATR computation
│   │   ├── trading_costs.py            # Zerodha fee structure (Decimal-precise)
│   │   ├── circuit_limits.py           # NSE circuit band awareness
│   │   ├── depth_analyzer.py           # Level-2 order book analysis
│   │   ├── sector_guard.py             # Sector concentration limit (max 2)
│   │   ├── time_of_day.py              # Market phase time gates
│   │   └── position_monitor.py         # Position MTM monitoring
│   ├── llm/
│   │   ├── grok_client.py              # Grok 4 portfolio orchestrator
│   │   └── groq_client.py              # Groq LLaMA event classifier
│   ├── reports/
│   │   ├── pre_market_brief.py         # 08:30 IST morning brief (Gemini)
│   │   ├── mid_session_pulse.py        # 12:00 IST midday pulse (no LLM)
│   │   ├── post_market_report.py       # 15:40 IST post-market debrief
│   │   ├── feedback_loop.py            # EOD prediction scorer
│   │   ├── email_sender.py             # Unified SMTP sender
│   │   └── coil_reporter.py            # VIPER COIL report
│   ├── juror/
│   │   ├── catalyst_analyzer.py        # CatalystAnalyzer (Gemini scoring)
│   │   └── gemini_client.py            # Gemini client for Juror
│   ├── sniper/
│   │   ├── antigravity_watcher.py      # VWAP-bounce state machine
│   │   ├── antigravity.py              # Antigravity signal computation
│   │   ├── core.py                     # evaluate_signal() entry point
│   │   ├── logger.py                   # Sniper decision logger
│   │   ├── momentum_scanner.py         # Top movers via Kite batch quote
│   │   ├── stock_discovery.py          # StockDiscovery (multi-source funnel)
│   │   └── technical_scorer.py         # TechnicalScorer + meets_entry_threshold()
│   ├── brokers/
│   │   └── zerodha_client.py           # ZerodhaClient wrapping KiteConnect
│   ├── db/
│   │   ├── __init__.py                 # All SQLAlchemy models + init_db()
│   │   └── db_writer.py                # Async DatabaseWriter singleton
│   ├── tools/
│   │   ├── auto_login.py               # auto_refresh_access_token() via TOTP
│   │   └── kite_login_helper.py        # Manual login helper
│   ├── sources/
│   │   ├── nse_announcements.py        # NSE announcement scraper
│   │   └── nse_prices.py               # NSE price fetcher
│   └── marketdata/
│       └── intraday.py                 # compute_vwap_stats()
└── docs/
    ├── VOLTEDGE_ARCHITECTURE.md        # This file
    └── QUICK_REFERENCE.md              # Ops quick reference
```

### Git Workflow

- Branch: `main` (single branch; all commits go directly to main)
- Commit format: `[module] short description`
- Never commit: `.env`, `__pycache__`, `*.pyc`, `data/*.json`, `logs/`
- Pattern: read → minimal change → journalctl check → commit

---

## 3. System Startup and Runner Loop

### main.py Bootstrap

`main.py` performs three operations:

1. `load_dotenv()` — loads `.env` into `os.environ`
2. Reads `VOLTEDGE_LIVE_MODE`, `VOLTEDGE_PER_TRADE_CAPITAL`, `VOLTEDGE_MAX_TRADES_PER_DAY` with fallbacks (defaults: `0`, `300`, `3`)
3. Calls `src.runner.run_loop(live_mode, per_trade_capital, max_trades_per_day)`

Actual .env values override: `VOLTEDGE_PER_TRADE_CAPITAL=10000`, `VOLTEDGE_MAX_TRADES_PER_DAY=5`.

### runner.py — The Main Event Loop

`run_loop()` in `src/runner.py` is a `while True:` loop with a 60-second sleep. Every iteration:

1. Gets current IST time: `now = datetime.now(IST)`
2. Checks if `current_date != risk_state.trading_date` → midnight reset
3. Fires scheduled pre-market jobs (time-gated, cascade-protected)
4. If `MARKET_START <= current_time <= MARKET_END`: runs intraday cycle
5. Fires scheduled post-market jobs

**Scheduler cascade prevention** — `_should_fire_scheduled_job()`:

```python
def _should_fire_scheduled_job(scheduled_time, runner_start_time, current_time) -> bool:
    # Fire only if: past scheduled time AND either (runner started before it OR
    # within 30-minute grace window after restart)
    if current_time < scheduled_time:
        return False
    if runner_start_time <= scheduled_time:
        return True
    sched_min = scheduled_time.hour * 60 + scheduled_time.minute
    current_min = current_time.hour * 60 + current_time.minute
    return (current_min - sched_min) <= 30
```

This prevents "cascade firing" — if the service restarts at 14:00, it will not re-fire the 09:00, 09:30, 10:00 jobs that already fired earlier in the day.

### Market Hours Detection

```python
MARKET_START = dt_time(9, 15)   # 09:15 IST
MARKET_END   = dt_time(15, 30)  # 15:30 IST
```

Only weekdays (`weekday < 5`) are processed.

### Daily Scheduled Jobs

| Time (IST) | Job | Module |
|------------|-----|--------|
| 08:00 | F&O ban list + T2T refresh | `src/data_ingestion/short_ban_list.py` |
| 08:30 | Pre-market macro check (NewsData) + Gemini sentiment scoring | `src/data_ingestion/news_context.py`, `src/juror/catalyst_analyzer.py` |
| 08:30 | Pre-Market Intelligence composite score | `src/data_ingestion/pre_market_intelligence.py` |
| 08:30 | Grok Morning Strategist call | `src/llm/grok_client.py` |
| 09:00 | HYDRA pre-market event scan | `src/strategies/hydra.py` |
| 09:30 | Momentum scanner + VIPER initial scan | `src/sniper/momentum_scanner.py`, `src/strategies/viper.py` |
| 10:00 | VIPER re-scan #1 | `src/strategies/viper.py` |
| 10:30 | VIPER re-scan #2 | `src/strategies/viper.py` |
| 11:00 | VIPER re-scan #3 | `src/strategies/viper.py` |
| 12:00 | VIPER re-scan #4 | `src/strategies/viper.py` |
| 12:00 | Mid-session negative news pulse | `src/data_ingestion/news_context.py` |
| 12:00 | Mid-Session Pulse report (email) | `src/reports/mid_session_pulse.py` |
| 09:17 | Grok Portfolio Optimizer | `src/llm/grok_client.py` |
| 09:30 | Grok Portfolio Optimizer | `src/llm/grok_client.py` |
| 10:00 | Grok Portfolio Optimizer | `src/llm/grok_client.py` |
| 10:45 | Grok Portfolio Optimizer | `src/llm/grok_client.py` |
| 11:45 | Grok Portfolio Optimizer | `src/llm/grok_client.py` |
| 15:40 | Grok EOD review + Post-market report | `src/llm/grok_client.py`, `src/reports/post_market_report.py` |

### Intraday 15-Minute Cycle (09:15-15:30)

Each cycle (every loop iteration, ~60s sleep between):

1. Drain `tick_exit_queue` — process any stop-loss signals from the BarBuilderThread
2. Drain `ExitMonitorThread` queue — process exit signals from the 1-second exit monitor
3. **HYDRA evaluation** — parallel evaluation of watchlist entries via `ThreadPoolExecutor(max_workers=4)`:
   - Fetch 70 minutes of intraday bars
   - Compute streaming TA via `TechnicalBody.compute_or_stream()`
   - Analyze order book depth via `analyze_depth()`
   - Score conviction via `hydra.evaluate()`
4. **ConvictionEngine tick** — fetch `MarketSnapshot` → `conviction_engine.tick()` → get triggered signals
5. **VIPER evaluation** (if movers available) — same parallel pattern as HYDRA
6. **Trade execution** — for each triggered signal, run the full risk stack, then `executor.execute_buy()` or `executor.execute_short_sell()`

### Daily Reset at Midnight

When `current_date != risk_state.trading_date`:

```python
risk_state.reset_for_new_day(current_date)
daily_traded_symbols.clear()
discovery.reset()
hydra.reset_daily()
slot_manager.reset_daily()
grok_call_count = 0
grok_morning_plan = None
grok_optimizer_index = 0
grok_last_actions = []
pre_market_intel = None
conviction_engine.reset_daily()
last_market_snapshot = None
reset_conviction_history()     # Grok stability tracker
reset_streaming_state()        # TechnicalBody streaming states
exit_engine._divergence_warned.clear()
# Auto-login: refresh Zerodha access token via TOTP
new_token = auto_refresh_access_token(env_file=".env")
```

### Outside Market Hours

Outside 09:15-15:30 IST (weekdays) and on weekends, the runner:
- Still fires pre-market jobs at their scheduled times (08:00, 08:30, 09:00)
- Still fires post-market jobs (12:00 report, 15:40 EOD review)
- Skips the intraday evaluation cycle entirely
- Sleeps for the full 60-second interval

---

## 4. Data Ingestion Layer

### 4a. Pre-Market Intelligence

**File:** `src/data_ingestion/pre_market_intelligence.py`

Computes a composite 0-100 score predicting today's market direction from 8 independent signals. Replaces the legacy FII-only regime logic.

**Signal Tiers and Maximum Points:**

| Signal | Source | Max Bullish | Max Bearish | Tier |
|--------|--------|-------------|-------------|------|
| SPY (S&P 500 close) | Finnhub | +12 | -12 | A |
| QQQ delta vs SPY | Finnhub | +3 | -3 | A |
| Brent Crude | Finnhub OANDA:BCO_USD | +8 | -8 | B |
| USD Strength (EUR/USD inverse) | Finnhub OANDA:EUR_USD | +5 | -5 | B |
| USD/INR direction | Finnhub OANDA:USD_INR | +3 | -3 | B |
| India VIX | Kite Connect LTP | +3 | -4 | C |
| Nifty PCR | NSE scraper | +3 | -3 | C |
| FII Cash Flow (yesterday) | NSE FII/DII data | +6 | -6 | D |
| DII Offset | NSE DII data | +3 | 0 | D |

**Score → Tier mapping:**

```
Score 70-100 → RISK_ON    (global signals bullish)
Score 55-69  → CLEAR      (neutral to mildly positive)
Score 40-54  → CAUTION    (mixed or mildly negative)
Score 25-39  → RISK_OFF   (clearly bearish)
Score 0-24   → EXTREME    (crisis-level)
```

Baseline is 50 (neutral). Each available signal adds or subtracts points. If all 8 signals are available, maximum possible range is ~0-100 (in practice 10-90).

**Key classes:**
- `SignalContribution`: one signal's contribution (name, value_str, points, available, label)
- `PreMarketIntelligence`: complete result (composite_score, signals list, tier_name)
- `fetch_and_compute(kite_client, macro_context)`: high-level convenience function

**Log format:**
```
[PreMkt] Score=62/100 → CLEAR | SPY=+0.8%(+7) Crude=-1.2%(+4) ... | Signals: 6/8 available
```

### 4b. Macro Context

**File:** `src/data_ingestion/macro_context.py`

Provides `MacroContext` — a unified snapshot of all macro intelligence — and `MacroRiskTier` for direction-aware conviction dampening.

**MacroRiskTier direction dampeners:**

| Tier | LONG adjustment | LONG min conviction | SHORT adjustment | SHORT min conviction |
|------|----------------|--------------------|-----------------|--------------------|
| RISK_ON | +10pts | 60 | -15pts | 80 |
| CLEAR | 0pts | 70 | 0pts | 70 |
| CAUTION | -10pts | 65 | +5pts | 60 |
| RISK_OFF | -20pts | 75 | +10pts | 60 |
| EXTREME | HALT (-999) | 999 | +15pts | 65 |

**Tier determination (v3 priority order):**
1. Circuit breaker active → EXTREME (override everything)
2. Nifty gap down > 2% at open → EXTREME
3. Composite pre-market score (when available) → tier from score
4. Fallback: FII-ratio vs 7-day rolling average → tier from ratio

**Refresh cycle:** every 90 minutes (`REFRESH_INTERVAL_SECONDS = 5400`)

**FII history:** persisted to `data/fii_history.json`, max 30 entries. 7-day rolling average is computed for dynamic tier classification.

**Staleness detection:** if all macro values are unchanged for 90+ minutes, a warning is logged. This catches stale Finnhub data.

**Log format:**
```
[MacroRisk] Tier-2 RISK_OFF | composite=42/100 | FII=-8500Cr | 7d_avg=-4200Cr | ratio=2.02x | LONG: -20pts (min 75) | SHORT: +10pts (min 60)
```

### 4c. Finnhub Client

**File:** `src/data_ingestion/finnhub_client.py`

Fetches forex/commodity quotes and US equity data from Finnhub free tier (60 calls/minute).

**Tracked symbols:**

| Symbol | Name |
|--------|------|
| OANDA:XAU_USD | Gold (USD/oz) |
| OANDA:BCO_USD | Brent Crude (USD/bbl) |
| OANDA:USD_INR | USD/INR |
| OANDA:EUR_USD | EUR/USD |
| OANDA:GBP_USD | GBP/USD |
| SPY | S&P 500 (SPY) |
| QQQ | Nasdaq 100 (QQQ) |

**DXY note:** UUP ETF proxy is broken (~$27 vs real DXY ~104). Dollar strength is assessed via EUR/USD inverse (EUR is 57% of the DXY basket). DXY symbol kept for backward compatibility but not used in scoring.

**Rate limiting:** tracks call timestamps in `_CALL_TIMESTAMPS` list; pauses if 50 calls in last 60 seconds.

**Staleness check:** for US market quotes, discards any quote older than 48 hours (timestamp check).

### 4d. Short Ban List

**File:** `src/data_ingestion/short_ban_list.py`

Two independent lists that gate SHORT trades:

**F&O Ban List:** NSE `/api/securities-under-ban` — stocks temporarily banned from new F&O positions. Updated daily. Fail-open: if fetch fails, ban list is empty (do not block all trades). Cached to `data/fo_ban_cache.json`.

**T2T / BE Series:** NSE bhavcopy SERIES column. Restricted series: `{"BE", "BZ"}`. Stocks in these series require compulsory delivery — no intraday short-selling allowed. Tries today's bhavcopy, then falls back up to 3 days.

**Gate function:** `is_safe_to_short(symbol)` — the single function `executor.py` calls before any SHORT order.

**Refresh time:** 08:00 IST daily via runner.

---

## 5. Strategy Layer

### Architecture: Dragon Pattern

All strategies inherit from `StrategyHead` (in `src/strategies/base.py`). The two live heads are HYDRA and VIPER. They share the `TechnicalBody` for TA computation and communicate only via `SlotManager`.

**Core dataclasses:**

```python
@dataclass
class ConvictionScore:
    strategy: str
    symbol: str
    direction: str              # "BUY" or "SHORT"
    total: float = 0.0          # Final score 0-100
    event_strength: float = 0.0     # 0-70 (HYDRA) or 0-30 (VIPER)
    technical_confirm: float = 0.0  # 0-22
    depth_signal: float = 0.0       # 0-10
    context_bonus: float = 0.0      # 0-10
    llm_conviction: float = 0.0     # 0-20 (Grok weighted)
    reasoning: str = ""

@dataclass
class WatchlistEntry:
    symbol: str
    direction: str              # "BUY" or "SHORT"
    event_summary: str = ""
    urgency: float = 0.0        # 1-10 from Groq
    conviction: Optional[ConvictionScore] = None
    metadata: dict = field(default_factory=dict)
```

### 5a. HYDRA Strategy

**File:** `src/strategies/hydra.py`

HYDRA is the event-driven catalyst head. It waits for VWAP retests after catalysts rather than chasing initial spikes.

**Scan schedule:**
- 09:00 IST: full scan for events since yesterday's close (min_urgency=6.0)
- Every market cycle: incremental scan for new events (min_urgency=7.0 — higher bar mid-day)
- Max watchlist: 5 symbols

**Scoring formula (max 102 raw, capped at 100):**

```
Base conviction = Event Strength (0-70) + TA Confirmation (0-22) + Depth Signal (0-10)
```

**Event Strength:** `urgency * 7.0` → urgency 10 = 70 points.

**TA Confirmation (0-22)** via `HydraRules.confirms_event()`:

| Component | Direction | Max Points |
|-----------|-----------|-----------|
| Volume spike ratio | BUY/SHORT | 8 |
| VWAP proximity | BUY (above), SHORT (below) | 5 |
| EMA 9/20 alignment | BUY: 9>20, SHORT: 9<20 | 4 |
| ORB breakout/breakdown | BUY: breakout, SHORT: breakdown | 5 |
| Bollinger Band bonus | +vol confirms | 3 |
| RANGING regime penalty | ADX < 20 | -5 |

**Regime-based weight multipliers:**

| Regime | Condition | Volume | VWAP | EMA | ORB |
|--------|-----------|--------|------|-----|-----|
| TRENDING | ADX ≥ 25, DI aligned | 1.0 | 1.0 | 1.3 | 1.0 |
| BREAKOUT | Vol ≥ 2x, ADX ≥ 18 | 1.5 | 0.8 | 1.0 | 1.4 |
| RANGING | ADX < 20 | 0.8 | 1.3 | 0.7 | 0.6 |
| EXHAUSTION | RSI > 78 (BUY) or < 22 (SHORT) | 0.7 | 1.2 | 0.8 | 0.5 |
| NORMAL | Default | 1.0 | 1.0 | 1.0 | 1.0 |

**Note:** RSI is intentionally ignored for event-driven HYDRA — overbought is expected after a catalyst.

**Illiquid hard kill:** if `depth_analysis.signal == "illiquid"`, conviction is set to 0.0 immediately.

**Pattern database:** saves trade results to `data/hydra_pattern_db.json` (last 200 trades).

**Daily reset:** `reset_daily()` clears watchlist, `_seen_headlines` set in `EventScanner`, and `trade_placed_today` flag.

### 5b. VIPER Strategy

**File:** `src/strategies/viper.py`

VIPER is the momentum mover head. Scans top gainers/losers, classifies move type, and scores them for either STRIKE (continuation) or COIL (reversal) trades.

**COIL invariant:** COIL mode = DRY_RUN only. COIL signals are logged and analyzed but never executed as live trades.

**Scan schedule:** initial at 09:30, re-scans at 10:00, 10:30, 11:00, 12:00. Max watchlist: 10 symbols.

**Scoring formula (max 75 raw, capped at 100):**

```
Base conviction = Move Quality (0-30) + TA Confirmation (0-25) + Depth Signal (0-10) + Context Bonus (0-10)
```

**Move Quality (0-30):**
- Price magnitude: ≥5% → 12pts, ≥3% → 8pts, ≥2% → 4pts, ≥1% → 2pts
- Gap quality: ≥3% gap + 2x vol → 8pts, ≥2% gap + 1.5x vol → 5pts, ≥1.5% → 3pts, ≥0.5% → 1pt
- Volume conviction: ≥2.5x → 10pts, ≥2x → 6pts, ≥1.5x → 3pts, ≥1x → 1pt
- COIL penalty: ×0.80 (counter-trend requires stronger evidence)

**WARNING:** `volume_ratio` is computed as `abs(pct_change) / 2` — a price-derived proxy, NOT actual relative volume. This is a known limitation logged at startup.

**Context Bonus (0-10):**
- Sector leader in SECTOR_WAVE: +5pts
- Volume ≥ 2.5x: +3pts
- COIL post-11:00 IST: +2pts

**COIL dry-run logging:** saved to `logs/viper_coil/YYYY-MM-DD_coil_report.json` at EOD.

**Confluence check:** `check_confluence(hydra_symbols)` — finds symbols in both watchlists. When detected, `SlotManager.register_confluence()` is called and a +15 bonus is applied.

### 5c. AntigravityWatcher

**File:** `src/sniper/antigravity_watcher.py`

A VWAP-bounce state machine. Watches stocks that Sniper's `evaluate_signal()` returned `WAIT` status for, specifically when the antigravity signal shows `WAITING_FOR_GRAVITY`.

**States:**
- `WAITING_FOR_GRAVITY`: price well above VWAP, waiting for pullback (>0.2% from VWAP)
- `WAITING_FOR_BOUNCE`: touched VWAP, waiting for green confirmation candle
- `COMPLETED`: bounce confirmed, signal emitted
- `CANCELLED`: 90-minute timeout without bounce

**VWAP touch band:** 0.2% — price within this % of VWAP triggers state transition.

### SlotManager

**File:** `src/strategies/slot_manager.py`

The global trade budget controller. Acts as the single arbiter for all trade slots.

**Key constants:**
- `CONVICTION_THRESHOLD = 70.0` — minimum score to trade
- `CONFLUENCE_BONUS = 15.0` — added when HYDRA and VIPER both flag a symbol
- `MAX_OPEN_POSITIONS = 5` — safety rail; can be set via `VOLTEDGE_MAX_OPEN_POSITIONS`

**Capital allocation:**
- conviction ≥ 85 → 100% of `per_trade_capital`
- conviction 70-84 → 70% of `per_trade_capital`
- conviction < 70 → no trade (0%)

**Confluence note:** The +15 bonus is already added to the conviction score before calling `get_capital_allocation()`. The capital allocation does NOT apply a separate multiplier for confluence (this would double-count the risk).

**`can_trade(symbol, direction)` checks:**
1. Max open positions not reached
2. Symbol not already locked
3. No opposite-direction lock on same symbol

---

## 6. Conviction Engine (The Core)

**File:** `src/trading/conviction_engine.py`

The dynamic multi-layer conviction system. Every signal lives on the watchboard and is recomputed every 15-minute cycle. Signals wait until conditions align, then fire.

### Five-Layer Formula

```
conviction = (A × 0.25) + (B × 0.15) + (C × 0.30) + (D × 0.20) + (E × 0.10)
```

| Layer | Weight | Name | Description | Dynamic? |
|-------|--------|------|-------------|---------|
| A | 25% | Market State | Phase-derived, direction-aware score | Yes — recomputed every cycle |
| B | 15% | Sector Context | Sector relative strength vs Nifty | Yes — recomputed every cycle |
| C | 30% | Catalyst Quality | Signal quality from HYDRA/VIPER | FROZEN at creation |
| D | 20% | Technical Confirmation | VWAP, ORB, volume, EMA, MACD | Yes — recomputed every cycle |
| E | 10% | Pattern Match | Historical win rate from PatternDB | FROZEN at creation |

### ActiveSignal Dataclass

```python
@dataclass
class ActiveSignal:
    symbol: str
    direction: str              # "BUY" or "SHORT"
    strategy: str               # "HYDRA", "VIPER", "V2_DISCOVERY"
    layer_c_score: float        # Catalyst quality 0-100, FROZEN
    layer_e_score: float = 50.0 # Pattern match, cold start at 50
    event_summary: str = ""
    created_at: Optional[datetime] = None
    conviction_history: List[Tuple[str, float, str]] = field(default_factory=list)
    status: str = "WATCHING"    # WATCHING, TRIGGERED, EXPIRED
    last_conviction: float = 0.0
    metadata: dict = field(default_factory=dict)
```

### Execution Threshold

`CONVICTION_THRESHOLD = 70.0` — universal gate. No trade is placed below 70.

### Signal Expiry

Signals expire when:
- Past 14:30 IST (no new entries in last hour)
- Older than 4 hours (`SIGNAL_MAX_AGE_HOURS = 4.0`)
- Weak catalyst (Layer C < 50) with conviction stuck below 50 after 3+ cycles

### ConvictionEngine.tick() Log Format

```
[ConvEng] RELIANCE BUY | A=65 B=70 C=80 D=75 E=55 → conviction=72 | phase=trending_bull | prev=68 | Δ=+4
```

When triggered:
```
[ConvEng] *** TRIGGERED *** RELIANCE BUY conviction=72 >= 70 | waited 3 cycles
```

### Morning Regime Bias

`set_morning_regime_bias(bias)` — Grok's pre-market assessment adjusts Layer A by ±10 points for the entire day. Range: -10 to +10.

---

## 7. Market Phase Classification

**File:** `src/trading/market_phase.py`

Classifies the NSE market into one of 6 phases every cycle, using Nifty 50 LTP, 5-minute direction, Advance/Decline ratio, and India VIX.

### Phase Rules (evaluated in priority order)

| Priority | Phase | Condition |
|----------|-------|-----------|
| 1 | PANIC | Nifty < -1.5% AND direction DOWN AND time < 09:45 AND VIX > 16 |
| 2 | RECOVERY | Recovery from low > 0.5% AND A/D improving AND prev phase was PANIC/STABILISATION/TRENDING_BEAR |
| 3 | STABILISATION | Was PANIC/BEAR AND direction FLAT AND Nifty < -0.3% |
| 4 | TRENDING_BULL | Nifty > +0.3% AND direction UP AND A/D > 0.6 |
| 5 | TRENDING_BEAR | Nifty < -0.3% AND direction DOWN AND A/D < 0.4 AND time >= 09:45 |
| 6 | CHOPPY | Default (everything else) |

### Layer A Base Scores per Phase

| Phase | BUY score | SHORT score |
|-------|-----------|-------------|
| PANIC | 10 | 85 |
| STABILISATION | 35 | 55 |
| RECOVERY | 65 | 30 |
| TRENDING_BULL | 85 | 15 |
| TRENDING_BEAR | 15 | 80 |
| CHOPPY | 45 | 45 |

### VIX and A/D Fine-Tuning Modifiers (applied to base)

| Condition | BUY effect | SHORT effect |
|-----------|------------|-------------|
| VIX > 22 | -10 | +5 |
| VIX < 14 | +5 | -5 |
| A/D > 0.65 | +10 | -10 |
| A/D < 0.35 | -10 | +10 |

**Phase transition log format:**
```
[Phase] choppy → trending_bull at 09:47 IST | Nifty=+0.4% | A/D=0.68 | VIX=13.2
```

### Data Sources

- Nifty 50 LTP and OHLC: Kite Connect `ltp()` and `ohlc()`
- India VIX: Kite Connect `ltp("NSE:INDIA VIX")`
- A/D ratio: `nsepython.nse_get_advances_declines()`
- Nifty 5-min direction: heuristic from LTP delta vs previous cycle's snapshot
- Sector indices: Kite Connect OHLC for 8 sectors (PHARMA, IT, BANKING, ENERGY, AUTO, METALS, FMCG, INFRA)

---

## 8. Juror and Signal Scoring

**Files:** `src/juror/catalyst_analyzer.py`, `src/juror/gemini_client.py`

The Juror uses Google Gemini to classify corporate events and score pre-market macro sentiment.

**CatalystAnalyzer:**
- `analyze_premarket_macro(headlines)` — scores a batch of headlines into a sentiment float (-1 to +1), used to set the `daily_regime.json` trend and strength
- Score > 0.2 → "bullish", Score < -0.2 → "bearish", else "sideways"

**JurorSignal DB model:**
```
juror_signals table:
  symbol, label (Positive/Negative/Neutral), confidence (0-1), reason, raw_text
```

**daily_decision_engine.py** — the pipeline between Juror and Sniper:
- Reads last 50 `JurorSignal` rows from SQLite
- Filters: `label == "Positive"` AND `confidence >= 0.80` (`MIN_JUROR_CONFIDENCE = 0.80`)
- For each passing signal, calls `sniper.core.evaluate_signal(symbol)`
- If `status == "KEEP"`: queued for execution
- If `status == "WAIT"` with antigravity detected: handed to `AntigravityWatcher`

---

## 9. Risk Management

The risk stack is evaluated in strict priority order. Any failure at any layer blocks the trade.

### Risk Stack (12 Layers)

| Layer | Module | Condition to Block |
|-------|--------|--------------------|
| 1 | `slot_manager.py` | Conviction < 70 |
| 2 | `slot_manager.py` | Max open positions reached or symbol locked |
| 3 | `daily_risk_state.py` | Daily P&L loss >= `max_daily_loss_rupees` |
| 4 | `atr.py` | ATR position sizing fails (stop > 2.5% hard cap) |
| 5 | `trading_costs.py` | Expected move < 3× breakeven cost (3:1 reward:cost) |
| 6 | `depth_analyzer.py` | Depth signal == "illiquid" |
| 7 | `circuit_limits.py` | Stock within 2% of circuit limit |
| 8 | `sector_guard.py` | Sector already has 2 positions |
| 9 | `time_of_day.py` | Time is OPENING_CHAOS (09:15-09:30) or SQUARE_OFF (15:15+) |
| 10 | `time_of_day.py` | F&O expiry risk factor |
| 11 | `macro_context.py` | MacroRiskTier EXTREME for LONG |
| 12 | `short_ban_list.py` | SHORT: symbol in F&O ban or T2T list |

### ATR-Based Position Sizing

**File:** `src/trading/atr.py`

Uses Wilder's ATR (14-period). Stop distance = 1.5× ATR. Position size = capital_at_risk / stop_distance.

```python
# Example: ₹500 risk, ATR=₹10, stop_distance=₹15 → 33 shares
compute_atr_position_size(capital_at_risk=500, stop_distance=15) → 33
```

### DailyRiskState

**File:** `src/trading/daily_risk_state.py`

Thread-safe using `threading.RLock`. All monetary fields use `decimal.Decimal` internally to prevent float drift. Public API exposes `float`.

- `realized_pnl`: closed trades P&L
- `unrealized_pnl`: open positions MTM
- `daily_pnl`: realized + unrealized (used for loss-cap check)
- `add_realized_pnl(amount)`: preferred thread-safe accumulation
- `reset_for_new_day(date)`: clears all fields at midnight

### RiskConfig Parameters

| Parameter | Default | Source |
|-----------|---------|--------|
| `intraday_stop_pct` | 2.5% | `VOLTEDGE_INTRADAY_STOP_PCT` |
| `intraday_exit_time` | 15:20 | `VOLTEDGE_INTRADAY_EXIT_TIME` |
| `min_price_rupees` | 50.0 | `VOLTEDGE_MIN_PRICE_RUPEES` |
| `min_avg_daily_turnover_rupees` | 2,000,000 | `VOLTEDGE_MIN_AVG_DAILY_TURNOVER_RUPEES` |
| `max_shares_per_trade` | 200 | `VOLTEDGE_MAX_SHARES_PER_TRADE` |
| `weak_market_size_factor` | 0.5 | `VOLTEDGE_WEAK_MARKET_SIZE_FACTOR` |
| `strong_market_size_factor` | 1.0 | `VOLTEDGE_STRONG_MARKET_SIZE_FACTOR` |

### Trading Costs (Zerodha MIS Intraday)

**File:** `src/trading/trading_costs.py`

Uses `decimal.Decimal` throughout to prevent float accumulation error.

| Charge | Rate |
|--------|------|
| Brokerage | min(₹20 flat, 0.03%) per order |
| STT | 0.025% on sell side (intraday) |
| NSE Exchange | 0.00345% on turnover |
| GST | 18% on brokerage + exchange |
| SEBI | ₹10 per crore (0.0001%) |
| Stamp Duty | 0.003% on buy side |

**Viability gate:** `is_trade_viable()` — expected move must be ≥ 3× breakeven cost.

### Time-of-Day Gates

**File:** `src/trading/time_of_day.py`

| Phase | Time | Entry Allowed | Score Modifier |
|-------|------|--------------|----------------|
| OPENING_CHAOS | 09:15-09:30 | No | ×0.5 |
| PRIME_TIME | 09:30-10:30 | Yes | ×1.2 |
| TREND_CONFIRM | 10:30-11:30 | Yes | ×1.0 |
| LUNCH_LULL | 11:30-13:30 | No | ×0.7 |
| AFTERNOON_BUILD | 13:30-14:30 | Yes | ×0.9 |
| CLOSING_MOMENTUM | 14:30-15:15 | No | ×0.6 |
| SQUARE_OFF | 15:15-15:30 | No | ×0.0 |

### Depth Analyzer

**File:** `src/trading/depth_analyzer.py`

Uses Kite Level-2 (5-level) order book data from full-mode WebSocket ticks.

**DepthAnalysis fields:** `bid_ask_spread_pct`, `buy_depth_qty`, `sell_depth_qty`, `imbalance_ratio`, `buy_wall_detected` (single level >40% of total), `sell_wall_detected`, `is_liquid` (spread < 0.1%), `signal` ("strong_bid" | "balanced" | "strong_ask" | "illiquid").

**Illiquid kill:** if `signal == "illiquid"`, conviction is set to 0.0 immediately — hard kill, no override.

---

## 10. Broker Integration

**File:** `src/brokers/zerodha_client.py`

Wraps `kiteconnect.KiteConnect` with a persistent HTTP session for reduced latency.

**Connection optimization:** `HTTPAdapter(pool_connections=1, pool_maxsize=4, max_retries=1)` — reuses existing TCP+TLS connection, eliminating 15-30ms per order.

**Order methods on `TradeExecutor` (`src/trading/executor.py`):**

| Method | Direction | Kite order type |
|--------|-----------|----------------|
| `execute_buy(symbol, ltp, qty)` | LONG entry | MARKET BUY |
| `execute_sell(symbol, qty, ltp)` | LONG exit | MARKET SELL |
| `execute_short_sell(symbol, ltp, qty)` | SHORT entry | MARKET SELL (MIS) |
| `execute_short_cover(symbol, qty, ltp)` | SHORT exit | MARKET BUY (MIS) |

All methods check `VOLTEDGE_LIVE_MODE` — in DRY_RUN mode they log and return success without placing actual orders.

**SHORT gate:** `execute_short_sell()` calls `is_safe_to_short(symbol)` before any other logic.

### Daily Auto-Login

**File:** `src/tools/auto_login.py`

`auto_refresh_access_token(env_file=".env")` runs at midnight every day to generate a fresh Zerodha access token via:
1. Automated browser login to Kite using stored password
2. TOTP 2FA via `pyotp` with `ZERODHA_TOTP_SECRET`
3. Updates `ZERODHA_ACCESS_TOKEN` in the `.env` file
4. Returns new token string

If auto-login fails, a CRITICAL log is emitted and the engine continues but cannot trade.

---

## 11. Exit Engine

**File:** `src/trading/exit_engine.py` (v3)

Smart exit logic with phase-adaptive, volume-aware trailing stops and partial profit taking.

### Exit Conditions (LONG position, checked every tick)

1. **Time exit:** force exit at 15:20 IST (MIS auto-close protection)
2. **Hard stop:** LTP <= initial_stop_price (prevents gap-down losses)
3. **Breakeven activation:** when unrealized P&L >= 1R, move stop to entry price
4. **Adaptive trail:** ATR multiplier changes based on time and profit:

| Phase | Trigger | ATR Multiplier |
|-------|---------|----------------|
| Settle | First 15 min after entry | 2.0× |
| Confirm | 15-45 min or at breakeven | 1.5× |
| Lock | Profit > 1.5R | 1.0× |
| Accelerate | Profit > 2.5R | 0.75× |

5. **Fake dip filter:** if dip volume < 40% of rally volume AND VWAP holds → delay stop trigger for up to 2 bars (`FAKE_DIP_GRACE_BARS = 2`)
6. **Partial exits:** 50% at 1.5R, 25% more at 2.5R, remaining 25% runs with Accelerate trail
7. **Momentum exhaustion:** gave back > 60% of peak gain with high volume

For VIPER: `VIPER_PARTIAL_1_R = 2.0` and `VIPER_PARTIAL_2_R = 3.0` (momentum runs further).

For SHORT positions: all logic is inverted (exit on price rising, trail from lowest point).

### ExitMonitorThread

**File:** `src/trading/exit_monitor.py`

Runs as a daemon thread, evaluating exit conditions every 1 second. Completely decoupled from the 60-second main loop. Exit signals are queued and drained by the main thread.

### Tick-Based Exit Pipeline

Ticks flow: Kite WebSocket → `_on_ticks` (O(1)) → `SimpleQueue` → `BarBuilderThread` → `exit_engine.check_tick()` → `tick_exit_queue`. Stop-loss detection latency: <1ms.

---

## 12. LLM Integration

### Groq — Llama-3.3-70B (Event Classification)

**File:** `src/llm/groq_client.py`

- **Purpose:** rapid event urgency classification (~300ms latency)
- **Model:** `llama-3.3-70b-versatile`
- **Budget:** 14,400 req/day (free tier) — not actively tracked
- **Usage:** `classify_event(symbol, headline, category, body)` → returns urgency 1-10, direction, event_type, summary, material flag
- **Batch mode:** `classify_events_batch(events)` uses `ThreadPoolExecutor` with up to 10 workers — all calls issue simultaneously, total latency = slowest single call
- **Fallback:** returns `{"urgency": 0, "direction": "NEUTRAL", ...}` on any failure

**Urgency scale:**
- 9-10: Market-moving NOW (earnings beat >10%, major acquisition, FDA rejection)
- 7-8: Significant catalyst (inline earnings + strong guidance, FII bulk deal)
- 5-6: Moderate (board meeting outcome, minor corporate action)
- 3-4: Low (routine filings, AGM notice)
- 1-2: Noise (compliance filing)

### Grok 4 — Portfolio Orchestrator

**File:** `src/llm/grok_client.py`

- **Purpose:** portfolio-level strategic reasoning (not per-symbol micro-analysis)
- **Model:** `grok-4` via `https://api.x.ai/v1` (OpenAI-compatible API)
- **Budget:** `GROK_DAILY_BUDGET = 10` calls/day (~7 typical)

**Three call types:**

| Function | Called When | Input | Output |
|----------|-------------|-------|--------|
| `grok_morning_strategist()` | 08:30 IST | macro context, HYDRA events, risk budget | regime, ranked watchlist, avoid list, urgency_delta adjustments |
| `grok_portfolio_optimizer()` | 09:17, 09:30, 10:00, 10:45, 11:45 | all open positions, both strategy candidates, risk state | prioritized action list |
| `grok_eod_review()` | 15:40 IST | trade outcomes, signal accuracy | learning notes |

**Safety invariant:** Grok PROPOSES; hard-coded risk DISPOSES. Every Grok output is validated by SlotManager, DailyRiskState, and the full risk stack before any trade is executed.

**Conviction stability check:** `_check_conviction_stability(symbol, conviction)` — if conviction swings >30pts in <30 minutes with no new data, logs a WARNING and uses the average of old and new values.

**JSON extraction:** `_extract_json(raw)` strips `<think>` reasoning blocks, handles fenced code blocks, and falls back to regex extraction from raw text.

### Gemini 2.0 — Reports and Macro Analysis

**File:** `src/juror/gemini_client.py`, `src/reports/pre_market_brief.py`

- **Purpose:** generate daily email reports and score pre-market macro sentiment
- **Model:** Google Gemini 2.0 via `google-genai` SDK with `urlContext` for live news access
- **Key use:** morning brief generation (`generate_pre_market_brief()`), catalyst analysis (`CatalystAnalyzer.analyze_premarket_macro()`)

---

## 13. Reporting System

Three daily email reports sent via SMTP (Gmail, port 587, STARTTLS). All reports are saved as `.md` files in `logs/daily_reports/`.

### Report 1: Pre-Market Morning Brief (08:30 IST)

**File:** `src/reports/pre_market_brief.py`

- Pulls last 12 hours of global news via Finnhub + Gemini Search
- Generates 5 specific stock predictions with direction and reasoning
- Includes learning context from `data/prediction_log.json` (last 5 scored predictions and lessons)
- Saves predictions to `data/prediction_log.json` for evening feedback loop
- Duplicate guard: skips if today's brief already exists at expected path
- **Known bug:** fires at 06:00 UTC (11:30 IST) instead of 03:30 UTC (09:00 IST)

**Saved to:** `logs/daily_reports/YYYY-MM-DD_morning_brief.md`

### Report 2: Mid-Session Pulse (12:00 IST)

**File:** `src/reports/mid_session_pulse.py`

- Pure template report — NO LLM call. Runs in <5 seconds.
- Sections:
  1. Market Phase: current phase, VIX, A/D, Nifty %, 5m direction, transitions
  2. Conviction Watchboard: all WATCHING signals with A/B/C/D/E breakdown
  3. Morning Plan Check: predictions vs current prices
  4. Position Status: open trades, unrealized P&L, slots used/remaining
  5. Flags: warnings and alerts

**Saved to:** `logs/daily_reports/YYYY-MM-DD_mid_session.md`

### Report 3: Post-Market Debrief (15:40 IST)

**File:** `src/reports/post_market_report.py` (v2)

- Complete daily audit integrating conviction engine, phase timeline, and trades.
- Sections:
  0. System Health (ALWAYS populated) — email status, Kite token, VIPER scan health, API failures
  1. Pre-Market Plan vs Reality — predictions vs outcomes
  2. Conviction Engine Audit — all signals, lifecycle, Layer breakdown
  3. Market Phase Timeline — transitions with timestamps
  4. Trades Executed — win rate, P&L per trade
  5. Market Context and Top Movers
  6. Tomorrow's Setup

**Saved to:** `logs/daily_reports/YYYY-MM-DD_post_market.md`

### Feedback Loop (EOD)

**File:** `src/reports/feedback_loop.py`

Scores each morning brief prediction against actual market data:
- +1: predicted direction matches actual move > 0.3%
- 0: stock moved < 0.3% (flat, scored 0)
- -1: predicted direction is opposite of actual move

Generates Gemini lessons ("system_lessons") that are injected into tomorrow's morning brief context.

Saves updated scores and lessons to `data/prediction_log.json`.

### Email Sender

**File:** `src/reports/email_sender.py`

- Single `send_report_email(subject, body_md, attachment_path)` function used by all reports
- Checks `REPORT_EMAIL_ENABLED == "1"` first
- STARTTLS on port 587 (gmail)
- Always logs outcome — never silently returns
- `validate_email_config()` called at runner startup and printed to banner

---

## 14. Pattern Database and Layer E Learning

**File:** `src/trading/pattern_db.py`

**Persistent file:** `data/pattern_db.json`

The Pattern Database records every signal outcome and uses it to compute Layer E (historical win rate) for future similar signals.

### PatternFingerprint

A 7-dimensional key that identifies a trade setup:

```python
@dataclass
class PatternFingerprint:
    strategy: str       # HYDRA, VIPER
    direction: str      # BUY, SHORT
    phase_at_trigger: str  # MarketPhase value
    sector: str         # From SECTOR_MAP
    catalyst_type: str  # "earnings", "acquisition", "upgrade", "downgrade", "momentum", "unknown"
    time_bucket: str    # "first_hour" (<10:15), "mid_session", "last_hour" (>14:00)
    vix_regime: str     # "low" (<14), "normal" (14-22), "elevated" (>22)
```

### PatternOutcome

```python
@dataclass
class PatternOutcome:
    fingerprint: PatternFingerprint
    triggered: bool     # Did conviction reach 70?
    pnl_pct: float      # Realized PnL as % of entry
    max_favorable: float  # Best unrealized %
    max_adverse: float    # Worst unrealized %
    outcome: str         # "WIN", "LOSS", "EXPIRED"
    date: str            # YYYY-MM-DD
```

### Layer E Computation

```
Layer E = win_rate × 100, clamped to [20, 80]
```
- Returns 50 if fewer than 5 historical matches (`MIN_MATCHES_FOR_SCORE = 5`)
- Cold start = 50 (neutral prior)
- A strategy with 70% historical win rate → Layer E = 70 → adds 7 points to conviction

### EOD Recording

`conviction_engine.record_eod_outcomes(trade_records)` — called before `reset_daily()`. For every signal on the watchboard, builds a fingerprint and records the outcome.

---

## 15. Database

**Engine:** SQLite at `voltedgeai.db` via SQLAlchemy 2.0.

**Session:** `SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)`

**Async writes:** `DatabaseWriter` in `src/db/db_writer.py` — bounded queue (max 500 items), single daemon thread, 3 retry attempts with backoff. Ensures trade records are never silently dropped.

### Tables

| Table | Model Class | Purpose |
|-------|-------------|---------|
| `juror_signals` | `JurorSignal` | Juror classification outputs |
| `daily_performance_snapshots` | `DailyPerformanceSnapshot` | Daily OHLCV + TA per symbol |
| `fundamental_universe` | `FundamentalUniverse` | Stock universe with fundamentals |
| `decision_records` | `DecisionRecord` | Sniper/Decision engine decisions |
| `trade_records` | `TradeRecord` | Closed trade P&L records |

### TradeRecord Schema

```sql
trade_records:
  symbol TEXT
  direction TEXT    -- LONG, SHORT
  qty INTEGER
  entry_price REAL
  exit_price REAL
  pnl REAL
  entry_time DATETIME
  exit_time DATETIME
  mode TEXT        -- INTRADAY, SWING
  strategy TEXT    -- e.g., ANTIGRAVITY, HYDRA, VIPER
  exit_reason TEXT -- TIME_EXIT, STOP_LOSS, TARGET
```

### HistoryStore (separate SQLite)

**File:** `src/data_ingestion/market_history.py`

Separate SQLite at `data/history.db` for OHLCV caching:

```sql
ohlcv:
  symbol TEXT
  interval TEXT
  timestamp TEXT
  open, high, low, close, volume
  PRIMARY KEY (symbol, interval, timestamp)
```

---

## 16. Known Bugs and Status

As of 2026-04-03:

### Active Bugs (from CLAUDE.md)

| # | Bug | Impact | Location |
|---|-----|--------|---------|
| 1 | `SlotManager` missing `.used` attribute | Grok optimizer crashes every cycle | `src/strategies/slot_manager.py` |
| 2 | Pre-market brief fires at 06:00 UTC (11:30 IST) | Morning brief is 2.5h late | `src/runner.py` scheduled time vs `src/reports/pre_market_brief.py` |
| 3 | Email not received for pre-market brief | Silent SMTP failure | `src/reports/email_sender.py` or `src/reports/pre_market_brief.py` |

### Operational Status (from 2026-04-03 reports)

| Component | Status | Evidence |
|-----------|--------|---------|
| Zerodha Kite token | EXPIRED / INCORRECT | All Kite API calls failing with auth error |
| Nifty fetch | FAILING | `Incorrect api_key or access_token` in post-market |
| VIX fetch | FAILING | Same auth error |
| Sector index fetch | FAILING | Same auth error |
| Pre-market brief | FAILED or skipped | Post-market shows no morning predictions |
| VIPER scan | 0/0 scans | Kite data unavailable |
| Trades today | 0 | No signals → no trades |
| Email config | ENABLED | Email sender configured correctly |
| Mid-session pulse | GENERATED | `2026-04-03_mid_session.md` exists |

### Root Cause

The Zerodha access token is expired. The daily auto-login (`auto_refresh_access_token()`) either did not run or failed silently. All Kite-dependent data feeds (Nifty LTP, VIX, sector indices, WebSocket ticks, momentum scanner) are offline until a valid token is set.

**Fix:** manually refresh `ZERODHA_ACCESS_TOKEN` in `.env` and restart the service, or investigate why auto-login failed (check `journalctl -u voltedge.service -n 200`).

### Known Proxy / Limitations

- `volume_ratio` in VIPER = `abs(pct_change) / 2` — NOT actual relative volume. All VIPER volume-based rules (COIL exhaust, GAP_AND_TRAP thresholds) operate on this price-derived proxy.
- Nifty 5-min direction is a heuristic from LTP delta between cycles, not actual 5-min candle data.
- F&O expiry factor and sector concentration are static heuristics, not real-time.

---

## 17. Deployment and Operations Runbook

### Service Management

```bash
# Check status
sudo systemctl status voltedge.service

# View live logs (last 100 lines, follow)
journalctl -u voltedge.service -n 100 -f

# Start / stop / restart
sudo systemctl start voltedge.service
sudo systemctl stop voltedge.service
sudo systemctl restart voltedge.service

# Enable on boot
sudo systemctl enable voltedge.service
```

### Check Kite Token Status

```bash
# Look for auth errors
journalctl -u voltedge.service -n 500 | grep -i "access_token\|api_key\|Incorrect"

# Check the token in .env
grep ZERODHA_ACCESS_TOKEN /home/mujtabasiddiqui/VoltEdgeAI/.env
```

### Manual Token Refresh

```bash
cd /home/mujtabasiddiqui/VoltEdgeAI
source .venv/bin/activate
python -c "from src.tools.auto_login import auto_refresh_access_token; t = auto_refresh_access_token(); print('Token:', t)"
```

### Check Email Config

```bash
# Verify email is enabled and configured
journalctl -u voltedge.service | grep -i "email\|smtp"

# Check from runner banner
journalctl -u voltedge.service | grep "Email:"
```

### Check Recent Reports

```bash
ls -lt /home/mujtabasiddiqui/VoltEdgeAI/logs/daily_reports/
cat /home/mujtabasiddiqui/VoltEdgeAI/logs/daily_reports/$(date +%Y-%m-%d)_post_market.md
```

### Manual Trade in DRY_RUN Mode

Set `VOLTEDGE_LIVE_MODE=0` in `.env` (default). The runner will log all buy/sell decisions with `DRY_RUN` prefix but place no real orders.

### Switch to LIVE Mode

```bash
# Edit .env
sed -i 's/VOLTEDGE_LIVE_MODE=0/VOLTEDGE_LIVE_MODE=1/' .env
# Restart service
sudo systemctl restart voltedge.service
# Verify in logs
journalctl -u voltedge.service -n 20 | grep "LIVE_MODE"
```

**WARNING:** Switching to LIVE MODE with an expired Kite token will result in failed orders. Always verify token is valid before enabling live mode.

### Monitoring Key Metrics

```bash
# Conviction engine signals
journalctl -u voltedge.service | grep "\[ConvEng\]"

# Trade executions
journalctl -u voltedge.service | grep "DRY_RUN\|LIVE BUY\|LIVE SELL"

# Market phase transitions
journalctl -u voltedge.service | grep "\[Phase\]"

# SlotManager allocations
journalctl -u voltedge.service | grep "\[SlotManager\]"

# Macro risk tier
journalctl -u voltedge.service | grep "\[MacroRisk\]"

# Pre-market intelligence
journalctl -u voltedge.service | grep "\[PreMkt\]"
```

---

## 18. Going Live Checklist

Before switching `VOLTEDGE_LIVE_MODE=1`:

### Infrastructure
- [ ] GCP VM is running and voltedge.service is active
- [ ] `journalctl` shows no Python errors or import failures in last 30 minutes
- [ ] Disk has at least 5GB free (`df -h`)

### Zerodha / Kite
- [ ] Fresh access token generated and in `.env`
- [ ] Token verified: `kite.profile()` returns user data without error
- [ ] WebSocket connects and receives ticks for at least one symbol
- [ ] Instruments CSV (`data/zerodha_instruments.csv`) is present and non-empty
- [ ] Test DRY_RUN shows correct symbol token mapping

### Data Feeds
- [ ] Nifty LTP fetch succeeds (no auth error in logs)
- [ ] India VIX fetch succeeds
- [ ] Pre-Market Intelligence produces a composite score at 08:30
- [ ] HYDRA scan at 09:00 returns at least 1 event
- [ ] VIPER scan at 09:30 returns top movers
- [ ] Momentum scanner returns gainers/losers list

### Risk Configuration
- [ ] `VOLTEDGE_PER_TRADE_CAPITAL` is set appropriately (recommend start with ₹5,000-₹10,000)
- [ ] `VOLTEDGE_MAX_DAILY_LOSS` is set (recommend ₹2,500 for start)
- [ ] `VOLTEDGE_MAX_TRADES_PER_DAY` is set (recommend 3 for start)
- [ ] `VOLTEDGE_INTRADAY_EXIT_TIME=15:20` confirmed

### Emails
- [ ] Mid-session pulse email arrives at 12:00 IST
- [ ] Post-market report email arrives after 15:40 IST
- [ ] Both show correct data (not "no signals" or "no trades" every day)

### Broker
- [ ] Test order placed in paper trading mode via Kite developer console
- [ ] `place_equity_order()` returns success for a trivial 1-share market order
- [ ] MIS (intraday margin) product type confirmed in account settings

### Legal / Regulatory
- [ ] SEBI Algo Trading registration (if required for automated systems)
- [ ] Zerodha API usage agreement reviewed

---

## 19. Future Roadmap

Based on patterns observed in the codebase (comments, TODO markers, architecture gaps):

### Near-Term (Bugs to Fix)
- Fix pre-market brief time: change scheduled time from 06:00 UTC to 03:30 UTC
- Fix email silent failure: add explicit `smtplib` exception logging to `email_sender.py`
- Fix SlotManager `.used` attribute causing Grok optimizer crash
- Fix auto-login: verify TOTP and password flow, add explicit failure notification

### Short-Term (1-4 Weeks)
- Replace VIPER's price-derived `volume_ratio` proxy with actual intraday volume from BarBuilder
- Add Nifty 5-min candle direction from BarBuilder instead of LTP heuristic
- Implement real-time A/D ratio (currently from nsepython which may lag)
- Add Slack/Telegram alerts as secondary notification channel
- Implement MFE (Max Favorable Excursion) and MAE (Max Adverse Excursion) tracking for PatternDB Layer E

### Medium-Term (1-3 Months)
- Add 3rd strategy head (e.g., COIL live mode once historical performance validates it)
- Implement swing trade mode (CNC orders, overnight positions)
- Add F&O strategy head for options writing (capital-efficient)
- Database migration to PostgreSQL for concurrent multi-process access
- Add Prometheus metrics endpoint for operational monitoring

### Long-Term (3+ Months)
- Backtesting framework using `data/history.db` OHLCV cache
- ML-based Layer E using gradient boosting over the full feature space
- Multi-account support (family office, multiple clients)
- Real-time volatility surface for dynamic position sizing
- Integration with NSE's official co-location for ultra-low latency

---

*Document generated from direct source-code analysis of all 60+ Python modules in the VoltEdgeAI codebase.*
