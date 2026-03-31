# CLAUDE.md — VoltEdgeAI Context File

> **Last Updated:** 2026-03-30 (v3 — TA Interpretation Layer: regime weights, BB, OBV, MACD exit, ADX gate, RSI divergence)
> **Project Phase:** Mid-Development (Dragon Architecture v1 complete, TA Interpretation Layer v3 deployed)
> **Status:** HYDRA + VIPER strategies wired, TA signals fully hardened, concurrency overhauled, deployed on VM

---

## 1. Project Identity

**VoltEdgeAI** is a fully autonomous, AI-powered intraday trading engine targeting the Indian equity market (NSE). It runs 24/7 on a VM with zero manual intervention — handling everything from pre-market intelligence gathering to live trade execution, position management, and post-market analysis.

**Core Philosophy:** Event-driven + momentum-driven dual-strategy architecture ("Dragon Architecture") with multi-LLM intelligence layer and layered risk management.

---

## 2. Tech Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| **Language** | Python | 3.11+ |
| **Runtime** | CPython on Linux VM (GCP/AWS) | — |
| **Database** | SQLite via SQLAlchemy ORM | SQLAlchemy 2.0.48 |
| **Broker API** | Zerodha KiteConnect (REST + WebSocket) | kiteconnect 5.0.1 |
| **LLM — Tier 1 (High-stakes)** | xAI Grok 4 via OpenAI-compatible API | openai ≥1.40.0 |
| **LLM — Tier 2 (Fast classification)** | Groq Llama-3.3-70B | groq ≥0.12.0 |
| **LLM — Tier 3 (Analysis/Reports)** | Google Gemini 2.5 Flash | google-genai 1.66.0 |
| **Technical Analysis** | pandas + numpy + scipy + ta-lib wrapper (`ta`) | pandas 3.0.1, numpy 2.4.2, scipy 1.17.1, ta 0.11.0 |
| **Data — News** | NewsData.io REST API | via requests |
| **Data — Macro** | Finnhub REST API | via requests |
| **Data — NSE Scraping** | jugaad-data, nsepython, nsepy, BeautifulSoup | jugaad-data 0.29, nsepython 2.97 |
| **HTTP Client** | requests + httpx | requests 2.32.5, httpx 0.28.1 |
| **Config** | python-dotenv (.env file) | python-dotenv 1.2.2 |
| **Validation** | Pydantic (used in dependencies, not yet project-wide) | pydantic 2.12.5 |
| **Auth** | pyotp (TOTP auto-login) | pyotp ≥2.9.0 |
| **Retry Logic** | tenacity | tenacity 9.1.4 |
| **WebSocket** | Twisted + autobahn (via KiteConnect), websockets | Twisted 25.5.0, websockets 16.0 |
| **Deployment** | systemd service on Linux VM | — |
| **Logging** | Python stdlib `logging` → file + stdout | — |

---

## 3. Architecture Overview

### Dragon Architecture (Dual-Head Strategy System)

```
                    ┌──────────────────────┐
                    │   VoltEdge Runner     │
                    │   (src/runner.py)     │
                    │   24/7 event loop     │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼────────┐  ┌───▼───┐  ┌─────────▼────────┐
     │  🔥 HYDRA       │  │ Slot  │  │  🐍 VIPER        │
     │  Event-Driven   │  │Manager│  │  Momentum         │
     │  Catalyst Head  │  │(共有)  │  │  Top-Mover Head   │
     └────────┬────────┘  └───┬───┘  └─────────┬─────────┘
              │               │                 │
              └───────┬───────┘─────────┬───────┘
                      │                 │
             ┌────────▼────────┐ ┌──────▼────────┐
             │ TechnicalBody   │ │ SlotManager   │
             │ (shared TA)     │ │ (trade budget)│
             └────────┬────────┘ └───────────────┘
                      │
        ┌─────────────┼──────────────┐
        │             │              │
   ┌────▼────┐  ┌─────▼─────┐  ┌────▼─────┐
   │ Groq    │  │ Grok 4    │  │ Gemini   │
   │ (fast)  │  │ (hi-conv) │  │ (reports)│
   └─────────┘  └───────────┘  └──────────┘
```

### Concurrency Architecture (3 threads + main)

```
  Kite WebSocket (Twisted reactor thread)
      │  _on_ticks() — O(1), no heavy work
      │
      ├──→ _last_ticks dict (instant lookups for ExitEngine)
      │
      └──→ SimpleQueue ──→ BarBuilderThread (daemon)
                              │  drains tick queue, calls bar_builder.on_tick()
                              │  latency: <1ms (was ~1000ms with polling)

  ExitMonitorThread (daemon, 1s interval)
      │  exit_engine.tick() → reads PositionBook (RLock)
      │  signals → SimpleQueue
      │
      └──→ Main runner drains queue each cycle → executor

  DatabaseWriterThread (daemon, queue-backed)
      │  main thread enqueues trade records (non-blocking)
      │  writer thread drains with 3 retries + loud failure logging
      └──→ SQLite (via SQLAlchemy)

  Thread Safety:
    PositionBook  → threading.RLock on all mutations
    DailyRiskState → threading.RLock on P&L fields (Decimal internally)
    SlotManager   → release() called on full position close
```

