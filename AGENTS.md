# AGENTS.md — Task-to-File Router
# READ THIS before exploring the repo. Open ONLY the listed files. Skip everything else.

## Morning Brief (9 AM IST email)
OPEN: src/reports/pre_market_brief.py
OPEN: src/reports/brief_pipeline.py → Section 0–6 assembly + USD/INR row
OPEN: src/data_ingestion/pre_market_intelligence.py → Section 0 signal table (fetch + score)
OPEN: src/data_ingestion/pre_market_data.py → shared pre-market pipeline (Tier1 filings + Tier2 Nifty200); cache at data/premarket_cache_YYYY-MM-DD.json; exposes get_scan_universe() and fetch_all_premarket_data()
OPEN: src/runner.py → search "pre_market" block only
IF LLM broken: src/llm/ + src/juror/
IF DB broken: src/db/models.py → DailySignal, ConvictionScore only
IF exchange filings broken: src/data_ingestion/exchange_filings.py
SKIP: strategies/, market_chronicle.py, feedback_loop.py

## Mid-Session Report (12:30 PM IST)
OPEN: src/reports/market_chronicle.py → search "mid_session" block
OPEN: src/runner.py → search "mid_session" block only
SKIP: pre_market_brief.py, strategies/, feedback_loop.py

## Post-Market / EOD Report
OPEN: src/reports/post_market_report.py (full))
OPEN: src/reports/feedback_loop.py
IF LLM broken: src/llm/ + src/juror/
SKIP: pre_market_brief.py, strategies/

## DawnHydraRouter
OPEN: src/strategies/router.py (full)
DEPENDS ON: src/strategies/base.py (WatchlistEntry), src/data_ingestion/pre_market_data.py (PreMarketSignals)
CALLED FROM: src/runner.py → between HYDRA scan (08:15) and DAWN email (08:52)
SKIP: everything else

## Phase 3: Execution Wiring (router output → trade paths)
OPEN: src/strategies/dawn.py → select_dawn_candidates(router_filter=...)
OPEN: src/runner.py → search "dawn_candidates_today" (outer-scope) and "hydra_shadows_today"
OPEN: src/trading/conviction_engine.py → CONVICTION_THRESHOLD=70, [CONVICTION GATE] drop log
OPEN: src/trading/executor.py → [DRY-RUN] log includes route/confidence/target/sl
OPEN: src/reports/post_market_report.py → _build_section_10_router
ARTIFACT: data/hydra_shadows_YYYY-MM-DD.json — counterfactual HYDRA tracker
TESTS: tests/test_router.py, tests/test_phase3_wiring.py
SKIP: everything else unless behavior bug crosses these files

## Phase 4: Config System (COMPLETE)
config.yaml at repo root is now the single source of truth for all tunable params.
Zero hardcoded values remain in runner / conviction_engine / router / post-market
report / executor. Env var LIVE_MODE=1 (or VOLTEDGE_LIVE_MODE=1) is the sole
override — it forces live_mode even if config says dry_run:true.

### Full config.yaml (8 sections)
```yaml
execution:
  dry_run: true
  max_trades_per_day: 5
  per_trade_risk_inr: 100000
  max_open_positions: 5
  conviction_threshold: 70

router:
  dawn_confidence_min: 0.85
  hydra_confidence_min: 0.0
  router_enabled: true

dawn:
  pre_market_scan_enabled: true
  scan_time_ist: "08:30"
  select_time_ist: "08:45"

hydra:
  scan_time_ist: "08:15"
  shadows_persist: true
  shadows_dir: "data/"

market:
  open_time_ist: "09:15"
  close_time_ist: "15:30"
  intraday_interval_minutes: 15

reporting:
  post_market_report_enabled: true
  router_performance_section: true
  email_enabled: true

logging:
  conviction_gate_log: true
  router_filter_log: true
  dry_run_log: true

counterfactual:
  target_pct: 2.0
  sl_pct: -1.5
  auto_run_post_market: true
```

