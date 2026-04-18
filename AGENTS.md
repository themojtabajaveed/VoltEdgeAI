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

## Phase 6 Roadmap (not yet implemented)
- [ ] Live mode: DRY_RUN=false + real Zerodha order placement
- [ ] Backtesting harness (replay historical days through router + conviction gate)
- [ ] Auto-apply tuning suggestions after N consecutive same-direction signals
- [ ] Telegram/WhatsApp alert for missed opportunities > 3 in a week

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