### Strategy Heads

| Head | File | Purpose | Trade Mode |
|------|------|---------|------------|
| **HYDRA** | `src/strategies/hydra.py` | Event-driven catalyst hunting (VWAP gate, base conviction scoring) | LIVE |
| **VIPER** | `src/strategies/viper.py` | Top mover momentum (classification, base scoring) | STRIKE=LIVE, COIL=DRY-RUN |

> **v2 change**: Strategy heads no longer call Grok directly. They produce
> base conviction scores (event + TA + depth + context). The runner's Grok
> Portfolio Orchestrator reviews top candidates from BOTH heads at key milestones.

### Shared Infrastructure

| Component | File | Role |
|-----------|------|------|
| **StrategyHead** (ABC) | `src/strategies/base.py` | Abstract base class for all strategy heads |
| **TechnicalBody** | `src/strategies/technical_body.py` | Pure-math TA engine (EMA, RSI, MACD, VWAP, ADX, ORB, ATR, BB, OBV) — v3 |
| **SlotManager** | `src/strategies/slot_manager.py` | Global trade budget, symbol locking, confluence detection, slot release |
| **MoveClassifier** | `src/strategies/move_classifier.py` | Classifies movers into 6 types (GAP_AND_GO, GRADUAL_RUNNER, etc.) |
| **ViperRules** | `src/strategies/viper_rules.py` | VIPER-specific TA rules with regime weights, OBV bonus, RSI embedded, ADX gate |
| **ConvictionScore** | `src/strategies/base.py` | 0-100 scoring: event(70) + TA(22/25) + depth(10) + context(10) + LLM(weighted) |
| **ExitMonitorThread** | `src/trading/exit_monitor.py` | Dedicated 1s exit detection thread (decoupled from 60s main loop) |
| **DatabaseWriter** | `src/db/db_writer.py` | Queue-backed async SQLite writer with retry logic |

### LLM Tiering

| Tier | Model | Client File | Use Case | Budget |
|------|-------|-------------|----------|--------|
| **Tier 1** | Grok 4 (xAI) | `src/llm/grok_client.py` | Portfolio-level orchestrator (morning strategist, intraday optimizer, EOD review) | ~7 calls/day (~154/month) |
| **Tier 2** | Llama-3.3-70B (Groq) | `src/llm/groq_client.py` | Event urgency classification (~300ms) | 14,400 req/day |
| **Tier 3** | Gemini 2.5 Flash | `src/juror/gemini_client.py` | Catalyst analysis, report generation | 1,500 RPD |

#### Grok 4.20 Orchestrator Schedule (v2)

Inspired by Grok 4.20's Alpha Arena architecture. Instead of per-symbol "gate" calls,
Grok receives the FULL portfolio + all candidates from both HYDRA and VIPER in each call.

| Time (IST) | Call Type | What Grok Sees |
|---|---|---|
| 08:30 | Morning Strategist | Macro context + HYDRA events + risk budget → daily regime + ranked watchlist |
| 09:17 | Intraday Optimizer | Post-open gap assessment (spreads settled) |
| 09:30 | Intraday Optimizer | ORB complete — highest-probability entry window |
| 10:00 | Intraday Optimizer | First hour done — fades/reversals |
| 10:45 | Intraday Optimizer | Pre-lunch — last clean moves |
| 11:45 | Intraday Optimizer | Final assessment — manage positions before afternoon lull |
| 15:40 | EOD Review | Day's trades vs morning plan → learning notes |

**Key principle**: Grok PROPOSES, hard-coded risk DISPOSES. Every Grok output is
validated by SlotManager, DailyRiskState, and the full mechanical risk stack.
Strategies (HYDRA/VIPER) no longer call Grok directly.

---

## 4. Folder Structure

