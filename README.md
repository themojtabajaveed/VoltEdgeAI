# VoltEdgeAI

**Autonomous Intraday Trading Engine for Indian Markets (NSE) — v3 (TA Interpretation Layer)**

VoltEdgeAI is a fully automated, AI-powered intraday trading system that runs 24/7 on a VM. It handles everything from pre-market intelligence gathering to live trade execution, position management, and post-market analysis — with zero manual intervention.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        VoltEdgeAI Runner                        │
│                     (src/runner.py — 24/7)                      │
├─────────┬──────────┬──────────┬──────────┬─────────┬───────────┤
│ 06:00   │ 08:30    │ 09:15    │ 16:00    │ 18:00   │ 18:01     │
│ Morning │ Pre-Mkt  │ Trading  │ EOD      │ Market  │ Feedback  │
│ Brief   │ Oracle   │ Loop     │ Autopsy  │ Chron.  │ Loop      │
├─────────┴──────────┴──────────┴──────────┴─────────┴───────────┤
│                                                                 │
│  DATA LAYER           INTELLIGENCE        EXECUTION             │
│  ┌──────────┐        ┌──────────┐        ┌──────────────┐      │
│  │ Kite WS  │───────▶│ Stock    │───────▶│ Trade        │      │
│  │ (Live)   │        │ Discovery│        │ Executor     │      │
│  ├──────────┤        ├──────────┤        ├──────────────┤      │
│  │ Kite API │───────▶│ Technical│───────▶│ Exit Engine  │      │
│  │ (Hist.)  │        │ Scorer   │        │ (SL/TP/TSL)  │      │
│  ├──────────┤        ├──────────┤        ├──────────────┤      │
│  │ NewsData │───────▶│ Catalyst │        │ Position     │      │
│  │ (.io)    │        │ Analyzer │        │ Monitor      │      │
│  ├──────────┤        ├──────────┤        ├──────────────┤      │
│  │ Finnhub  │───────▶│ Macro    │        │ Risk Manager │      │
│  │ (Macro)  │        │ Context  │        │ (₹ caps)     │      │
│  └──────────┘        └──────────┘        └──────────────┘      │
│                                                                 │
│  AI ENGINE            REPORTS             LEARNING              │
│  ┌──────────┐        ┌──────────┐        ┌──────────────┐      │
│  │ Gemini   │───────▶│ Chronicle│        │ EOD Autopsy  │      │
│  │ 2.5 Flash│        │ (Daily)  │        │ (Patterns)   │      │
│  └──────────┘        ├──────────┤        ├──────────────┤      │
│                      │ Pre-Mkt  │        │ Pattern DB   │      │
│                      │ Brief    │        │ (JSON)       │      │
│                      └──────────┘        └──────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

---

 ## Module Reference

### Core Engine (`src/`)

| Module | Purpose |
|--------|---------|
| `runner.py` | Main 24/7 scheduler — orchestrates the entire daily lifecycle |
| `daily_decision_engine.py` | Morning regime classification (Bullish/Bearish/Sideways) |
| `db.py` | SQLAlchemy models — trades, decisions, snapshots, signals |
| `log_daily_performance.py` | EOD performance logger with full technicals |
| `trade_planner.py` | ATR-based position sizing and R:R calculation |

### Data Ingestion (`src/data_ingestion/`)

| Module | Data Source | Purpose |
|--------|-------------|---------|
| `market_live.py` | Kite WebSocket | Real-time tick data + bar building |
| `market_history.py` | Kite HTTP API | Historical OHLCV with SQLite cache |
| `instruments.py` | Kite CSV | Symbol ↔ Token mapping |
| `intraday_context.py` | Internal | In-memory bar store for fast lookups |
| `news_context.py` | NewsData.io | 7 specialized query methods (macro, sector, stock) |
| `finnhub_client.py` | Finnhub | Crude oil, gold, DXY, USD/INR quotes |
| `nse_scraper.py` | NSE India | FII/DII flows, bulk/block deals |
| `macro_context.py` | Composite | Macro risk signal (0.7x–1.15x score modifier) |
| `market_sentiment.py` | NSE India | Market breadth and advance-decline data |
| `pcr_tracker.py` | NSE India | Put-Call Ratio → contrarian signal |
| `corporate_actions.py` | NSE India | Dividend/split/bonus date guard |

### Sniper Engine (`src/sniper/`)

