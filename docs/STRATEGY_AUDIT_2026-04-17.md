# VoltEdgeAI — Strategy Audit (2026-04-17)

Read-only system survey. No code changed, no recommendations issued. Each
strategy is described from the perspective of what it does today in the
running code, not what it is intended to become.

---

## Discovery

### Strategy classes & entry points found

| File | Entity | Kind |
|------|--------|------|
| `src/strategies/base.py` | `StrategyHead` (ABC) | Base class |
| `src/strategies/hydra.py` | `HydraStrategy` | Concrete StrategyHead |
| `src/strategies/viper.py` | `ViperStrategy` | Concrete StrategyHead |
| `src/strategies/dawn.py` | `DawnStrategy` + `execute_dawn_dryrun`, `update_dawn_dryrun`, `select_dawn_candidates` | Standalone strategy (NOT a StrategyHead) |
| `src/strategies/slot_manager.py` | `SlotManager` | Budget arbiter (not a strategy) |
| `src/strategies/technical_body.py` | `TechnicalBody`, `TechnicalSnapshot` | Shared TA engine |
| `src/strategies/viper_rules.py` | `ViperRules` | STRIKE/COIL TA confirmation rules |
| `src/strategies/move_classifier.py` | `MoveClassifier`, `MoveType`, `TradeMode` | VIPER classification helper |
| `src/sniper/stock_discovery.py` | `StockDiscovery` | Discovery/fusion layer |
| `src/sniper/technical_scorer.py` | `TechnicalScorer` | TA scorer used by discovery path |
| `src/sniper/momentum_scanner.py` | `fetch_top_movers()` | Movers fetcher |
| `src/sniper/antigravity.py` | `evaluate_symbol()`, `evaluate_antigravity()` | VWAP stretch / bounce check |
| `src/sniper/antigravity_watcher.py` | `AntigravityWatcher` | VWAP-bounce state machine |
| `src/sniper/core.py` | `evaluate_signal()` | Sniper v1 daily-bar rules (legacy) |
| `src/trading/conviction_engine.py` | `ConvictionEngine`, `ActiveSignal` | 5-layer dynamic conviction watchboard |

### Entry points in `runner.py`

- HYDRA: `HydraStrategy()` instantiated, `.scan()` at 09:00 IST (`HYDRA_SCAN_TIME = 08:15` constant but gated inside the 09:00 path), `.evaluate()` every loop while watchlist is populated.
- VIPER: `ViperStrategy()` instantiated, `.scan()` at 09:30 IST plus re-scans at 10:00, 10:30, 11:00, 12:00. `.evaluate()` every loop.
- DAWN: `DawnStrategy()` instantiated; `pre_market_scan()` at 08:30, `merge_brief_and_generate()` is called from the brief pipeline, `record_entries()` at 09:15-09:20, `manage_positions()` every 5 min, `close_all()` at 15:20. Also `execute_dawn_dryrun` / `update_dawn_dryrun` / `select_dawn_candidates` module-level functions.
- Sniper family: `AntigravityWatcher` instantiated, `StockDiscovery(top_n=10)`, `TechnicalScorer`. `fetch_top_movers()` is called at 09:30 and its output is *fed into both* VIPER and StockDiscovery. `watcher.tick()` runs in the main loop.
- `ConvictionEngine()` instantiated. Both HYDRA and VIPER add signals to it. Its `tick()` runs every intraday interval and produces `triggered` signals that flow into the execution path.

---

## 1. HYDRA — Event-Driven Catalyst Hunter

### What is it?
`src/strategies/hydra.py` → class `HydraStrategy(StrategyHead)`. Scans
corporate events and news, Groq-classifies urgency, confirms with TA,
and produces a base conviction score for the central ConvictionEngine
and Grok Portfolio Orchestrator.

### When does it run?
- `hydra.scan()` runs once per day, gated at 09:00 IST (`HYDRA_SCAN_TIME = dt_time(8, 15)` but the fire condition uses the 09:00 gate in runner).
- Intraday evaluation runs every runner loop tick (~60s) while watchlist exists and `trade_placed_today` is False and a slot is open.
- Mid-day incremental scan: `event_scanner.scan_new_events()` is called during the intraday loop when new hot events arrive.