```
VoltEdgeAI/
├── main.py                          # CLI entry point (loads .env, calls runner)
├── requirements.txt                 # Pinned dependencies
├── .env                             # API keys (gitignored)
├── .gitignore
├── README.md
├── voltedgeai.db                    # SQLite database (gitignored)
│
├── src/
│   ├── __init__.py                  # Package marker
│   ├── runner.py                    # 🧠 MAIN: 24/7 scheduler + trading loop (~1135 lines)
│   ├── db.py                        # SQLAlchemy models (JurorSignal, TradeRecord, etc.)
│   ├── daily_decision_engine.py     # Morning regime classification
│   ├── trade_planner.py             # ATR-based position sizing + R:R calculation
│   ├── log_daily_performance.py     # EOD performance logging
│   ├── run_juror_nse_live.py        # Juror pipeline runner
│   │
│   ├── db/                          # Database Layer
│   │   ├── __init__.py              # Re-exports SessionLocal, TradeRecord from db.py
│   │   └── db_writer.py             # Queue-backed async DB writer (singleton, 3 retries)
│   │
│   ├── strategies/                  # 🐉 Dragon Architecture
│   │   ├── base.py                  # StrategyHead ABC, ConvictionScore, WatchlistEntry (+metadata field)
│   │   ├── hydra.py                 # 🔥 HYDRA: Event-driven strategy head (VWAP-distance gate)
│   │   ├── viper.py                 # 🐍 VIPER: Momentum strategy head (+_score_move_quality)
│   │   ├── viper_rules.py           # VIPER-specific TA rules (STRIKE + COIL, midday volume gate)
│   │   ├── move_classifier.py       # Top mover classification (6 MoveTypes)
│   │   ├── technical_body.py        # Shared TA computation (TechnicalSnapshot)
│   │   └── slot_manager.py          # Global trade budget + confluence + release()
│   │
│   ├── trading/                     # Execution & Risk Layer
│   │   ├── executor.py              # Buy/Sell/Short/Cover via Kite
│   │   ├── exit_engine.py           # SL, TSL, TP, time-based exits (volume-aware exhaustion)
│   │   ├── exit_monitor.py          # Dedicated 1s ExitMonitorThread (decoupled from main loop)
│   │   ├── positions.py             # PositionBook (RLock-protected, Decimal P&L)
│   │   ├── position_monitor.py      # Real-time alerts (drawdown, momentum loss)
│   │   ├── sizing.py                # ATR-based position sizing + regime filtering
│   │   ├── atr.py                   # ATR computation
│   │   ├── daily_risk_state.py      # Daily P&L (Decimal + RLock, thread-safe accumulation)
│   │   ├── trading_costs.py         # Brokerage + STT + GST (Decimal-precision)
│   │   ├── circuit_limits.py        # NSE circuit breaker detection
│   │   ├── time_of_day.py           # Market hours + session phase logic
│   │   ├── sector_guard.py          # Sector concentration limits
│   │   ├── orders.py                # Order data models
│   │   ├── depth_analyzer.py        # Level 2 order book analysis
│   │   └── execution_logger.py      # Trade execution CSV logging
│   │
│   ├── data_ingestion/              # Market Data Layer
│   │   ├── market_live.py           # Kite WebSocket (real-time ticks + bar builder)
│   │   ├── market_history.py        # Kite HTTP (historical OHLCV + SQLite cache)
│   │   ├── instruments.py           # Symbol ↔ Token mapping from Kite CSV
│   │   ├── intraday_context.py      # In-memory bar store
│   │   ├── news_context.py          # NewsData.io (7 specialized query methods)
│   │   ├── finnhub_client.py        # Crude oil, gold, DXY, USD/INR
│   │   ├── nse_scraper.py           # FII/DII flows, bulk/block deals
│   │   ├── macro_context.py         # Composite macro risk signal (0.7x–1.15x)
│   │   ├── market_sentiment.py      # Market breadth + advance-decline
│   │   ├── pcr_tracker.py           # Put-Call Ratio → contrarian signal
│   │   ├── corporate_actions.py     # Dividend/split/bonus guard
│   │   └── event_scanner.py         # Unified event aggregator (NSE + deals + news)
│   │
│   ├── sniper/                      # V1 Scoring Engine (still active alongside Dragon)
│   │   ├── stock_discovery.py       # Momentum scanner finds top N candidates
│   │   ├── technical_scorer.py      # 0-100 scoring (Daily 30 + Intraday 40 + Momentum 30)
│   │   ├── momentum_scanner.py      # NSE top gainers/losers scraper
│   │   ├── antigravity.py           # VWAP z-score stretch detection
│   │   ├── antigravity_watcher.py   # Continuous z-score monitoring
│   │   ├── core.py                  # V1 Sniper rules engine (breakout + veto)
│   │   └── logger.py                # Decision CSV logging
│   │
│   ├── juror/                       # Gemini Intelligence Layer
│   │   ├── catalyst_analyzer.py     # Gemini-powered news catalyst classification
│   │   └── gemini_client.py         # Gemini API wrapper
│   │
│   ├── llm/                         # Multi-LLM Client Layer
│   │   ├── __init__.py              # Package marker
│   │   ├── grok_client.py           # xAI Grok 4 (conviction scoring, watchlist ranking)
│   │   └── groq_client.py           # Groq Llama-3.3-70B (fast event classification)
│   │
│   ├── reports/                     # Reporting & Learning
│   │   ├── pre_market_brief.py      # 06:00 IST global intelligence brief
│   │   ├── market_chronicle.py      # 18:00 IST full day review
│   │   ├── feedback_loop.py         # 18:01 IST score predictions + generate lessons
│   │   ├── eod_autopsy.py           # 16:00 IST top 20 movers pattern analysis
│   │   ├── coil_reporter.py         # VIPER COIL dry-run performance tracker
│   │   └── daily_summary.py         # Legacy (replaced by market_chronicle.py)
│   │
│   ├── config/                      # Configuration
│   │   ├── risk.py                  # RiskConfig dataclass + env loader
│   │   └── zerodha.py               # Kite API key loader
│   │
│   ├── brokers/                     # Broker Abstraction
│   │   └── zerodha_client.py        # Zerodha-specific broker wrapper
│   │
│   ├── broker/                      # (empty — legacy placeholder)
│   │
│   ├── marketdata/                  # Market Data Abstraction
│   │   └── intraday.py              # Intraday data interface
│   │
│   ├── sources/                     # External Data Sources
│   │   ├── nse_announcements.py     # NSE corporate announcements fetcher
│   │   └── nse_prices.py            # NSE historical price data
│   │
│   └── tools/                       # Utilities
│       ├── auto_login.py            # Headless Zerodha TOTP auto-login
│       └── kite_login_helper.py     # Manual login helper
│
├── scripts/                         # Deployment Scripts
│   ├── install_service.sh           # systemd service installer
│   ├── setup_server.sh              # VM setup script
│   └── voltedge.service.example     # systemd unit template
│
├── data/                            # Persistent Data (gitignored except structure)
│   ├── zerodha_instruments.csv      # Kite instrument dump (~14MB)
│   ├── fundamentals.csv             # Fundamental universe
│   ├── prediction_log.json          # Morning prediction tracking
│   └── daily_regime.json            # Current market regime (auto-generated)
│
├── logs/                            # Runtime Logs (gitignored)
│   ├── runner.log                   # Main runner log
│   ├── executions.log               # Trade execution log
│   ├── sniper_decisions.csv         # Sniper decision audit trail
│   └── daily_reports/               # Generated market reports
│
└── .claude/                         # Claude AI context
    └── rules/                       # Style & logic enforcement rules
```