### Wiring table (param → config key → reader)
| Param                  | Config key                              | Reader (file)                           |
|------------------------|-----------------------------------------|-----------------------------------------|
| conviction threshold   | execution.conviction_threshold          | src/trading/conviction_engine.py        |
| dry-run / live mode    | execution.dry_run (env LIVE_MODE wins)  | src/runner.py (entry point)             |
| max trades / day       | execution.max_trades_per_day            | src/runner.py                           |
| per-trade risk ₹       | execution.per_trade_risk_inr            | src/runner.py                           |
| max open positions     | execution.max_open_positions            | src/runner.py                           |
| router kill-switch     | router.router_enabled                   | src/strategies/router.py                |
| DAWN min confidence    | router.dawn_confidence_min              | src/strategies/router.py                |
| HYDRA min confidence   | router.hydra_confidence_min             | src/strategies/router.py                |
| shadow persistence     | hydra.shadows_persist                   | src/runner.py (08:15 block)             |
| shadow directory       | hydra.shadows_dir                       | src/runner.py (08:15 block)             |
| Section 10 toggle      | reporting.router_performance_section    | src/reports/post_market_report.py       |
| email send toggle      | reporting.email_enabled                 | src/reports/post_market_pipeline.py     |
| conviction-gate log    | logging.conviction_gate_log             | src/trading/conviction_engine.py        |
| dry-run log            | logging.dry_run_log                     | src/trading/executor.py                 |
| counterfactual targets | counterfactual.target_pct / sl_pct      | src/analysis/counterfactual.py          |
| counterfactual auto    | counterfactual.auto_run_post_market     | src/runner.py (16:00 post-market block) |

### validate_config() — raises ValueError on:
- execution.conviction_threshold not int in [50, 100]
- execution.max_trades_per_day not int in [1, 20]
- execution.per_trade_risk_inr not numeric > 0
- execution.max_open_positions not int in [1, 20]
- execution.dry_run not bool
- router.dawn_confidence_min not float in [0.0, 1.0]
- router.hydra_confidence_min not float in [0.0, 1.0]
- counterfactual.target_pct / sl_pct not numeric
- counterfactual.auto_run_post_market not bool

Called at runner startup — service fails loudly on bad config rather than
trading with garbage parameters.

### Phase 4 Roadmap (remaining, not yet implemented)
- Wire `route` + `confidence` into ConvictionEngine metadata so TradeRecord stores them
- Pattern DB learning loop: use hydra_shadows_*.json to populate follow-through rates
- Router R5 graduation: replace cold-start pass with live pattern_db lookup
- Post-market feedback compares DAWN actual vs HYDRA shadow counterfactual

### One-liner: disable the router without touching code
Set `router.router_enabled: false` in config.yaml → restart voltedge service.
All candidates then bypass routing as route="UNROUTED".

## Phase 5: Counterfactual Analysis (DONE)
Purpose: feedback loop telling you whether the router's DAWN-vs-HYDRA decisions
were correct. Reads the HYDRA shadow roster persisted each morning, fetches
EOD prices, compares actual market outcome to what DAWN would have captured.

OPEN: src/analysis/counterfactual.py
- `load_shadows(date_str)` → reads data/hydra_shadows_YYYY-MM-DD.json
- `fetch_eod_result(symbol, date_str)` → yfinance (.NS) OHLCV
- `score_shadow(shadow, eod)` → pct_move, direction, verdict
- `run_counterfactual(date_str)` → summary dict {total_shadows,
  correct_routes, missed_opportunities, neutral, router_accuracy, details}
- `persist_counterfactual(summary)` → data/counterfactual_YYYY-MM-DD.json

Verdicts:
- CORRECT_ROUTE      — pct_move ≤ sl_pct (HYDRA was right; DAWN would have stopped out)
- MISSED_OPPORTUNITY — pct_move ≥ target_pct (DAWN would have captured the move)
- NEUTRAL            — neither threshold hit
Thresholds from config.yaml:counterfactual (target_pct, sl_pct).

OPEN: src/analysis/router_tuner.py (READ-ONLY — never writes config.yaml)
- `load_cf_history(days=5)` → recent counterfactual_*.json summaries
- `compute_tuning_suggestion(history)` → {action, current_value,
  suggested_value, reason}
- `print_tuning_report(days=5)` → CLI-friendly summary
CLI: `python -m src.analysis.router_tuner`

OPEN: src/config_loader.py → get_cf_target_pct, get_cf_sl_pct, get_cf_auto_run,
  get_router_dawn_confidence_min
OPEN: src/reports/post_market_pipeline.py → _build_section_11_counterfactual,
  _build_section_12_weekly_router (Fridays only, weekday==4)
OPEN: src/runner.py → search "Phase 5: Counterfactual" — runs after 16:00 report
  if get_cf_auto_run() is True

Weekly summary (Fridays only): 5-day accuracy table + tuning suggestion.
How to act on suggestions: edit config.yaml → router.dawn_confidence_min →
restart voltedge service. NEVER auto-applied.