### What data does it consume?
- `EventScanner` (corporate filings + news via `src/data_ingestion/event_scanner.py`).
- yfinance fast_info → `gap_pct`.
- `data/daily_regime.json` → sector momentum bias (BULLISH / BEARISH / NEUTRAL).
- Shared `TechnicalBody` snapshots (VWAP, ORB, EMAs, ADX, DI, RSI, volume ratio).
- Depth book via `DepthAnalysis` (passed in by runner).
- Groq classification of events (inside EventScanner).

### Logic
Scoring model:
1. Event strength: `urgency * 7` capped at 70.
2. TA confirmation via `HydraRules.confirms_event`. Regime-weighted
   volume / VWAP / EMA / ORB components, max 22 points. Regimes:
   TRENDING, BREAKOUT, RANGING, EXHAUSTION, NORMAL.
3. Depth: +5 strong bid/ask aligned, +3 wall detected, +2 if liquid.
   `signal == "illiquid"` → hard kill (total = 0).
4. No inline Grok call — LLM work is centralised in the orchestrator.

Decision rule: total ≥ 70 → candidate for trade; runner then checks slot
manager, time-of-day, risk stack, etc.

### Output
- `self.watchlist` (in-memory list, max 5).
- `data/hydra_watchlist.json` (`{generated_at, candidates:[…]}`).
- `ConvictionEngine.add_signal(...)` with `strategy="HYDRA"`.
- On live fire: orders through `place_order` / `slot_manager.allocate("HYDRA", …)`.
- Learning: `data/hydra_pattern_db.json` via `save_trade_result`.

### Consumers
- ConvictionEngine watchboard (Layer C frozen at creation).
- Grok Portfolio Orchestrator (`get_top_candidates`).
- Confluence detection in runner (intersection with VIPER watchlist
  triggers `slot_manager.register_confluence(...)`).
- DAWN (`select_dawn_candidates` reads `data/hydra_watchlist.json`).

### Health
- Instantiated and wired in runner.
- EOD report references HYDRA via ConvictionEngine signals (none logged
  in `2026-04-16_post_market.md` — all 15 signals that day were VIPER or
  VIPER-COIL).
- `data/hydra_watchlist.json` exists (untracked).

### Missing / broken
- The 08:15 constant in runner does not match the actual 09:00 gate —
  this is a latent inconsistency, not a crash.
- Strategy relies on EventScanner returning classified events; if
  upstream fails, watchlist becomes empty silently (caught and logged).
- No TODO/FIXME comments inside `hydra.py`.

---

## 2. VIPER — Top-Mover Momentum

### What is it?
`src/strategies/viper.py` → class `ViperStrategy(StrategyHead)`.
Top gainers/losers watcher. Classifies each mover as STRIKE
(continuation, live) or COIL (reversal, dry-run only).

### When does it run?
- Initial `viper.scan()` at 09:30 IST.
- Re-scans at 10:00, 10:30, 11:00, 12:00.
- `viper.evaluate()` in every runner loop tick while watchlist is
  populated and slot manager has room.
- COIL evaluation is gated: before 11:00 COIL rows are filtered out.

### What data does it consume?
- `fetch_top_movers()` from `src/sniper/momentum_scanner.py` (Kite
  quote batches across all NSE EQ instruments).
- Sector map via `src/trading/sector_guard.py`.
- `MoveClassifier` + Groq enrichment (summaries per mover).
- Shared `TechnicalBody` snapshots.
- Depth analysis.
- Risk config (`src/config/risk.py` for `dry_run_conviction_threshold`).

### Logic
Score components (max 100):
1. Move quality (0–30): price magnitude + gap quality + volume
   conviction. COIL gets a 20% penalty. **`volume_ratio` is a
   price-derived proxy (`abs(pct_change)/2`) — documented in code.**