---

## 5. Naming Conventions & Coding Patterns

### File & Module Naming
- **snake_case** for all Python files: `market_live.py`, `exit_engine.py`, `slot_manager.py`
- Module names describe their domain: `{domain}_{function}.py` (e.g., `daily_risk_state.py`, `depth_analyzer.py`)
- Strategy files named after their codename: `hydra.py`, `viper.py`

### Class Naming
- **PascalCase** for all classes: `HydraStrategy`, `TechnicalBody`, `SlotManager`, `ConvictionScore`
- Strategy heads: `{Name}Strategy` inheriting from `StrategyHead` ABC
- Data containers: `@dataclass` (preferred over dicts for structured data)
- Enums: `MoveType`, `TradeMode` (uppercase values)

### Function & Variable Naming
- **snake_case** for functions: `compute_atr()`, `should_allow_new_entry()`, `get_hot_events()`
- Private methods prefixed with `_`: `_get_grok_conviction()`, `_classify_single()`
- Constants: **UPPER_SNAKE_CASE**: `MARKET_START`, `VIPER_RESCAN_TIMES`, `CONFLUENCE_BONUS`
- Boolean functions: `is_*`, `should_*`, `has_*`, `meets_*` prefixes

### Type Hints
- **Consistently used** throughout the codebase
- Return types on all public methods: `-> List[WatchlistEntry]`, `-> ConvictionScore`
- `Optional[]` for nullable: `Optional[datetime]`, `Optional[dict]`
- Tuples for multi-return: `-> tuple[bool, str]` (for allowed/reason pairs)

### Error Handling Pattern
- **Try-except at boundaries**: every external API call, file I/O, and LLM call is wrapped
- **Graceful degradation**: failures return safe defaults (e.g., `conviction=0`, empty list)
- **Logging + continue**: errors are logged but never crash the main loop
- Pattern: `try: ... except Exception as e: logging.error(f"...{e}"); <fallback>`

### Data Flow Pattern
- **Dataclass pipelines**: `MarketEvent → WatchlistEntry → ConvictionScore → TradeSlot`
- Scoring outputs are always `@dataclass` with `.to_dict()` methods
- Strategy heads return structured objects, never raw dicts

### Import Style
- Standard lib first, then third-party, then `src.*` imports
- Lazy imports inside functions for heavy/optional deps: `from src.llm.grok_client import ...`
- Runner has all imports at the top (except in-loop lazy imports for pandas)