ARTIFACTS: data/counterfactual_YYYY-MM-DD.json
TESTS: tests/test_counterfactual.py, tests/test_router_tuner.py
SKIP: everything else unless a bug crosses these modules

## Phase 6: Backtesting Harness (DONE)

### Architecture
data_loader → signal_replayer → engine

- `src/backtest/data_loader.py` — fetches daily OHLCV via Zerodha KiteConnect / SQLite cache
- `src/backtest/signal_replayer.py` — builds WatchlistEntry from candle, runs router + conviction gate
- `src/backtest/engine.py` — orchestrator: BacktestRouter, BacktestConvictionScorer, run_backtest(), print_backtest_report()
- `src/backtest/__main__.py` — CLI entry point

### How to run
```
python -m src.backtest --days 90
python -m src.backtest --days 30 --no-persist
python -m src.backtest --days 90 --warm-cache    # fetch fresh data first
```

### Populating the history cache (run ONCE after install)
```
python scripts/populate_history.py               # 201 symbols × 0.35s = ~70s
```
Requires `ZERODHA_API_KEY` + `ZERODHA_ACCESS_TOKEN` in `.env`.  If the token
is expired, run `python -m src.tools.auto_login` first.

### What it tests
Router + conviction gate replayed on 90 calendar days of real daily price data.
No filing data, no news, no intraday resolution — pure daily OHLCV through the
deterministic routing rules and simplified conviction scoring.

### config.yaml backtest section
```yaml
backtest:
  default_days: 90
  target_pct: 2.0        # 2% profit target per trade
  sl_pct: 1.5            # stop loss % (positive, applied as -sl_pct)
  throttle_seconds: 0.35 # sleep between Kite API calls
  auto_run_weekly: false  # set true to trigger every Friday post-market
```

### Limitations
- No intraday simulation: uses daily open/close/high/low only; assumes 09:15 open entry
- No filing or news data in replay: filing_category="" for all historical entries → router
  R1/R2 rules always fail → most signals route to HYDRA, almost none to DAWN
- BacktestConvictionScorer is a simplified heuristic (gap_pct + volume), not the live
  multi-layer engine — scores are directionally correct but not calibrated
- KiteConnect auth is reused from `.env` (ZERODHA_API_KEY + ZERODHA_ACCESS_TOKEN); if
  the token is absent/expired the backtest falls back to SQLite cache only — see
  "Populating the history cache" above and/or the `--warm-cache` flag
- The summary now reports `symbols_with_data`, `symbols_cache_only`, and
  `data_quality_pct` so you can tell at a glance when results are skewed by
  missing data

### Output
`data/backtest_YYYY-MM-DD_90d.json` + formatted CLI report

### config.yaml accessors added to config_loader.py
`get_backtest_days()`, `get_backtest_target_pct()`, `get_backtest_sl_pct()`,
`get_backtest_throttle()`, `get_backtest_auto_run_weekly()`

TESTS: tests/test_backtest.py (8 tests)

## Phase 7: Live Execution (DONE)

Real Zerodha order placement is now wired. `execution.dry_run: true` remains
the default and preserves pre-Phase-7 behavior exactly; setting it to `false`
enables a 5-gate safety system before every real order.

### 5-gate safety system (evaluated in order)
1. **Config gate** — `get_dry_run()` must be `False`; otherwise
   `[LIVE GATE 1 FAIL] dry_run=true — order blocked`.
2. **Conviction gate** — `conviction >= get_conviction_threshold()` (70);
   otherwise `[LIVE GATE 2 FAIL] {symbol} conviction={X} < {threshold}`.
3. **Daily trade limit** — `trades_placed_today < get_max_trades()` (5);
   otherwise `[LIVE GATE 3 FAIL] Daily limit {max} reached — {symbol} blocked`.
4. **Position limit** — `open_positions < get_max_open_positions()` (5);
   otherwise `[LIVE GATE 4 FAIL] Max positions {max} reached — {symbol} blocked`.
5. **Market hours** — IST time within `09:15 ≤ t ≤ 15:15`;
   otherwise `[LIVE GATE 5 FAIL] Outside trading hours {HH:MM} IST — {symbol} blocked`.

Every gate PASS also logs `[LIVE GATE {n} PASS] ...`. Gates are implemented in
`src/trading/executor.py::run_live_gates`. If any gate fails, `place_order` is
never called — the runner continues.

