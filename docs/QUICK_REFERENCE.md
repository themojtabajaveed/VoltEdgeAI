# VoltEdgeAI — Quick Reference

> Operator reference card for daily use.

---

## Emergency Commands

```bash
# Restart service (most common fix)
sudo systemctl restart voltedge.service

# Check service status
sudo systemctl status voltedge.service

# Follow live logs
journalctl -u voltedge.service -f

# Last 200 lines
journalctl -u voltedge.service -n 200

# Stop service immediately
sudo systemctl stop voltedge.service

# Check if process is alive
pgrep -f "main.py"
```

---

## Reading Key Log Lines

### Service Banner (startup)
```
--- VoltEdgeAI Automated Runner ---
Market Hours: 09:15:00 to 15:30:00 IST
VoltEdge LIVE_MODE = False (DRY_RUN only)
Max Trades / Day: 5
Max Daily Loss : ₹2,500.00
Per-Trade Risk : ₹10,000.00
Open Positions : 5
  📧 Email: ENABLED TO=mujtaba12cr@gmail.com SMTP=smtp.gmail.com:587 USER=set PASS=set
```
**What to check:** LIVE_MODE status, daily loss cap, per-trade capital, email config.

---

### Pre-Market Intelligence Score
```
[PreMkt] Score=62/100 → CLEAR | SPY=+0.8%(+7) QQQ δ=+0.3%(+0) Crude=-1.2%(+4) USD Strength (via EUR/USD)=flat(0) | Signals: 5/8 available
```
**Tiers:** EXTREME (<25) | RISK_OFF (25-39) | CAUTION (40-54) | CLEAR (55-69) | RISK_ON (70+)

---

### Macro Risk Tier
```
[MacroRisk] Tier-2 RISK_OFF | composite=42/100 | FII=-8500Cr | 7d_avg=-4200Cr | ratio=2.02x | LONG: -20pts (min 75) | SHORT: +10pts (min 60)
```
**What it means:** LONG trades need 75+ conviction, will have 20 pts subtracted. SHORT gets +10 bonus.

---

### HYDRA Scan
```
[09:00:15] 🔥 HYDRA: Pre-market event scan...
  HYDRA watchlist (3 events):
    SUNPHARMA [BUY] urgency=8/10 — Q3 results beat 12%, management guidance raised
    TATASTEEL [SHORT] urgency=7/10 — Global steel price drop, export ban rumour
```
**HYDRA scores:** Event (urgency × 7 = max 70) + TA (max 22) + Depth (max 10)

---

### VIPER Scan
```
[09:30:22] 🐍 VIPER: Initial top mover scan...
  VIPER watchlist (4 movers):
    BAJFINANCE [BUY] GAP_AND_RUN → STRIKE
    HCLTECH [SHORT] GAP_AND_TRAP → COIL
```
**Trade modes:** STRIKE = live trade eligible | COIL = dry-run only (never executed)

---

### Dragon Confluence
```
  🐉 DRAGON CONFLUENCE: ['SUNPHARMA'] found in BOTH HYDRA + VIPER!
```
**Effect:** +15 conviction bonus applied to SUNPHARMA signal.

---

### Conviction Engine Tick
```
[ConvEng] SUNPHARMA BUY | A=75 B=70 C=80 D=65 E=60 → conviction=73 | phase=trending_bull | prev=65 | Δ=+8
```

**Formula:** `(A×0.25) + (B×0.15) + (C×0.30) + (D×0.20) + (E×0.10)`
**Example above:** `(75×0.25)+(70×0.15)+(80×0.30)+(65×0.20)+(60×0.10) = 73.0`

**Layer meanings:**
- A: Market phase score (varies by phase and direction)
- B: Sector relative strength score
- C: Catalyst quality (FROZEN — set at signal creation)
- D: Live TA (VWAP, ORB, volume, EMA, MACD)
- E: Historical win rate from PatternDB (50 = neutral/cold start)

---

### Conviction Triggered
```
[ConvEng] *** TRIGGERED *** SUNPHARMA BUY conviction=73 >= 70 | waited 3 cycles
```
**Next:** signal is passed to risk stack. If all gates pass, trade is placed.

---

### SlotManager Allocation
```
[SlotManager] ALLOCATED: HYDRA -> BUY SUNPHARMA (conviction=73.0, capital=70%) [1/5]
```
**Capital:** 85+ → 100%, 70-84 → 70% of `per_trade_capital`

---