### Configuration Pattern
- All runtime config via environment variables loaded through `python-dotenv`
- `RiskConfig` dataclass with `load_risk_config()` factory
- Env vars prefixed: `VOLTEDGE_*` (app config), API-specific keys use provider prefix

### Logging Pattern
- Module-level: `logger = logging.getLogger(__name__)`
- Runner uses both `print()` (formatted with timestamps + emoji) and `logging.info()`
- Strategy heads prefix logs with `[STRATEGY_NAME]`: `[HYDRA]`, `[VIPER]`, `[SlotManager]`

### CSS/Frontend
- **No frontend** — this is a pure backend Python system
- Reports are generated as Markdown files (emailed or saved to disk)

---

## 6. Key Design Decisions

### Conviction Scoring System (0-100)
Every trade decision flows through a unified conviction pipeline:
- **Event/Move Strength**: 0-70 (HYDRA) or 0-30 (VIPER `_score_move_quality()`)
- **Technical Confirmation**: 0-22 (HYDRA with VWAP-distance gate) or 0-25 (VIPER)
- **Order Book Depth**: 0-10
- **Context Bonus**: 0-10 (VIPER only — macro/sector/time-of-day)
- **LLM Conviction**: Weighted 30% of Grok score
- **Trade Threshold**: ≥ 70 to execute
- **Confluence Bonus**: +15 when both heads agree (conviction score only, no capital multiplier)

### Multi-LLM Strategy
- **Groq (fast, free)**: First-pass classification, runs on every event (~300ms)
- **Grok (expensive, smart)**: Final conviction, runs only on promising setups (budget: 25/day)
- **Gemini (free tier)**: Report generation, catalyst analysis, not in trade loop

### Concurrency & Thread Safety
- **3 daemon threads** + main runner thread (see Architecture diagram above)
- **RLock** on `PositionBook` (all mutations) and `DailyRiskState` (P&L fields) — mandatory because ExitMonitorThread writes positions from a separate thread
- **Push tick pipeline**: Kite `_on_ticks` → `SimpleQueue` → `BarBuilderThread` (sub-ms latency, replaces 1s polling)
- **ExitMonitorThread**: 1-second independent exit detection; signals queued, main thread executes orders
- **DatabaseWriter**: Bounded queue (500), single writer thread, 3 retries with backoff, NEVER silently drops records
- **Financial precision**: `decimal.Decimal` in `DailyRiskState`, `PositionBook._precise_pnl()`, and `trading_costs.py`; floats only at API boundary

### Risk Layers (Defense in Depth)
1. **Conviction threshold** (≥70 to trade)
2. **SlotManager** (max 5 open positions, symbol locking, auto-release on close)
3. **Daily loss cap** (configurable hard limit in ₹, Decimal-precision accumulation)
4. **ATR-based position sizing** (2.5% max stop loss)
5. **Trading costs viability check** (breakeven % vs expected move)
6. **Liquidity check** (illiquid = hard kill, skip)
7. **Circuit breaker guard** (skip near circuit limits)
8. **Sector concentration** (max 2 per sector)
9. **Time-of-day guard** (no new entries in last 30 min)
10. **F&O expiry factor** (reduced sizing on expiry days)
11. **Macro risk-off dampener** (0.7x-1.15x score modifier)
12. **PCR contrarian signal** (score modifier)
13. **HYDRA VWAP-distance gate** (prevents buying spike tops; 0pts if >1.5% above VWAP)
14. **Midday COIL volume gate** (11:30–13:30 IST: tighter threshold to avoid NSE lunch lull false signals)
15. **Exhaustion exit volume check** (retrace on low volume = hold; retrace on real volume = exit)

### COIL Mode (Dry-Run Reversal Trading)
VIPER classifies some moves as "OVEREXTENDED" → `TradeMode.COIL`. These are logged but never traded live. Weekly reports track hypothetical P&L to validate before enabling.

---

## 7. Daily Lifecycle (IST)

| Time | Event | Module |
|------|-------|--------|
| 06:00 | Morning Global Brief | `reports/pre_market_brief.py` |
| 08:30 | Pre-Market Oracle (news + macro sentiment) | `runner.py` inline |
| 09:00 | HYDRA event scan (since last close) | `strategies/hydra.py` |
| 09:15 | Market open → auto-login + WebSocket connect | `tools/auto_login.py` |
| 09:30 | Momentum scanner + VIPER initial scan | `sniper/momentum_scanner.py`, `strategies/viper.py` |
| 09:30–15:30 | Trading loop (15-min intervals): Score → Analyze → Execute | `runner.py` |
| 09:15–11:00 | HYDRA Grok re-ranking (every 30 min) | `llm/grok_client.py` |
| 10:00, 10:30, 11:00, 12:00 | VIPER re-scans | `strategies/viper.py` |
| 15:30 | Market close → flatten all positions | `trading/exit_engine.py` |
| 16:00 | EOD Autopsy (top 20 movers pattern analysis) | `reports/eod_autopsy.py` |
| 18:00 | Market Chronicle (full day review) | `reports/market_chronicle.py` |
| 18:01 | Feedback Loop (score morning predictions) | `reports/feedback_loop.py` |