2. TA confirmation (0–25): `ViperRules.strike_confirms` for STRIKE,
   `ViperRules.coil_confirms` for COIL. Direction is reversal direction
   for COIL.
3. Depth (0–10): same shape as HYDRA. illiquid → hard kill.
4. Context bonus (0–10): sector leader, strong volume, post-11:00 bonus
   for COIL.

Decision rule: STRIKE ≥ 70 live trade; COIL ≥ `dry_run_conviction_threshold`
(default 60) logs a COIL signal — **never** executes live.

### Output
- `self.watchlist` (max 10).
- `self._coil_signals` list.
- `logs/viper_coil/YYYY-MM-DD_coil_report.json` at EOD (`save_coil_report`).
- ConvictionEngine signals with `strategy="VIPER"` (STRIKE) and
  `strategy="VIPER-COIL"` (dry-run, `is_dry_run=True`). COIL capped at 5
  per day.
- Live orders through slot_manager + executor on STRIKE fire.

### Health
- Active and producing output. `2026-04-16_post_market.md` shows 10 VIPER
  signals + 5 VIPER-COIL signals.
- Scan health summary method present.
- Reported conviction of 0 on those signals — they were logged but
  stayed below trigger threshold the whole session.

### Missing / broken
- `volume_ratio` proxy is acknowledged as not real relative volume. All
  volume-gated rules ride on this estimate.
- No TODO/FIXME in file.
- All 15 signals on 2026-04-16 had `conviction=0` → suggests upstream
  conviction not being filled or CE weighting zeroing them out. (Not a
  crash, but a pattern worth noting.)

---

## 3. DAWN — Day's Alpha Watch Network

### What is it?
`src/strategies/dawn.py` → class `DawnStrategy` (does **not** inherit
`StrategyHead`). Pre-market catalyst strategy. DRY-RUN ONLY — never
places live orders. Owns its scanner, scoring, position tracking, and
CSV logging.

### When does it run?
- `pre_market_scan()` at 08:30 IST.
- `merge_brief_and_generate()` is called from `pre_market_brief.py` /
  `brief_pipeline.py` around 08:52 IST.
- `execute_dawn_dryrun()` and `record_entries()` at 09:15–09:20.
- `update_dawn_dryrun()` every 15 min during market hours.
- `manage_positions()` every 5 min while any signal is ACTIVE.
- `close_all()` at 15:20.

### What data does it consume?
- `EventScanner.scan_since_close()` (Source B).
- Brave Search via `src/llm/brief_analyzer.fetch_brave_news` + Groq JSON
  extraction.
- Morning brief markdown (`logs/daily_reports/YYYY-MM-DD_morning_brief.md`)
  → parsed JSON block (Source A).
- `data/hydra_watchlist.json` via `select_dawn_candidates` (Source C).
- yfinance fast_info for entry and mark-to-market prices.

### Logic
1. Qualification filters: valid catalyst types per direction (LONG vs
   SHORT sets), min urgency (7 SHORT / 6 LONG), no Thursday (F&O
   expiry), freshness ≤ 24h, catalyst strength not LOW.
2. Scoring (0–100): catalyst quality (0–40), freshness (0–20),
   liquidity (0–15), technical (implicit), context (implicit).
3. Thresholds: LONG ≥ 60, SHORT ≥ 65.
4. Max signals/day: 5.
5. SL: 1.5% tight / 2% hard cap; trails at 1.5% ratcheting only.
6. Fixed dry-run capital: ₹5,000 per trade.

### Output
- `logs/dawn_dryrun/YYYY-MM-DD.csv` — full row per candidate with
  date/symbol/direction/catalyst/score/entry/current/SL/PnL/status.
- Dawn email at 08:52 (`send_dawn_email`).
- In-memory `_signals` list driving intraday management.

### Consumers
- EOD post-market report ("DAWN Post-Mortem" section).
- Morning brief email references DAWN signals.
- Operators (informational — dry-run only).

### Health
- Wired across runner (8 separate DAWN hooks).
- `2026-04-16_post_market.md` says "DAWN did not generate signals
  today." → DAWN is running but producing zero signals most days.