### Position sizing (live mode)
```
per_trade_risk = get_per_trade_risk()          # ₹100000 from config
quantity       = int(per_trade_risk / entry_price)
```
Below-1 quantity → order skipped with
`[LIVE] {symbol} quantity=0 at ₹{entry_price} — skipped (price too high)`.
Successful sizing logs
`[LIVE] {symbol} | qty={qty} | entry=₹{entry} | risk=₹{risk}`.

### Live trade tracker
`src/trading/live_trade_tracker.py` persists every live order to
`data/live_trades_YYYY-MM-DD.json` with:
```
symbol, order_id, quantity, entry_price,
route, confidence, conviction_score,
placed_at (ISO IST),
status ("OPEN" | "CLOSED"),
exit_price, exit_at, pnl_pct
```
APIs: `record_live_trade`, `get_trades_placed_today`, `get_open_positions`,
`get_open_trades_today`, `get_all_trades_today`, `update_trade_status`.

### EOD square-off (15:15 IST)
`src/runner.py` checks at 15:15 IST (config key `eod_squareoff_time_ist`).
For each OPEN record in today's live-trades file, a SELL MIS order is placed
via `executor.execute_sell`, the tracker is closed with exit price and
`pnl_pct`, and a logline `[EOD SQUARE-OFF] {symbol} | qty={n} | order_id={id}`
is emitted. Failures emit
`[EOD SQUARE-OFF FAILED] {symbol} — MANUAL ACTION REQUIRED` plus an immediate
email alert.

### Startup banner
When live mode is on at startup, runner prints:
```
⚠️  LIVE MODE ACTIVE — REAL ORDERS WILL BE PLACED
Conviction threshold : 70
Max trades/day       : 5
Per-trade risk       : ₹1,00,000
Max open positions   : 5
Press Ctrl+C within 10 seconds to abort...
```
then sleeps `execution.live_startup_countdown` (default 10s) before proceeding.

### Email alert on live order
`src/reports/email_sender.py::send_live_order_alert` fires the moment an order
is placed. Subject: `VoltEdgeAI LIVE ORDER: {symbol} | qty={qty} | ₹{entry}`.
Body includes all 5 gate passes + conviction. Gated on
`reporting.email_enabled`.

### Post-market Section 13
`src/reports/post_market_pipeline.py::_build_section_13_live_trades` adds
`## 13. Live Trades Today` to the EOD email when dry_run=false:
a per-trade table (symbol · order_id · qty · entry · exit · pnl_pct · status)
plus a summary line with realized and unrealized totals.

### New config.yaml keys (execution)
```yaml
execution:
  live_order_tag: "VOLTEDGE_AUTO"
  eod_squareoff_time_ist: "15:15"
  live_startup_countdown: 10
```
Accessors: `get_live_order_tag()`, `get_eod_squareoff_time()`,
`get_live_startup_countdown()`.

### How to enable live mode
Set `execution.dry_run: false` in `config.yaml`, then restart the service.
WARNING: Real orders will be placed. Ensure Kite token is valid.

### How to emergency-stop
Set `execution.dry_run: true` in `config.yaml` and restart, OR
`sudo systemctl stop voltedge` to halt the runner immediately.

ARTIFACTS: `data/live_trades_YYYY-MM-DD.json`
TESTS: `tests/test_live_execution.py` (10 tests)

## Phase 8: Stop-Loss Unification (DONE)
OPEN: src/trading/exit_engine.py — unified ExitEngine, reads pos.strategy_config
OPEN: src/trading/positions.py — Position now carries strategy_config, time_stop_ist, orb_stop_low
OPEN: src/trading/daily_risk_state.py — circuit breaker, halt/flatten logic
OPEN: src/trading/shadow_book.py — dry-run shadow PositionBook, write-through JSON
OPEN: src/trading/stoploss_config.py — DEFAULT_STRATEGY_CONFIG fallback (implemented in config_loader.py)
OPEN: src/trading/atr.py — compute_daily_atr_from_cache helper
OPEN: config.yaml — stoploss: section (by_strategy: DAWN/HYDRA/VIPER)
OPEN: src/config_loader.py — get_stoploss_config(), get_daily_loss_halt_multiplier(), apply_conviction_modifier()
ARTIFACTS: data/sl_shadow_YYYY-MM-DD.json
TESTS: tests/test_phase8_stoploss.py, tests/test_phase8_dawn_live.py

## DAWN Strategy (8:45 AM email + 9:15 market order)
OPEN: src/strategies/viper.py → search "DAWN" block
OPEN: src/reports/pre_market_brief.py → search "dawn" function
OPEN: src/runner.py → search "dawn" or "0845" block
SKIP: market_chronicle.py, feedback_loop.py, sniper/