---

## 8. Database Schema (SQLite)

| Table | Purpose |
|-------|---------|
| `juror_signals` | Gemini classification outputs (label, confidence, reason) |
| `daily_performance_snapshots` | EOD technicals for all tracked symbols |
| `fundamental_universe` | Fundamental stock screener data |
| `decision_records` | Trade decision audit log |
| `trade_records` | Closed trade log (symbol, direction, entry/exit, P&L, strategy) |

---

## 9. External API Dependencies

| API | Purpose | Auth Env Var | Rate Limit |
|-----|---------|-------------|------------|
| Zerodha KiteConnect | Trading + Historical + WebSocket | `ZERODHA_API_KEY`, `ZERODHA_API_SECRET` | Unlimited |
| xAI Grok 4 | High-conviction scoring | `GROK_API_KEY` or `XAI_API_KEY` | 25 calls/day (self-imposed) |
| Groq (Llama 3.3 70B) | Fast event classification | `GROQ_API_KEY` | 14,400 req/day |
| Google Gemini 2.5 Flash | Catalyst analysis + reports | `GEMINI_API_KEY` | 1,500 RPD |
| NewsData.io | News headlines (7 query types) | `NEWDATA_API_KEY` | 200 credits/day (~28 used) |
| Finnhub | Commodity + forex quotes | `FINNHUB_API_KEY` | 60 calls/min |

---

## 10. Progress Summary

### ✅ Completed
- Full 24/7 runner with scheduled lifecycle (06:00→18:01 daily flow)
- **Dragon Architecture** — HYDRA (event-driven) + VIPER (momentum) dual-head strategy system
- Unified conviction scoring (ConvictionScore dataclass, 0-100 pipeline)
- Multi-LLM integration (Grok 4 + Groq Llama-3.3-70B + Gemini 2.5 Flash)
- SlotManager with cross-head confluence detection (+15 bonus)
- **TechnicalBody v3** — full TA engine: EMA, RSI, MACD, VWAP, ADX, ORB, ATR, **Bollinger Bands, OBV, RSI divergence, MACD histogram prev**
- **TA Interpretation Layer v3** — regime-aware dynamic weighting, BB integration, OBV divergence scoring, RSI embedded momentum, ADX hard -5 penalty, RSI divergence exit, MACD distribution 50% partial exit
- MoveClassifier (6 move types: GAP_AND_GO, GRADUAL_RUNNER, SECTOR_WAVE, OVEREXTENDED, GAP_AND_TRAP, DEAD_CAT_BOUNCE)
- VIPER COIL dry-run mode with weekly performance reporting
- Full risk management stack (15+ layers)
- ATR-based position sizing with F&O expiry adjustments
- Real-time position monitoring with alerts
- Exit engine v3 (SL, TSL, TP, time-based, RSI divergence trail tighten, MACD distribution partial)
- Order book depth analysis (liquidity checks)
- Macro context integration (commodities, FII/DII, PCR)
- Pre-market brief + market chronicle + feedback loop reporting
- Auto-login via headless TOTP for Zerodha
- Deployed on VM with systemd

### 🔧 TA Interpretation Layer v3 (2026-03-30)

**Improvement A — Context-Aware Regime Weighting (hydra.py + viper_rules.py):**
- `_get_regime_weights()` classifies current market into 5 regimes: TRENDING / BREAKOUT / RANGING / EXHAUSTION / NORMAL
- Each regime shifts per-component scoring multipliers (volume, VWAP, EMA, ORB, ADX)
- TRENDING → EMA 1.3×; RANGING → EMA 0.7×, ORB 0.6×; BREAKOUT → Volume 1.5×, ORB 1.4×

**Improvement B — Bollinger Band Squeeze Integration (technical_body.py + hydra.py + viper_rules.py):**
- 5 new fields on `TechnicalSnapshot`: `bb_upper`, `bb_lower`, `bb_mid`, `bb_width`, `bb_squeeze`
- Cold-start: rolling 20-period SMA ± 2σ via Pandas; streaming: ring buffer of 20 closes
- HYDRA: +1 to +3 pts on BB position/squeeze; VIPER STRIKE: +2 to +4 pts

**Improvement C — RSI Overhaul (viper_rules.py + exit_engine.py):**
- VIPER STRIKE: RSI 70–85 → +4 pts "embedded institutional momentum" (NOT overbought)
- ExitEngine: bearish RSI divergence detected → tighten trailing stop to breakeven (warns only, no force-exit)
- Fires only when `breakeven_activated = True` to prevent premature exits