- `logs/dawn_dryrun/` directory pattern exists per date string.

### Missing / broken
- Reliance on morning brief file existing AND containing a `json` block
  with a `predictions` list — if that shape changes upstream, Source A
  silently returns empty (logged as warning).
- Heavy dependence on Brave Search JSON extraction via Groq — parsing
  uses a regex for the first `[...]` block, will return `[]` on
  malformed response.
- "Liquidity warning" is set but `avg_daily_turnover` is 0 for most
  candidates (no source populates it for DAWN_SCAN or BRAVE sources),
  so the `score_liquidity` falls back to the "unknown" branch (8.0).
- No TODO/FIXME comments in file.

---

## 4. Sniper-family modules

Not a single strategy — a collection of discovery/confirmation modules
that feed other things. They do have classes and entry points so they
are listed individually.

### 4a. `StockDiscovery` (`src/sniper/stock_discovery.py`)
- Ingests momentum_scanner output via `ingest_scanner_results` at 09:30
  and NSE/news catalysts via `ingest_manual`.
- Scores each symbol on catalyst(0–5) + momentum(0–5) + liquidity(0–5).
- Produces `top_n=10` ranked `DiscoveredStock` records.
- Output fuels `TechnicalScorer` evaluation and also fed into the
  "V2_DISCOVERY" branch that adds to ConvictionEngine (see
  `runner.py:904`).
- Health: alive in runner, but no recent reports explicitly label
  "V2_DISCOVERY" signals in the 2026-04-16 EOD (they go through CE).

### 4b. `TechnicalScorer` (`src/sniper/technical_scorer.py`)
- Imported as `TechnicalScorer, meets_entry_threshold`.
- Evaluates discovered stocks against an entry threshold before they
  become live trade candidates.

### 4c. `fetch_top_movers` (`src/sniper/momentum_scanner.py`)
- Hits Kite `quote` across every NSE EQ instrument in 450-symbol
  batches.
- Returns top 10 gainers + top 10 losers filtered by MIN_VOLUME 500k
  and MIN_PRICE ₹50. Runs once at 09:30.
- This is the **single shared source** for VIPER and StockDiscovery.

### 4d. `evaluate_symbol` / `AntigravityDecision` (`src/sniper/antigravity.py`)
- Computes live+historical stitched intraday bars, derives VWAP z-score.
- Emits IMMEDIATE_BUY_ALLOWED / WAITING_FOR_GRAVITY / BEAR_CONTROL /
  NO_DATA. Not used directly for trade execution — feeds the watcher.

### 4e. `AntigravityWatcher` (`src/sniper/antigravity_watcher.py`)
- State machine per symbol: WAITING_FOR_GRAVITY → WAITING_FOR_BOUNCE →
  COMPLETED.
- Fed by `bars_provider` in `watcher.tick(...)` each loop. Emits BUY
  signals on confirmed VWAP bounce.
- Output logged to `logs/antigravity_signals.csv` (runner line 1524).

### 4f. `evaluate_signal` (`src/sniper/core.py`)
- Legacy Sniper v1 daily-bar evaluator (EMA/RSI/MACD/BB/ADX on 252
  days). Not wired into `runner.py` — **dead-weight / library
  function only as of today**.

### Sniper module health
- Watcher and discovery pipeline are alive.
- `core.evaluate_signal` not referenced by `runner.py`.
- No TODO/FIXME markers.

---

## 5. ConvictionEngine (`src/trading/conviction_engine.py`)

### Relation to strategies
The ConvictionEngine is the central watchboard. Strategies are *signal
producers* that register ActiveSignals with it; the engine recomputes
conviction on a 15-min cycle.

### Layers (weights vary by signal type)
- Layer A (25% / 10% catalyst): Market phase via `market_phase`.
- Layer B (15%): Sector relative strength vs Nifty.
- Layer C (30% / 45% catalyst): Catalyst quality — FROZEN at add time.
- Layer D (20%): VWAP, ORB, volume, RSI, EMA9, MACD histogram.
- Layer E (10%): Pattern DB match (cold-start 50).