| Module | Purpose |
|--------|---------|
| `stock_discovery.py` | Momentum scanner — finds top N candidates by volume + change |
| `technical_scorer.py` | 0–100 scoring across 3 axes: Daily (30), Intraday (40), Momentum (30) |
| `momentum_scanner.py` | NSE top gainers/losers scraper |
| `antigravity.py` | VWAP z-score stretch detection (anti-chasing guard) |
| `antigravity_watcher.py` | Continuous z-score monitoring for open positions |
| `core.py` | V1 Sniper rules engine (breakout + veto logic) |
| `logger.py` | Decision logging to CSV |

### Trading Engine (`src/trading/`)

| Module | Purpose |
|--------|---------|
| `executor.py` | Buy/Sell/Short/Cover via Kite orders API |
| `exit_engine.py` | Stop-loss, trailing stop, take-profit, time-based exits, **RSI divergence trail tighten, MACD distribution 50% partial exit** |
| `positions.py` | Position book — tracks open positions, fills, P&L |
| `position_monitor.py` | Real-time alerts (drawdown, momentum loss, time warnings) |
| `sizing.py` | ATR-based position sizing with max capital checks |
| `atr.py` | Average True Range computation |
| `daily_risk_state.py` | Daily P&L tracker + trade counter |
| `trading_costs.py` | Brokerage + STT + GST cost estimation |
| `circuit_limits.py` | NSE circuit breaker detection |
| `time_of_day.py` | Market hours validation + session phase detection |
| `sector_guard.py` | Sector concentration limits |
| `orders.py` | Order data models |
| `depth_analyzer.py` | Level 2 order book analysis (liquidity checks) |

### Intelligence Layer (`src/juror/`)

| Module | Purpose |
|--------|---------|
| `catalyst_analyzer.py` | Gemini-powered news catalyst classification |
| `gemini_client.py` | Gemini API wrapper |

### Reports (`src/reports/`)

| Module | Schedule | Purpose |
|--------|----------|---------|
| `pre_market_brief.py` | 06:00 IST | Global intelligence brief (macro + sector + commodity) |
| `market_chronicle.py` | 18:00 IST | Full day review — predictions vs reality, trade analysis |
| `feedback_loop.py` | 18:01 IST | Scores morning predictions, generates lessons |
| `eod_autopsy.py` | 16:00 IST | Technical pattern analysis for top 20 movers |
| `daily_summary.py` | Legacy | Replaced by market_chronicle.py |

### Configuration (`src/config/`)

| Module | Purpose |
|--------|---------|
| `risk.py` | Risk parameters — max trades, daily loss cap, position sizing |
| `zerodha.py` | Kite API key + access token loader |

### Tools (`src/tools/`)

| Module | Purpose |
|--------|---------|
| `auto_login.py` | Headless Zerodha TOTP auto-login (generates fresh access tokens daily) |
| `kite_login_helper.py` | Manual login helper for initial setup |

---

## API Dependencies

| API | Purpose | Cost | Rate Limit |
|-----|---------|------|------------|
| **Zerodha KiteConnect** | Trading + Historical Data + WebSocket | ₹2,000/mo | Unlimited |
| **NewsData.io** | Macro/Sector/Stock news | Free tier | 200 credits/day (using ~28) |
| **Google Gemini 2.5 Flash** | AI analysis + pattern classification | Free tier | 1500 RPD |
| **Finnhub** | Crude oil, gold, DXY, USD/INR | Free tier | 60 calls/min |

---

## Environment Variables

```env
# Zerodha KiteConnect
ZERODHA_API_KEY=your_api_key
ZERODHA_API_SECRET=your_api_secret
ZERODHA_USER_ID=your_user_id
ZERODHA_PASSWORD=your_password
ZERODHA_TOTP_KEY=your_totp_secret
ZERODHA_ACCESS_TOKEN=auto_generated_daily

# AI & Data Providers
GEMINI_API_KEY=your_gemini_key
NEWDATA_API_KEY=your_newsdata_key
FINNHUB_API_KEY=your_finnhub_key

# Email Reports
REPORT_EMAIL_ENABLED=1
REPORT_EMAIL_TO=your@email.com
REPORT_SMTP_HOST=smtp.gmail.com
REPORT_SMTP_PORT=587
REPORT_SMTP_USER=your@gmail.com
REPORT_SMTP_PASSWORD=your_app_password
```

---

## Deployment

### Prerequisites
- Python 3.11+
- Zerodha Kite API subscription
- VM with 24/7 uptime (GCP/AWS/DigitalOcean)

### Setup
```bash
git clone https://github.com/your-repo/VoltEdgeAI.git
cd VoltEdgeAI
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in your API keys
```

