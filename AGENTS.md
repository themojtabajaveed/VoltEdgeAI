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

## Phase 4 Roadmap (not yet implemented)
- Wire `route` + `confidence` into ConvictionEngine metadata so TradeRecord stores them
- Pattern DB learning loop: use hydra_shadows_*.json to populate follow-through rates
- Router R5 graduation: replace cold-start pass with live pattern_db lookup
- Post-market feedback compares DAWN actual vs HYDRA shadow counterfactual

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