## Conviction Score / Threshold
OPEN: src/strategies/viper.py → search "conviction_score" or "threshold"
OPEN: src/db/models.py → ConvictionScore table only
OPEN: src/juror/ → search "score" block
SKIP: reports/, feedback_loop.py

## VIPER Strategy
OPEN: src/strategies/viper.py (full)
OPEN: src/db_writer.py → search "viper" block only
SKIP: sniper/, reports/, llm/ (unless LLM scoring broken)

## PatternDB / Layer E
OPEN: data/pattern_db.json → inspect first
OPEN: src/db_writer.py → search "pattern" or "layer_e"
SKIP: everything else unless root cause points elsewhere

## Scheduler / Service Timing
OPEN: src/runner.py (full)
OPEN: /etc/systemd/system/voltedge.service
SKIP: all src/ except runner.py

## Token Refresh / Auth / Kite
OPEN: search repo for "access_token" or "kite_connect" → open that file only
SKIP: everything else

## DB / SQLAlchemy
OPEN: src/db/models.py
OPEN: src/db_writer.py
SKIP: reports/, strategies/, llm/, runner.py

## Email / SMTP
OPEN: search repo for "smtplib" or "send_email" → open that utility file only
OPEN: whichever report module is broken (see above)
SKIP: strategies/, db/, runner.py

## Juror / LLM Scoring
OPEN: src/juror/ (full)
OPEN: src/llm/ → only the vendor file relevant to the bug (gemini.py / grok.py / claude.py)
SKIP: strategies/, reports/, db/

## R4 Filing Freshness + Event Quality (DONE)
OPEN: src/utils/market_calendar.py → is_market_day(), market_minutes_of_exposure(), get_nse_holidays()
OPEN: src/utils/filing_freshness.py → FilingFreshness enum, classify_filing_freshness()
OPEN: src/utils/event_quality.py → score_event_quality(), passes_quality_gate(), classify_market_confirmation()
OPEN: src/strategies/router.py → R4 block (4-layer), _parse_filing_ts(), _get_sector_followthrough()
OPEN: src/data_ingestion/exchange_filings.py → _parse_deal_size_inr(), enrich_filing(), FilingEvent new fields
OPEN: src/data_ingestion/event_scanner.py → apply_event_evaluation_gate(), MarketEvent new fields
OPEN: src/trading/conviction_engine.py → apply_filing_metadata_adjustment()
ARTIFACTS: data/nse_holidays_{year}.json (run scripts/fetch_nse_holidays.py each December)
TESTS: tests/test_r4_freshness.py, tests/test_event_quality.py, tests/test_event_scanner.py, tests/test_exchange_filings.py, tests/test_market_calendar.py, tests/test_conviction_engine.py
KNOWN GAP: pattern_db sector follow-through (Step 9) — 67 legacy entries, 0 active, deferred
SKIP: everything else

## Event Study Pipeline (Priority 1) — COMPLETED ✅

### What it does
End-to-end pipeline that:
1. Archives BSE corporate action filings via `FilingArchiver` → `filing_archive` table
2. Promotes confirmed filings to `filings_archive` via `promote_to_filings_archive()`
3. Builds event windows (T-10 to T+10) using `EventStudyBuilder` → `event_study` table
4. Exports results to CSV/XLSX via `EventStudyExporter`
5. Runs sequentially via `python -m src.eventstudy.run_event_study` (no CLI flags — hardcoded runner)

### Key files
- `src/eventstudy/filing_archiver.py` — BSE scraping + DB insert
- `src/eventstudy/event_study_builder.py` — OHLCV window construction
- `src/eventstudy/event_study_exporter.py` — CSV/XLSX export
- `src/eventstudy/run_event_study.py` — sequential pipeline runner
- `config/config.yaml` → `event_study:` section for all tuning params

### Test coverage
- Group 1: FilingArchiver unit tests
- Group 2: Symbol mapping tests
- Group 3: BSE backfill tests
- Group 4: EventStudyBuilder tests
- Group 5: Bridge/promote tests
Run with: `pytest tests/test_event_study*.py -v`

### Known limitations (dev machine)
- No OHLCV cache locally → 0 events with price data in DAWN validation
- `tzlocalize` error in EventStudyBuilder on tz-naive datetimes — fix pending (Prompt 1.9 candidate)
- Groq rate-limit warnings on VM — non-blocking

### Status
Structurally complete and tested (324 tests passing). Needs live Kite token on GCP VM for real DAWN verdict.