**Improvement D — On-Balance Volume / OBV (technical_body.py + viper_rules.py):**
- 3 new `TechnicalSnapshot` fields: `obv`, `obv_bullish_div`, `obv_bearish_div`
- `StreamingTechnicalState.obv_history`: ring buffer of 5 `(close, obv)` tuples; incremental: `obv ± volume`
- `detect_bullish_obv_divergence()` / `detect_bearish_obv_divergence()` static methods
- VIPER STRIKE: +3 pts for OBV accumulation divergence (BUY) or distribution divergence (SHORT)

**Improvement E — ADX Hard Regime Gate (hydra.py + viper_rules.py):**
- ADX < 20 was RANGING with soft multipliers; now triggers **-5 hard penalty** on raw score
- Kills marginal trades in choppy markets without rejecting rare high-conviction setups

**Improvement F — MACD Distribution Exit (exit_engine.py):**
- `_check_macd_distribution()`: LONG only; fires when `macd_hist < 0` AND worsening AND `volume_spike_ratio < 0.8`
- Emits `PARTIAL_EXIT` at 50% qty (not full exit); remaining 50% rides the trailing stop
- `_make_signal(qty_pct=)` updated to support partial quantity percentage

**Bug Fixed (2026-03-30):**
- **CRITICAL**: `TechnicalBody.update()` was a `@staticmethod` but called `cls.detect_bullish_obv_divergence()` / `cls.detect_bearish_obv_divergence()`. This would raise `NameError: name 'cls' is not defined` at runtime on every streaming bar tick. Fixed to `TechnicalBody.detect_*`.

### 🔧 Recently Audited & Fixed (2026-03-28)

**Strategy Audit Fixes:**
- **CRITICAL BUG**: Implemented missing `_score_move_quality()` in VIPER — method was called but never defined, causing `AttributeError` crash on every VIPER evaluation
- **BUG**: Added `metadata: dict` field to `WatchlistEntry` dataclass — VIPER was setting it dynamically, fragile under serialization
- **BUG**: Added `SlotManager.release()` method — closed positions were permanently locking slots, blocking all new trades after 5 exits
- **LOGIC**: HYDRA VWAP-distance gate — replaces binary `above_vwap` check with distance-based scoring to prevent buying at spike tops
- **LOGIC**: Midday COIL volume gate — 11:30–13:30 IST uses tighter volume threshold (0.3 vs 0.7) to avoid false exhaustion signals during NSE lunch lull
- **LOGIC**: Exhaustion exit volume check — requires dip volume >50% of rally volume before confirming exhaustion (prevents whipsaw exits during midday)
- **LOGIC**: Confluence double-counting fix — removed 1.5x capital multiplier from `get_capital_allocation()`; +15 conviction bonus is sufficient
- **DOC**: VIPER `volume_ratio` proxy warning — documented that it's derived from `abs(pct_change)/2`, not actual relative volume

**Concurrency Architecture (P0–P3):**
- **P0**: `PositionBook` + `DailyRiskState` → `threading.RLock` on all mutations (prevents data races with ExitMonitorThread)
- **P1**: Push tick pipeline — `SimpleQueue` replaces 1s polling loop in runner (sub-ms latency)
- **P2**: `ExitMonitorThread` — dedicated 1s daemon thread for exit detection, signals queued for main thread execution
- **P3-A**: `DatabaseWriter` singleton — queue-backed async SQLite writes with 3 retries, NEVER silently drops records
- **P3-B**: `decimal.Decimal` for all financial quantities — `DailyRiskState`, `PositionBook._precise_pnl()`, `trading_costs.py`

**Grok 4.20 Portfolio Orchestrator (v2, 2026-03-28):**
- **ARCH**: Replaced per-symbol `grok_conviction_analysis()` + `grok_watchlist_ranking()` with 3 portfolio-level functions: `grok_morning_strategist()`, `grok_portfolio_optimizer()`, `grok_eod_review()`
- **ARCH**: Strategy heads (HYDRA, VIPER) no longer call Grok — produce base conviction scores only. Runner handles all LLM orchestration centrally.
- **ARCH**: Orchestrator schedule aligned with NSE volatility: 08:30, 09:17, 09:30, 10:00, 10:45, 11:45 (no calls after 11:45 for new entries)
- **ARCH**: Global Grok call budget tracked in runner (not per-strategy), with `GROK_DAILY_BUDGET = 10` generous upper bound
- **ARCH**: Each call receives full portfolio state + candidates from BOTH heads + risk state + macro context
- **ARCH**: Fallback: if Grok is unavailable or returns invalid JSON, system continues with mechanical TA rules (no halt)

**Earlier Fixes:**
- **BUG-1**: Illiquid stocks hard-killed (conviction → 0) instead of negative depth scores
- **BUG-2**: HYDRA Grok trigger threshold lowered from 55→45 (now irrelevant — orchestrator replaces this)
- **BUG-6**: Grok `<think>` block stripping + robust JSON extraction with regex fallback