### SlotManager Rejection
```
[SlotManager] REJECTED: VIPER -> BUY HDFCBANK — Max open positions reached (5/5)
```

---

### Trade Execution (DRY_RUN)
```
DRY_RUN BUY: 47 × SUNPHARMA @ ~1842.50
```

### Trade Execution (LIVE)
```
[TradeExecutor] LIVE BUY: 47 × SUNPHARMA @ market | order_id=ABC123
```

---

### Exit Signal
```
[ExitEngine] SUNPHARMA: TRAILING_STOP triggered | ltp=1862.40 trail=1858.10 entry=1842.50 | strategy=HYDRA
```
**Exit reasons:** STOP_LOSS | TRAILING_STOP | TIME_EXIT | PARTIAL_1 | PARTIAL_2

---

### Market Phase Transition
```
[Phase] choppy → trending_bull at 09:47 IST | Nifty=+0.4% | A/D=0.68 | VIX=13.2
```

---

### Ban List
```
  🚫 F&O ban: ['DELTACORP', 'HINDCOPPER'] | T2T/BE: 234 symbols
```

---

### Daily Reset (midnight)
```
🔑 Attempting daily auto-login for new session: 2026-04-04...
✅ Token refreshed successfully for new day: NHDiPcd...
```

---

## Key Thresholds

| Parameter | Value | Source |
|-----------|-------|--------|
| Conviction threshold to trade | 70 | `src/strategies/slot_manager.py:CONVICTION_THRESHOLD` |
| Confluence bonus | +15 pts | `src/strategies/slot_manager.py:CONFLUENCE_BONUS` |
| Capital at 85+ conviction | 100% | `SlotManager.get_capital_allocation()` |
| Capital at 70-84 conviction | 70% | `SlotManager.get_capital_allocation()` |
| HYDRA min event urgency (pre-mkt) | 6.0 | `src/strategies/hydra.py:HydraStrategy.scan()` |
| HYDRA min event urgency (mid-day) | 7.0 | `src/strategies/hydra.py:HydraStrategy.scan()` |
| Juror min confidence | 0.80 | `src/daily_decision_engine.py:MIN_JUROR_CONFIDENCE` |
| Signal max age | 4 hours | `src/trading/conviction_engine.py:SIGNAL_MAX_AGE_HOURS` |
| Signal entry cutoff | 14:30 IST | `src/trading/conviction_engine.py:SIGNAL_EXPIRY_TIME` |
| Hard stop loss | 2.5% | `RiskConfig.intraday_stop_pct` |
| Force exit time | 15:20 IST | `RiskConfig.intraday_exit_time` |
| Max sector concentration | 2 positions | `src/trading/sector_guard.py` |
| Max open positions | 5 | `SlotManager.MAX_OPEN_POSITIONS` |
| ATR trail: Settle (0-15 min) | 2.0× ATR | `src/trading/exit_engine.py:PHASE_SETTLE_ATR` |
| ATR trail: Confirm (15-45 min) | 1.5× ATR | `src/trading/exit_engine.py:PHASE_CONFIRM_ATR` |
| ATR trail: Lock (profit > 1.5R) | 1.0× ATR | `src/trading/exit_engine.py:PHASE_LOCK_ATR` |
| ATR trail: Accelerate (> 2.5R) | 0.75× ATR | `src/trading/exit_engine.py:PHASE_ACCELERATE_ATR` |
| Partial 1 exit | 50% at 1.5R | `src/trading/exit_engine.py:PARTIAL_1_R` |
| Partial 2 exit | 25% at 2.5R | `src/trading/exit_engine.py:PARTIAL_2_R` |
| Min trade viability | 3× breakeven cost | `src/trading/trading_costs.py:is_trade_viable()` |
| Grok daily budget | 10 calls | `src/llm/grok_client.py:GROK_DAILY_BUDGET` |
| Groq daily budget | 14,400 calls | Groq free tier |
| Macro context refresh | 90 minutes | `src/data_ingestion/macro_context.py:REFRESH_INTERVAL_SECONDS` |
| F&O ban list refresh | 08:00 IST daily | `src/runner.py:BAN_LIST_TIME` |
| Pre-market job time | 08:30 IST | `src/runner.py:PREMARKET_TIME` |

---

## Daily Ops Checklist

### Pre-Market (08:00-09:15 IST)

- [ ] Service running: `sudo systemctl status voltedge.service`
- [ ] No auth errors: `journalctl -u voltedge.service -n 50 | grep -i "error\|token"`
- [ ] Ban list refreshed: look for `🚫` in logs around 08:00
- [ ] Pre-market intelligence score computed: look for `[PreMkt] Score=` around 08:30
- [ ] Grok morning strategist called: look for `🧠 Grok Morning Strategist` around 08:30