### Producers
- HYDRA → adds `ActiveSignal(strategy="HYDRA", …)` after pre-market scan.
- VIPER → adds `strategy="VIPER"` (STRIKE) and `strategy="VIPER-COIL"`
  (dry-run, capped at 5/day) after each scan.
- StockDiscovery V2 path → adds `strategy="V2_DISCOVERY"`.

### Consumers
- `conviction_engine.tick()` emits triggered signals every intraday
  interval; those go through the usual risk stack (SlotManager,
  TimeOfDay, TradeCosts, Sizing, DailyRisk) and then the executor.
- EOD reporting uses the watchboard summary and conviction history.

### Dedup & lifecycle
- `ASSET_CLASS_DEDUP`: only the highest conviction per asset class
  (e.g. SILVER ETFs, GOLD ETFs) is kept.
- `SIGNAL_MAX_AGE_HOURS = 4.0`, `SIGNAL_EXPIRY_TIME = 14:30` — no new
  entries in the last hour.
- `signal_type` auto-classifies as SCALP / MOMENTUM / SWING via ATR%
  and gap%.
- `is_dry_run=True` never executes live (COIL enforcement).

---

## 6. Full signal lifecycle

```
Pre-market
├─ 08:30 IST — DAWN.pre_market_scan()  →  own candidate list (Source B)
├─ 08:52 IST — DAWN.merge_brief_and_generate()
│                ├─ reads morning brief (Source A)
│                └─ reads data/hydra_watchlist.json (Source C)
│                → logs/dawn_dryrun/*.csv + DAWN email
└─ 09:00 IST — HYDRA.scan()
                 ├─ EventScanner → classified events
                 ├─ daily_regime.json → sector_momentum
                 ├─ yfinance → gap_pct
                 └─ writes data/hydra_watchlist.json
                    + adds to ConvictionEngine watchboard

Market open (09:15)
├─ DAWN records virtual entries for its signals + HYDRA dry-run picks
│   at 09:15–09:20 open prices → CSV rows created
└─ DAWN execute_dawn_dryrun + 15-min updates loop begins

09:30 momentum/discovery sweep
├─ fetch_top_movers() via Kite quote batch
├─ StockDiscovery.ingest_scanner_results + ingest_manual (NewsData EOD)
├─ VIPER.scan() → classify STRIKE/COIL, add to ConvictionEngine
└─ Confluence check: HYDRA ∩ VIPER symbols → SlotManager.register_confluence

Intraday loop (each ~60s tick)
├─ HYDRA.evaluate() on watchlist → conviction per symbol
│     └─ if ≥70 → place order via slot_manager.allocate("HYDRA", …)
├─ VIPER.evaluate() on watchlist
│     ├─ STRIKE ≥70 → live order
│     └─ COIL ≥ threshold → dry-run log only
├─ AntigravityWatcher.tick(...) → VWAP-bounce signals → logs CSV
└─ Every INTRADAY_INTERVAL_MIN:
      ConvictionEngine.tick(mkt_snap, tech_snaps)
         → recompute all layers, emit triggered signals
         → for each trigger: slot_manager → risk checks → executor

DAWN management
├─ manage_positions() every 5 min (SL trail, MTM updates)
├─ VIPER re-scans 10:00 / 10:30 / 11:00 / 12:00
└─ 15:20 IST — DAWN.close_all("TIME_EXIT_15:20")

EOD (post_market_report)
├─ VIPER.save_coil_report() → logs/viper_coil/*.json
├─ Post-mortem grades each signal (CORRECT/WRONG × HIT/MISSED)
└─ ConvictionEngine.persist_watchboard_to_json()
```

---

## 7. Shared data & duplication

### Shared data structures
- `SlotManager` — single instance, every strategy checks `can_trade` and
  calls `allocate`.
- `TechnicalBody` — each strategy owns its own instance, but the class
  is stateless on inputs; snapshots are built per-symbol per-tick.