### Run
```bash
# Local development
PYTHONPATH=. python src/runner.py

# Production (systemd)
sudo systemctl enable voltedge
sudo systemctl start voltedge
```

### systemd Service
```ini
# /etc/systemd/system/voltedge.service
[Unit]
Description=VoltEdgeAI Trading Engine
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/VoltEdgeAI
Environment=PYTHONPATH=/path/to/VoltEdgeAI
ExecStart=/path/to/venv/bin/python src/runner.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Daily Schedule

| Time (IST) | Event | Description |
|------------|-------|-------------|
| 06:00 | Morning Brief | Global intelligence + macro risk assessment |
| 08:30 | Pre-Market Oracle | Sector rotation + commodity + macro news queries |
| 09:15 | Market Open | Auto-login + WebSocket connect + instrument load |
| 09:15–09:30 | Discovery Scan | StockDiscovery identifies top N momentum candidates |
| 09:30–15:30 | Trading Loop | Technical scoring → Catalyst check → Trade execution |
| 15:30 | Market Close | Flatten all intraday positions |
| 16:00 | EOD Autopsy | Technical pattern analysis for top 20 movers |
| 18:00 | Market Chronicle | Full post-market report (Gemini-generated) |
| 18:01 | Feedback Loop | Score morning predictions + generate lessons |

---

## Risk Management

- **Per-trade risk**: 2% of capital (ATR-based stop-loss)
- **Max daily loss**: Configurable hard cap (₹)
- **Max trades/day**: Configurable limit
- **Max open positions**: Configurable limit
- **Sector concentration**: Max 2 positions per sector
- **Liquidity check**: Level 2 order book analysis (hard skip if illiquid)
- **Circuit breaker**: Auto-skip stocks near circuit limits
- **Time-of-day guard**: No new entries in last 30 min before close
- **ADX regime gate**: -5 hard penalty on conviction score when ADX < 20 (choppy market kills marginal trades)
- **RSI divergence exit**: Trailing stop tightened to entry on bearish RSI divergence (LONG positions only)
- **MACD distribution exit**: 50% partial exit when MACD bearish + histogram worsening + volume shrinking

---

## TA Interpretation Layer v3

The TA engine (shared across HYDRA + VIPER) was overhauled in 2026-03-30 to add 6 research-backed improvements:

| Feature | Where | What It Does |
|---------|---------|--------------|
| **Regime-Aware Weighting** | `hydra.py`, `viper_rules.py` | 5-regime classifier (TRENDING/BREAKOUT/RANGING/EXHAUSTION/NORMAL) dynamically scales per-component point caps |
| **Bollinger Band Squeeze** | `technical_body.py` → `hydra.py`, `viper_rules.py` | 20-period SMA ± 2σ bands; squeeze threshold 3.5%; awards breakout/squeeze bonus |
| **RSI Embedded Momentum** | `viper_rules.py` | RSI 70–85 = institutional "embedded" strength (+4 pts), not overbought signal |
| **RSI Divergence Exit** | `exit_engine.py` | Bearish RSI divergence (price high, RSI lower) tightens trailing stop to breakeven |
| **OBV Accumulation/Distribution** | `technical_body.py` → `viper_rules.py` | Cumulative OBV + divergence detectors; +3 pts bonus on hidden accumulation/distribution |
| **ADX Hard Gate** | `hydra.py`, `viper_rules.py` | ADX < 20 → -5 hard penalty; kills choppy-market entries more aggressively than soft multipliers |
| **MACD Distribution Exit** | `exit_engine.py` | Bearish MACD + worsening histogram + low volume → 50% PARTIAL_EXIT |

**All existing TA fields on `TechnicalSnapshot`:**

```
EMAs:      ema9, ema20, ema50, ema200, ema_alignment
RSI:       rsi14
MACD:      macd_line, macd_signal, macd_hist, macd_histogram, macd_histogram_prev, macd_crossover_bullish
VWAP:      vwap, above_vwap
ADX:       adx, plus_di, minus_di
ORB:       orb_high, orb_low, orb_breakout, orb_breakdown
Volume:    volume_avg, volume_current, volume_spike_ratio
ATR:       atr14
BB:        bb_upper, bb_lower, bb_mid, bb_width, bb_squeeze      ← v3
OBV:       obv, obv_bullish_div, obv_bearish_div                 ← v3
Price:     last_price, day_high, day_low, day_open
```

---

## License

Private — All rights reserved.