### Market Open (09:00-09:30 IST)

- [ ] HYDRA scan completed: look for `🔥 HYDRA:` at 09:00
- [ ] VIPER scan completed: look for `🐍 VIPER:` at 09:30
- [ ] Momentum scanner returned movers: look for `Scanner → LONG:` at 09:30
- [ ] ConvictionEngine watchboard has signals: `[ConvEng]` log lines appearing every cycle

### Intraday Monitoring

- [ ] Watch for `*** TRIGGERED ***` in ConvictionEngine logs → trade is about to be placed
- [ ] Watch for `ALLOCATED` in SlotManager logs → slot consumed
- [ ] Watch for `DRY_RUN BUY/SELL` → trade placed (dry-run)
- [ ] Watch for phase transitions: `[Phase] X → Y` in logs
- [ ] Mid-session pulse email received at 12:00 IST

### Post-Market (15:30+ IST)

- [ ] All positions exited (TIME_EXIT at 15:20)
- [ ] Post-market report email received
- [ ] `data/pattern_db.json` updated (Layer E learning)
- [ ] `data/daily_regime.json` updated for tomorrow

---

## Log Search Shortcuts

```bash
# Today's activity summary
journalctl -u voltedge.service --since="today" | grep -E "TRIGGERED|ALLOCATED|DRY_RUN|LIVE BUY|LIVE SELL"

# Conviction engine signals
journalctl -u voltedge.service | grep "\[ConvEng\]" | tail -50

# Market phase history today
journalctl -u voltedge.service --since="today" | grep "\[Phase\].*→"

# Auth errors (token issues)
journalctl -u voltedge.service | grep -i "incorrect.*api_key\|access_token"

# Any CRITICAL errors
journalctl -u voltedge.service | grep "CRITICAL\|FATAL"

# Email send results
journalctl -u voltedge.service | grep "\[Email\]"

# Grok calls
journalctl -u voltedge.service | grep "\[Grok\]"
```

---

## Key File Locations

| File | Purpose |
|------|---------|
| `/home/mujtabasiddiqui/VoltEdgeAI/.env` | All secrets and config |
| `/home/mujtabasiddiqui/VoltEdgeAI/data/daily_regime.json` | Today's macro regime |
| `/home/mujtabasiddiqui/VoltEdgeAI/data/pattern_db.json` | Layer E learning data |
| `/home/mujtabasiddiqui/VoltEdgeAI/data/fii_history.json` | 30-day FII flow history |
| `/home/mujtabasiddiqui/VoltEdgeAI/data/prediction_log.json` | Morning predictions + scores |
| `/home/mujtabasiddiqui/VoltEdgeAI/voltedgeai.db` | Main SQLite database |
| `/home/mujtabasiddiqui/VoltEdgeAI/logs/daily_reports/` | All daily report .md files |
| `/tmp/voltedge_logs/runner.log` | Runner log file |
| `/etc/systemd/system/voltedge.service` | systemd service definition |

---

## Conviction Engine Layer A Quick Reference

| Phase | BUY base | SHORT base | Typical scenario |
|-------|----------|------------|------------------|
| PANIC | 10 | 85 | Gap-down > 1.5%, VIX > 16, first 30 min |
| STABILISATION | 35 | 55 | Was panic/bear, now flat |
| RECOVERY | 65 | 30 | Bouncing from lows, A/D improving |
| TRENDING_BULL | 85 | 15 | Nifty +0.3%+, UP direction, A/D > 0.6 |
| TRENDING_BEAR | 15 | 80 | Nifty -0.3%-, DOWN direction, A/D < 0.4 |
| CHOPPY | 45 | 45 | Default — mixed or no trend |

---

## Trade Cost Estimate (Per Round-Trip)

At ₹10,000 capital, ₹500 stock price, 20 shares:

| Charge | Amount |
|--------|--------|
| Brokerage (buy) | ₹20.00 |
| Brokerage (sell) | ₹20.00 |
| STT (sell side) | ₹2.50 |
| NSE Exchange | ₹0.69 |
| GST | ₹7.32 |
| SEBI | ₹0.02 |
| Stamp Duty | ₹0.30 |
| **Total round-trip** | **~₹50** |
| Breakeven move | ~0.25% |
| Min viable move (3×) | ~0.75% |