- `ConvictionEngine._watchboard` — in-memory dict that all strategies
  push ActiveSignals into.
- `data/hydra_watchlist.json` — HYDRA writes, DAWN reads.
- `data/daily_regime.json` — written by morning brief pipeline, read
  by HYDRA and others.

### Duplication
- **Top-mover fetching**: `fetch_top_movers()` is called once at 09:30
  in the main runner block; `ViperStrategy._fetch_movers` then re-invokes
  `fetch_top_movers()` **again** inside `viper.scan()`. Both executions
  hit Kite quote API across all NSE EQ instruments.
- **Event scanning**: HYDRA and DAWN both construct `EventScanner()`
  instances. Each does its own scan, they do not share cached classified
  events.
- **Intraday bars**: HYDRA evaluator, VIPER evaluator, AntigravityWatcher,
  and ConvictionEngine each request intraday bars per symbol through
  `get_intraday_bars_for_symbol`. The BarBuilder cache absorbs most of
  this, but compute_or_stream runs per strategy.
- **yfinance fast_info**: called by HYDRA (gap_pct), DAWN (entry price,
  mark-to-market), independently.

### Cross-strategy dependencies
- DAWN → HYDRA (via `data/hydra_watchlist.json` + `select_dawn_candidates`).
- VIPER ← HYDRA (confluence check only — both-of-us set).
- All three → ConvictionEngine (Layer C frozen, Layers A/B/D/E timely).
- StockDiscovery → ConvictionEngine (V2_DISCOVERY branch).

---

## 8. System-wide observations (factual, no recommendation)

- `StrategyHead` ABC is implemented by HYDRA and VIPER. DAWN does NOT
  inherit from it — it is a standalone module with its own lifecycle.
- The Sniper family exists outside the Dragon abstraction entirely.
- One class (Sniper `core.evaluate_signal`) is not wired into the
  runner.
- The 2026-04-16 EOD shows DAWN zero-signals, VIPER/COIL 15 logged
  signals all with conviction=0, zero executed trades. The system ran,
  but no strategy crossed the execution threshold that day.
- No `TODO`, `FIXME`, `HACK`, `XXX`, or `NotImplemented` markers in any
  strategy file or sniper file.
- `HYDRA_SCAN_TIME = dt_time(8, 15)` in runner is a constant; the
  fire-gate uses 09:00 in the same file — cosmetic inconsistency.
- VIPER `volume_ratio` is explicitly documented in code as a proxy, not
  real relative volume.

---

## Appendix — File → entity index

```
src/strategies/base.py            StrategyHead, ConvictionScore, WatchlistEntry
src/strategies/hydra.py           HydraStrategy, HydraRules
src/strategies/viper.py           ViperStrategy
src/strategies/viper_rules.py     ViperRules
src/strategies/move_classifier.py MoveClassifier, MoveType, TradeMode, ClassifiedMover
src/strategies/dawn.py            DawnStrategy, DawnCandidate, DawnSignal,
                                  DawnDailyMetrics, execute_dawn_dryrun,
                                  update_dawn_dryrun, select_dawn_candidates,
                                  send_dawn_email
src/strategies/technical_body.py  TechnicalBody, TechnicalSnapshot,
                                  StreamingTechnicalState
src/strategies/slot_manager.py    SlotManager, TradeSlot
src/sniper/momentum_scanner.py    fetch_top_movers, CandidateStock
src/sniper/stock_discovery.py     StockDiscovery, DiscoveredStock
src/sniper/technical_scorer.py    TechnicalScorer, TechScore, ScoreBreakdown,
                                  meets_entry_threshold
src/sniper/antigravity.py         evaluate_symbol, evaluate_antigravity,
                                  AntigravityDecision, AntigravityStatus
src/sniper/antigravity_watcher.py AntigravityWatcher, WatchedSymbol, WatchState
src/sniper/core.py                evaluate_signal (legacy, unwired)
src/trading/conviction_engine.py  ConvictionEngine, ActiveSignal, layer fns
```