### 🚧 Immediate Next Steps
- **COIL → live**: Analyze COIL dry-run reports to determine if reversal trades should go live
- **True volume ratio**: Replace price-derived volume proxy in VIPER with actual Kite historical volume data
- **Position correlation**: Detect correlation between open positions to avoid concentration risk
- **Backtest harness**: Build a replay engine using stored OHLCV + event data to test strategy changes offline
- **Dashboard**: Consider a lightweight web dashboard for real-time monitoring (currently log-only)
- **Market-aware filters**: Stronger regime-based gating for choppy/sideways markets

---

## 11. Running the Project

```bash
# Local development
cd VoltEdgeAI
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Fill in API keys

# Run with custom settings
PYTHONPATH=. python main.py
# OR directly:
PYTHONPATH=. python src/runner.py

# Production (systemd on VM)
sudo systemctl enable voltedge
sudo systemctl start voltedge
```

### Key Environment Variables
```
VOLTEDGE_LIVE_MODE=0|1           # 0=dry-run, 1=live broker orders
VOLTEDGE_PER_TRADE_CAPITAL=5000  # ₹ per trade
VOLTEDGE_MAX_TRADES_PER_DAY=5    # Max simultaneous positions
VOLTEDGE_MAX_DAILY_LOSS=2500     # ₹ daily loss cap
```

---

## 12. Critical Invariants (Do Not Break)

1. **ConvictionScore ≥ 70 to trade** — this is the universal gate across all strategies
2. **SlotManager is the single source of truth** for trade budget and symbol locking; always call `release()` on full close
3. **COIL mode must NEVER execute live trades** — dry-run only until explicitly approved
4. **Grok budget: ~7 calls/day, max 10** — tracked globally in runner via `grok_call_count`, never per-strategy
5. **Runner must never crash** — every external call is wrapped in try-except with fallbacks
6. **IST timezone** — all time comparisons use `Asia/Kolkata`, never UTC
7. **Strategy heads are stateless per-day** — `reset_daily()` clears all watchlists at midnight
8. **Illiquid stocks = hard kill** — if depth analysis says illiquid, conviction → 0, no trade
9. **RLock before mutation** — `PositionBook` and `DailyRiskState` must ALWAYS acquire `_lock` before modifying state (ExitMonitorThread runs concurrently)
10. **Broker calls in main thread only** — ExitMonitorThread detects exits and queues signals; order execution happens in the main runner thread (never in daemon threads)
11. **DB writes via DatabaseWriter** — never use inline `SessionLocal()` in the trading loop; always use `db_writer.write_trade_record()`
12. **Decimal for money** — P&L accumulation, cost calculations, and position P&L use `decimal.Decimal`; plain `float` only at API boundaries
13. **Confluence = conviction bonus only** — +15 to conviction score, NO separate capital multiplier (prevents double-counting risk)
14. **Grok proposes, rules dispose** — every Grok output is validated by the mechanical risk stack (SlotManager, sector_guard, circuit_limits). The LLM cannot bypass hard-coded safety rails.
15. **Strategy heads don't call Grok** — HYDRA and VIPER produce base conviction scores. The runner's Grok Portfolio Orchestrator is the sole LLM integration point.
16. **Grok calls stop at 11:45** — no new-entry LLM calls after 11:45 IST. Afternoon is mechanical-only (ExitMonitorThread + trailing stops).
17. **BB guard: always check `bb_upper > 0`** before any BB scoring block — `bb_upper` defaults to `0.0` until 20 bars are observed. Scoring without this guard will award phantom points.
18. **OBV divergence = additive bonus, not gate** — OBV adds evidence but does not block a trade. A stock can trade without OBV divergence; the conviction threshold handles the gate.
19. **RSI divergence exit = trail tighten only** — `_check_rsi_divergence()` adjusts `trailing_stop_price` to entry; it never forces an immediate exit and only fires once per symbol per day (`_divergence_warned` set).
20. **MACD distribution = LONG only, 50% only** — `_check_macd_distribution()` emits a 50% PARTIAL_EXIT, not a full exit. SHORT distribution logic is explicitly deferred.
21. **`TechnicalBody.update()` static methods** — all helper calls inside `update()` must use `TechnicalBody.method_name()`, never `cls.method_name()` (it's a `@staticmethod`, `cls` is not in scope).
22. **Regime weights are per-call** — `_get_regime_weights()` is called fresh each evaluation. Never cache the regime dict between bars; regime can shift bar-to-bar.
23. **ADX hard penalty (-5) preserves philosophy** — it's a score deduction, not a hard block, so exceptional multi-indicator signal agreement can still overcome choppy market conditions.
