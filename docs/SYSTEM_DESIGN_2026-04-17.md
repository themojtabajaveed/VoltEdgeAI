# VoltEdgeAI — System Design 2026-04-17

**Author:** Principal architect (Claude)
**Scope:** Full architecture for DAWN / HYDRA / VIPER on a shared pre-market data foundation, fully automatic (no human in the loop for dry-run entries/exits).
**Status:** Design doc — drives all future development.

---

## Context (why this doc exists)

The system has three strategies (DAWN, HYDRA, VIPER), each with independent data fetchers, independent scoring systems, and partially overlapping responsibilities. A shared `pre_market_data.py` foundation was just built but is not yet consumed by all three. The owner wants:

1. A clean architectural boundary between DAWN (9:15 blind entry) and HYDRA (catalyst + confirmation).
2. A real-time intraday filings pipeline so HYDRA can react to mid-day news within minutes.
3. VIPER driven by continuous live tick data, not 15-minute polling.
4. Every strategy consuming the shared `pre_market_data.py` cache — no duplicate fetches.
5. Feedback loops so each strategy learns from its own outcomes.

The current audit (docs/STRATEGY_AUDIT_2026-04-17.md) confirms: `HYDRA_SCAN_TIME=08:15` is never honored, `fetch_top_movers()` runs twice at 09:30, VIPER's `volume_ratio` is a proxy (`abs(pct)/2`), DAWN runs standalone with its own 8-layer scoring, and on 2026-04-16 all 15 signals hit conviction=0. The fix is not another patch — it is a principled architecture that binds these three strategies to a shared foundation and removes duplicated logic.

---

## Executive Summary

**Recommendation in one paragraph:** Keep three strategies with distinct personalities, but wire them all to a single shared DataBus (pre_market_data.py + a new IntradayBus that wraps the already-integrated Kite WebSocket). Make DAWN an escalation filter (conviction ≥ 85 with catalyst-only-at-open signature), and HYDRA the default path for everything else with catalyst. VIPER becomes a pure tick-driven breakout engine that subscribes to the live BarBuilder, not a 15-min polled scanner. Retire `sniper/core.py` from production hot path (keep it for EOD scripts), absorb AntigravityWatcher into VIPER as a post-breakout pullback filter, and fix the `volume_ratio` proxy by reading `rel_volume` from the shared pre-market cache. Add a real intraday filings watcher (poll NSE corporate announcements every 30s during market hours) that pushes events into HYDRA's evaluation queue. Add a per-strategy feedback loop keyed on catalyst type × time-of-day × outcome.

**Five highest-leverage changes to make first:**

1. Wire DAWN, HYDRA, VIPER to `pre_market_data.fetch_all_premarket_data()` as the sole pre-market data source. Delete the three independent EventScanner instances and the duplicate `fetch_top_movers()` call in `viper.scan()`.
2. Fix HYDRA's 08:15 timing — fire the pre-market scan at 08:15 from the shared cache, not at 09:00.
3. Build `IntradayFilingsWatcher` (new component) — polls NSE filings every 30s during 09:15–14:30 IST and pushes new high-urgency filings into HYDRA's evaluate queue.
4. Hook VIPER into the existing BarBuilder tick stream — subscribe to a live universe, run breakout detection on every tick instead of 15-min scans.
5. Introduce `DawnHydraRouter` — one decision function that takes a pre-market candidate and routes it to DAWN (open-and-done) or HYDRA (catalyst + intraday confirmation) based on deterministic rules.

---

## Full Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                      SHARED DATA FOUNDATION                         │
│                                                                     │
│  ┌────────────────────────────┐    ┌──────────────────────────────┐ │
│  │ pre_market_data.py         │    │ IntradayBus (NEW)            │ │
│  │ - fetch_all_premarket_data │    │ - KiteTicker WebSocket       │ │
│  │ - PreMarketSignals cache   │    │ - BarBuilder (1m/5m bars)    │ │
│  │ - Tier1 filings + Nifty200 │    │ - ExitEngine.check_tick      │ │
│  │ - Nightly refresh 08:00IST │    │ - pub/sub to strategies      │ │
│  └────────────┬───────────────┘    └──────────────┬───────────────┘ │
│               │                                    │                 │
│               │         ┌──────────────────────────┤                 │
│               │         │ IntradayFilingsWatcher   │                 │
│               │         │ (NEW, polls NSE every 30s│                 │
│               │         │  during market hours)    │                 │
│               │         └──────────────┬───────────┘                 │
└───────────────┼────────────────────────┼─────────────────────────────┘
                │                        │
     ┌──────────┼────────────────────────┼──────────┐
     │          │                        │          │
     ▼          ▼                        ▼          ▼
┌─────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌──────────────┐
│  DAWN   │ │ DawnHydraRouter │ │     HYDRA       │ │    VIPER     │
│ (08:30  │ │ (NEW, decides   │ │ (08:15 + live   │ │ (live tick-  │
│  scan,  │ │  dawn vs hydra) │ │  filings +      │ │  driven      │
│  9:15   │ │                 │ │  orchestrator)  │ │  breakout)   │
│  entry) │ │                 │ │                 │ │              │
└────┬────┘ └─────────────────┘ └────────┬────────┘ └──────┬───────┘
     │                                   │                  │
     └───────────────┬───────────────────┴──────────┬───────┘
                     ▼                              ▼
           ┌──────────────────────┐      ┌──────────────────────┐
           │  SlotManager         │      │  ConvictionEngine    │
           │  (single arbiter)    │      │  (5-layer dynamic    │
           │                      │      │   watchboard)        │
           └──────────┬───────────┘      └──────────┬───────────┘
                      │                             │
                      └──────────────┬──────────────┘
                                     ▼
                          ┌──────────────────────┐
                          │  TradeExecutor       │
                          │  (Zerodha orders +   │
                          │   ExitEngine hooks)  │
                          └──────────┬───────────┘
                                     │
                                     ▼
                          ┌──────────────────────┐
                          │ FeedbackLoop (per-   │
                          │ strategy pattern DB) │
                          └──────────────────────┘
```

**Key property:** every arrow carries a typed dataclass. No raw dicts flow between modules.

---

## 1. System Architecture — How Pieces Connect

### 1.1 Timeline of a trading day

| IST Time | Actor | Action |
|---|---|---|
| 08:00 | `runner` | Refresh F&O ban lists, T2T lists |
| 08:00 | `pre_market_data` | `fetch_all_premarket_data(force_refresh=True)` → `data/premarket_cache_YYYY-MM-DD.json` |
| 08:15 | HYDRA | `scan()` reads cache, builds watchlist using Tier1 filings + Tier2 Nifty200 with urgency ≥ 6.0 |
| 08:20 | DawnHydraRouter | Iterates HYDRA's watchlist, extracts DAWN-eligible subset (see §2) |
| 08:30 | DAWN | `pre_market_scan()` scores DAWN subset using shared cache (no independent fetch) |
| 08:52 | DAWN | `merge_brief_and_generate()` — emits final DAWN signals, sends email |
| 09:00 | `runner` | Morning brief email + Grok strategist |
| 09:15 | DAWN | `record_entries()` — places market-open orders (dry-run or live) |
| 09:15 | IntradayBus | WebSocket already connected; subscribes to DAWN entries + HYDRA watchlist + VIPER universe |
| 09:15 | VIPER | `on_tick` handler starts firing — no polling, pure event-driven |
| 09:15 | IntradayFilingsWatcher | Starts 30s poll loop on NSE corporate announcements API |
| 09:15–14:30 | HYDRA | Evaluates pre-market watchlist on each orchestrator tick + on every new high-urgency filing |
| 09:15–15:15 | VIPER | Continuous breakout detection on tick stream |
| 14:30 | HYDRA | No new entries after this time (time-of-day guard) |
| 15:15 | VIPER | No new entries last 15 min |
| 15:20–15:25 | DAWN | `close_all()` — hard close all DAWN positions |
| 15:30 | IntradayBus | Unsubscribe WebSocket, flush tick queue |
| 15:40 | `runner` | Grok EOD optimizer |
| 16:00 | `runner` | Post-market report (subprocess) |
| 16:30 | FeedbackLoop | Updates per-strategy pattern DBs with today's outcomes |

### 1.2 New components to build

| Component | File | Responsibility |
|---|---|---|
| `DawnHydraRouter` | `src/strategies/router.py` (new) | Pure function: given `PreMarketSignals` + filing metadata, return `DAWN` or `HYDRA` |
| `IntradayFilingsWatcher` | `src/data_ingestion/intraday_filings.py` (new) | 30s poll of NSE corporate announcements; dedupe; push new filings to a queue |
| `IntradayBus` | `src/data_ingestion/intraday_bus.py` (new, thin wrapper) | Pub/sub API over existing `market_live.py` BarBuilder. Strategies subscribe by event type (tick, 1m_bar, 5m_bar, filing) |
| `ViperTickHandler` | `src/strategies/viper_tick.py` (new) | Pure per-tick breakout detection. Subscribed to IntradayBus. No polling, no rescans |
| `FeedbackWriter` | `src/feedback/writer.py` (new) | EOD post-mortem: for each trade, compute realized outcome and append to the right pattern DB |

### 1.3 What each strategy consumes from the shared foundation

**All three read from `PreMarketSignals` dataclass cache**: `prev_close`, `current_price`, `gap_pct`, `rel_volume`, `volume_signal`, `pct_from_30d_high`, `technical_setup`, `oi_change_pct`, `pcr`, `fno_signal`, `bulk_deal`, `sector`, `sector_momentum`, `filing_category`, `filing_urgency`, `filing_impact_score`.

- **DAWN** adds: 30-day gap follow-through rate (computed once in pre_market_data, cached).
- **HYDRA** adds: full filing text (from IntradayFilingsWatcher or overnight EventScanner).
- **VIPER** adds: live tick stream + real-time VWAP/ORB/ATR (from BarBuilder, not from pre_market_data).

**Delete from strategies:** all direct calls to yfinance, all independent `EventScanner()` instances, all independent `fetch_top_movers()` calls.

---

## 2. The DAWN / HYDRA Boundary

### 2.1 Current thinking (from owner)

- DAWN: catalyst so strong the only entry is 9:15. After 9:15 the opportunity is gone. Highest conviction.
- HYDRA: same type of stocks but medium-to-high conviction, needs TA confirmation or waits for gap-up risk to clear.

### 2.2 Pushback

"Highest conviction" is not a signal — it is an outcome. You cannot tell DAWN from HYDRA based on a conviction number because DAWN's conviction score and HYDRA's conviction score are computed by different formulas. What actually distinguishes them is **catalyst decay profile** — how much of the expected move happens in the first 15 minutes vs distributed across the day.

The boundary must be drawn on catalyst *type* and *timing properties*, not on score magnitude.

### 2.3 The deterministic router

A candidate goes to DAWN iff **all** of the following are true:

| # | Rule | Why |
|---|---|---|
| R1 | Filing urgency ≥ 8.5 (from pre_market_data.filing_impact_score) | Only top-tier catalysts compress entire move into first 15 min |
| R2 | Catalyst category in DAWN_CATEGORIES | See table below |
| R3 | Projected open > previous_day_high × 1.02 OR < previous_day_low × 0.98 | Must actually gap out of range |
| R4 | Time-since-filing ≤ 16 hours (filing landed overnight or pre-market, not day-old) | Stale catalysts don't compress |
| R5 | Historical follow-through rate for this (category, direction) ≥ 65% | Based on pattern_db; if unknown, default to HYDRA |
| R6 | Liquidity: avg_volume_20d ≥ 500,000 shares | Can't market-order a thin stock at open |

**DAWN_CATEGORIES** (pre-market open-only): `FDA_APPROVAL`, `MAJOR_CONTRACT_WIN`, `ACQUISITION_TARGET`, `EARNINGS_BLOWOUT` (>25% surprise), `REGULATORY_CLEARANCE`, `INDEX_ADDITION`, `DIVIDEND_SPECIAL_HUGE`, `MGMT_CHANGE_CEO_POSITIVE`.

**HYDRA_CATEGORIES** (catalyst + confirmation): everything else including `GUIDANCE_RAISE`, `CONTRACT_WIN_NORMAL`, `ANALYST_UPGRADE`, `DIVIDEND_REGULAR`, `ORDER_BOOK_UPDATE`, `MERGER_RUMOR`, all negative catalysts for shorts.

If **any** rule fails, the candidate goes to HYDRA. This is the "if not high enough, pass to HYDRA" contract: DAWN is a strict superset filter, HYDRA is the default.

### 2.4 Score-based override (edge case)

The 8-layer DAWN scoring (L1–L8) stays, but **after** the router. A candidate that passes R1–R6 still must hit `DAWN_CONVICTION ≥ 75` on the 8-layer scorer. Candidates that pass the router but fail scoring drop to HYDRA.

### 2.5 Confluence with HYDRA

A DAWN signal **also** stays in HYDRA's watchlist as a "HYDRA-SHADOW" entry. If DAWN fills at 9:15 and price moves against entry by 1.5%, HYDRA is already warmed up to consider a second entry on VWAP retest — no cold start.

---

## 3. HYDRA Intraday Alert System

### 3.1 The reliable, fastest approach

**Poll NSE corporate announcements every 30 seconds during 09:15–14:30 IST.**

Justification: NSE does not publish a public filings WebSocket. The only channels are:
- NSE corporate announcements page (polled HTML or JSON endpoint)
- BSE corporate announcements feed (polled)
- Paid services: Tickertape, Stockedge, Refinitiv — all poll-based under the hood
- Exchange tape — expensive and requires membership

30s poll = 570 polls per trading day, well under any reasonable rate limit. A real filing that hits NSE at 11:00:14 is detected by 11:00:44, evaluated by 11:01:30, order placed by 11:01:35. That 80-second latency is acceptable — retail can't beat that anyway, and the move on a real catalyst runs for 15–45 minutes.

### 3.2 Component design

```
IntradayFilingsWatcher (new)
├── Runs in dedicated daemon thread, started by runner at 09:15
├── Poll cycle:
│   1. GET https://www.nseindia.com/api/corporate-announcements?index=equities
│   2. Parse JSON → list of (symbol, filing_id, timestamp, subject, pdf_url)
│   3. Dedupe against _seen_filing_ids (reset daily)
│   4. For each new filing:
│      a. Call event_scanner.classify_single() — reuse existing Groq logic
│      b. If urgency ≥ 7.0 → push FilingEvent onto HYDRA queue
│      c. Else → log and discard
├── Failure handling: HTTP error → exponential backoff (30s → 60s → 120s, max 5 min)
├── Dedupe key: (symbol, filing_id) — survives restarts via on-disk set
```

### 3.3 HYDRA's response path

HYDRA maintains a small priority queue of incoming filings. On each orchestrator tick (every 60s):

1. Drain filing queue.
2. For each filing: fetch latest `TechnicalSnapshot` for the symbol.
3. Run `evaluate(entry, snapshot, depth_analysis)` immediately.
4. If `ConvictionScore.total ≥ 70` and SlotManager allows → submit trade.
5. If conviction ≥ 60 but < 70 → add to rolling watchlist, re-evaluate every 60s for next 10 minutes (catalyst might need TA to catch up).

### 3.4 Failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| NSE endpoint rate-limits us | Watcher blinded | Exponential backoff; fall back to BSE feed; alert via logger.error |
| NSE page structure changes | Parse fails silently | Schema validation; if parse fails 3× in a row → alert, suspend watcher |
| Groq API latency spike | Filings queue backs up | Bounded queue (100 items); drop oldest on overflow with warning |
| Duplicate filing with new ID | Trade placed twice | Dedupe not just by ID but by (symbol, subject_hash) within 30 min |
| Filing is a correction/withdrawal | Counter-trade | Classify step must detect "withdrawal" keyword → urgency = 0 |
| WebSocket disconnect during evaluation | Stale ticks | IntradayBus has reconnect logic; HYDRA skips evaluation if snapshot age > 90s |

### 3.5 What we are NOT doing and why

- **No Twitter/X scraping for breaking news.** Rumor-driven entries are strictly outside the mandate. If owner wants it, separate component with its own kill switch.
- **No Telegram channel ingestion.** Unverified, legally dubious, unstable.
- **No paid feeds initially.** NSE public endpoint is sufficient at 30s cadence. Revisit if we consistently lose to the tape by >2 minutes.

---

## 4. VIPER — Live Monitoring Analysis

### 4.1 Key finding from the audit

**Kite WebSocket is already integrated.** `runner.py` lines 236–264 already: starts `client.start_websocket()`, subscribes an initial universe, runs `BarBuilderThread` that builds 1m/5m bars from ticks, and pushes exit signals via a tick queue. The P1 push-tick pipeline achieves sub-millisecond tick latency.

The owner's question "should we use Kite WebSocket for VIPER" is therefore the wrong question. The real question is: **why is VIPER still doing 15-minute polled rescans (`VIPER_RESCAN_TIMES = [10:00, 10:30, 11:00, 12:00]`) when the tick infrastructure is already there?**

### 4.2 Options table (for completeness)

| Option | Latency | Cost | Reliability | Complexity | Verdict |
|---|---|---|---|---|---|
| **Kite WebSocket (existing)** | <1s tick | included | Auto-reconnect in market_live.py | Low (infra exists) | **Winner** |
| Polled `fetch_top_movers()` every 30s | 30–60s | Rate limit risk | Occasional stale | Medium | Inferior, redundant |
| Chartink webhooks | 30–120s | ₹2,000/mo subscription | External dependency | High (webhook server needed) | No — outside-the-fence, slower |
| Polygon/Alpha Vantage | 1–5s | $$$ (USD 100+/mo) | Good | Medium | Overpaid for Indian market |
| NSE NOW / member feeds | <100ms | requires membership | Best | Very high | Out of scope for retail setup |

### 4.3 VIPER tick-driven design

```
ViperTickHandler (new, subscribed to IntradayBus)
├── On startup (09:15):
│   - Load VIPER universe from pre_market_data: top 40 by (gap_pct | rel_volume | sector_momentum match)
│   - Subscribe these tokens to IntradayBus
│   - For each symbol, pre-compute from premarket cache:
│       * prev_day_high, prev_day_low, 30d_high
│       * avg_volume_20d (for true rel_volume, replaces the fake proxy)
│       * ATR_14
│
├── On each tick:
│   - Update per-symbol state (last_price, running_volume, VWAP from BarBuilder)
│   - Check breakout conditions (see §4.4)
│   - If breakout confirmed → evaluate() → conviction → submit
│
├── On each 1m bar close (event from BarBuilder):
│   - Recompute ORB if within first 15 min
│   - Update ADX, RSI (for confirmation filters)
│
├── Universe refresh: every 30 min, re-rank candidates from latest movers
```

### 4.4 Breakout definition (deterministic)

A tick triggers breakout evaluation when **all** are true:
1. `last_price > max(prev_day_high, 30d_high) × 1.001` (20 bps breakout buffer)
2. Last 1m bar volume / avg_1m_volume_20d ≥ 2.0 (**real** rel_volume, not the proxy)
3. `last_price > VWAP` (not a dead-cat above high)
4. `ADX_14 ≥ 20` (trend exists)
5. Within first 45 min OR within 15 min of ORB high break

Then the existing `viper.evaluate()` runs. Conviction scoring stays but uses the **real** `rel_volume` from pre-market + live BarBuilder — kills the `abs(pct_change)/2` proxy.

### 4.5 Failure modes and mitigations

| Failure | Mitigation |
|---|---|
| WebSocket disconnect | BarBuilder auto-reconnects; VIPER pauses new entries when `tick_age > 10s` |
| Tick flood during circuit halt | Per-symbol rate limit: max 1 evaluation per 3s |
| Universe too large (>200 symbols) | Kite WebSocket subscription cap is 3000; 40 symbols is trivial |
| Broker order rate limit (3/sec) | SlotManager queues; TradeExecutor paces submissions |
| Ghost fill from stale quote | TradeExecutor uses LIMIT orders with tight slippage band, not MARKET |

### 4.6 Integration steps

1. Remove `VIPER_RESCAN_TIMES` and the four 15-min polled scans from runner.
2. `runner` instantiates `ViperTickHandler` at 09:15, subscribes it to IntradayBus.
3. Delete the duplicate `fetch_top_movers()` call inside `viper.scan()` — consume universe from pre_market_data instead.
4. Keep `viper.evaluate()` but refactor to use real `rel_volume`.

---

## 5. Kill / Fix / Absorb Decisions

| Item | Current state | Verdict | Reason |
|---|---|---|---|
| `src/sniper/core.py` | Legacy v1 daily-bar rules; called only by EOD scripts (`log_daily_performance.py`, `daily_decision_engine.py`) | **KEEP** (not a production hot-path component; removing breaks EOD reports) | Audit was wrong to call it "dead" — grep confirms it is imported |
| `AntigravityWatcher` | VWAP-bounce state machine in `src/sniper/antigravity_watcher.py`; currently a filter layer | **ABSORB into VIPER** as an optional confirmation mode: after a breakout, if price pulls back to VWAP and bounces green → second-entry signal | VWAP-bounce is a VIPER-style move (technical, no catalyst). Having it as a separate module with state machine is overhead. Move into `viper_tick.py` as `check_vwap_bounce()` |
| `StockDiscovery` (V2) | Multi-source fusion fed by `fetch_top_movers()`, outputs feed `TechnicalScorer` then ConvictionEngine | **FIX** (not kill). Keep the fusion, but change the input: feed from pre_market_data + IntradayFilingsWatcher, not from fetch_top_movers. Its output *does* reach reports via ConvictionEngine — audit claim that it never appears in reports is outdated | Fusion logic is sound; the data pipes into it are wrong |
| `VIPER.volume_ratio = abs(pct_change)/2` | Fake proxy, acknowledged in warning log | **FIX** — read `rel_volume` from pre_market_data cache at scan time; use live per-minute rel volume from BarBuilder for tick-time decisions | Already have the real data; proxy only exists because VIPER doesn't currently consume the shared cache |
| HYDRA timing: `HYDRA_SCAN_TIME=08:15` but actually fires at 09:00 | Confusing dead constant | **FIX** — fire the pre-market scan at 08:15 immediately after pre_market_data cache is populated (08:00–08:05). Keep a second lightweight rescan at 09:00 for anything that landed in the 08:15–09:00 window | Owner wants earlier signal visibility; the cache is ready at 08:00 |
| Duplicate `fetch_top_movers()` (runner line 583 + viper.py `_fetch_movers()`) | Two full Kite batches at 09:30 | **FIX** — single call in runner; pass result into `viper.scan(movers=...)` as a parameter; remove internal fetch | Simple mechanical consolidation |
| Two EventScanner instances (HYDRA + DAWN) | Each constructs own instance | **FIX** — single instance lives inside pre_market_data module; both strategies read from the cached filings list | Saves API calls + keeps classification consistent |
| DAWN's 8-layer scoring vs ConvictionEngine's 5-layer | Two scoring systems | **KEEP BOTH** — DAWN's scoring is catalyst-specialized and lookback-heavy; ConvictionEngine is dynamic/intraday. Do not unify | Different time horizons need different math |
| `VIPER-COIL` dry-run counter-trend signals | Currently logged, never executed | **KEEP as-is** — valuable for feedback loop; owner can decide to promote to live later | No change needed |
| 15-min `VIPER_RESCAN_TIMES` polled scans | Redundant once tick-driven VIPER lives | **KILL** after ViperTickHandler is validated for a week | Don't delete on day one; run both in parallel for 5 trading days |

---

## 6. The Feedback Loop

The feedback loop is where the system improves. Each strategy has a different learning signal because each makes a different kind of decision.

### 6.1 Shared infrastructure

One EOD job at 16:30 IST: `FeedbackWriter.run()`. For every signal fired today (across all three strategies):
1. Fetch end-of-day close price + next-day open (stored from tomorrow's pre_market_data run, so feedback for day N lands in day N+1's morning report).
2. Classify outcome: `win` (≥+2%), `loss` (≤−1%), `neutral` (in between), `stopped` (SL hit intraday).
3. Append to the right pattern DB with full feature context.

### 6.2 DAWN feedback — "was the 9:15 entry right?"

Question: given the pre-market catalyst signature, did the stock actually move in the predicted direction by end of day?

DB schema (append-only JSON, `data/dawn_pattern_db.json`):
```
{
  "date": "YYYY-MM-DD",
  "symbol": "...",
  "direction": "LONG|SHORT",
  "catalyst_category": "FDA_APPROVAL",
  "filing_urgency": 9.2,
  "gap_pct_at_open": 4.1,
  "rel_volume_pre_open": 3.5,
  "entry_price": 1234.5,
  "close_price": 1289.2,
  "outcome_pct": 4.43,
  "stopped_out_intraday": false,
  "outcome_class": "win"
}
```

Learning surface:
- Monthly rollup: hit rate per `catalyst_category`. If `MGMT_CHANGE_CEO_POSITIVE` sits below 50% hit rate over 30 trades, **remove from DAWN_CATEGORIES** and demote to HYDRA.
- Tunable threshold: the R1 urgency threshold (currently 8.5) floats based on 30-day P&L curve. If last 30 days show net loss, raise by 0.5. If strong profit with few signals, lower by 0.5. Hard bounds: [7.5, 9.5].

### 6.3 HYDRA feedback — "did the confirmation improve my timing?"

Question: given a catalyst, is my TA-confirmed entry better than a blind 9:15 entry would have been?

This requires counterfactual tracking. For every HYDRA signal, also snapshot the hypothetical DAWN-style 9:15 open price.

```
hydra_entry_pct = (close - hydra_entry_price) / hydra_entry_price
dawn_shadow_pct = (close - open_at_9_15) / open_at_9_15
hydra_alpha     = hydra_entry_pct - dawn_shadow_pct   # positive → confirmation helped
```

DB stores `hydra_alpha` per trade. Weekly rollup: if `hydra_alpha < 0` over 30 trades on a specific catalyst category, that category should skip HYDRA confirmation and route to DAWN (or be skipped entirely).

Also: track **time-to-entry** after filing. If the best trades consistently entered within 5 minutes of filing, tighten HYDRA's evaluation cadence.

### 6.4 VIPER feedback — "was the breakout real?"

Question: when I fired on a breakout signal, did price continue or fake out?

For every VIPER entry, track: entry price, price 30 min later, price 60 min later, EOD close.

DB (`data/viper_pattern_db.json`) stores:
```
{
  "breakout_type": "prev_high | 30d_high | orb_high",
  "rel_volume_at_breakout": 2.8,
  "adx": 24,
  "sector_momentum": "HOT",
  "move_+30min_pct": 1.4,
  "move_+60min_pct": 0.2,
  "move_eod_pct": -0.8,
  "classification": "real | fakeout | range_trap"
}
```

Learning: breakouts with `adx < 22` and `sector_momentum == NEUTRAL` have high fakeout rate → raise ADX threshold. This tuning is automatic in the EOD job — it writes new thresholds to `config/viper_thresholds.json` and VIPER reads at next startup.

### 6.5 Hard rule across all three

No strategy can modify its own thresholds mid-day. Only the EOD FeedbackWriter writes to threshold files, and strategies read at startup (daily reset). This makes behavior reproducible and auditable.

---

## 7. What We Haven't Considered (Biggest Gaps)

Ordered by severity.

1. **Daily kill switch / PnL circuit breaker.** Fully automatic execution without a daily-loss hard cap is reckless. Recommend: if cumulative P&L ≤ −1.5% of deployed capital, freeze new entries for rest of day; at −2.5%, flatten all positions. Not currently implemented.

2. **Circuit breaker / trading halt awareness.** If a stock hits upper/lower circuit, tick flow freezes but last price stays stale. Without explicit halt detection, VIPER will keep evaluating with a frozen price. Need: detect via `last_tick_age > 120s AND last_price == circuit_limit` → force pause on that symbol.

3. **WebSocket reconnection blindspot.** During a reconnect (typical 3–30s), ticks are lost. If a breakout happens *during* the gap, we miss it permanently. Mitigation: after reconnect, re-poll last-minute bars for subscribed universe to fill the gap before resuming tick logic.

4. **Cross-strategy symbol collision.** DAWN fills RELIANCE at 9:15. At 11:00 a filing lands; HYDRA evaluates RELIANCE and wants to add. SlotManager must enforce: one active position per symbol, ever. The second signal is logged as "shadowed by DAWN entry, would-have conviction=82, actual entry delta +X.Y%" — this becomes a reinforcement signal for DAWN.

5. **Broker rate limits.** Kite: 3 orders/sec/user. TradeExecutor needs a token-bucket pacer. Without it, a multi-signal burst triggers rate-limit rejections.

6. **No backtesting framework.** All three strategies learn forward-only from live data. A quant firm would run a full bar-replay backtest over the last 2 years of NSE data to tune thresholds before production. VoltEdgeAI has none. This means every threshold is guessed on paper and validated in live risk. Recommend: after VIPER tick-driven lands, build a deterministic replay harness using cached 1m bars.

7. **Position sizing is unspecified.** The doc and audit both talk about conviction thresholds but not about how conviction maps to rupees. A 85-conviction DAWN signal vs a 70-conviction VIPER signal — do they get the same capital? Recommend: explicit sizing function in `risk.py`: `size(conviction, strategy, volatility) → ₹`.

8. **Overnight risk in DAWN.** DAWN hard-closes at 15:20, good. But what about gap-down-the-next-day if an entry was near EOD? Currently DAWN only operates intraday, so this is fine — but a future variant carrying overnight needs explicit gap risk modeling.

9. **LLM as decision authority.** Grok is wired in as conviction uplift (30% weighted). If Grok rate-limits or returns malformed JSON, the entire scoring silently collapses to the non-LLM 70%. This is fine for degraded operation but must be logged prominently; currently it is logged as `warning` and missed in daily ops.

10. **Timezone edge cases.** Code uses `zoneinfo.ZoneInfo("Asia/Kolkata")` correctly, but pre_market_data cache filenames use `YYYY-MM-DD` without a timezone — a 23:59 UTC call on 04-17 writes to a `2026-04-17.json` file that the 08:00 IST (02:30 UTC) run on 04-18 might read. Confirm all cache date keys use IST date, not UTC date.

11. **What a quant firm would do differently.**
   - Dedicated execution server co-located with broker (we don't need this for retail IST cadence).
   - Separate research env running the same strategies in simulation — continuous A/B comparison.
   - Formal risk dashboards (not just email reports). A real-time panel showing open positions, P&L, risk utilization — right now everything is logs + EOD email.
   - Kill-switch hotkeys for human override of "fully automatic" — in practice, every automated system I have seen in production needs a red button.

12. **"No human in the loop" is not a free dinner.** Fully automatic requires more defensive coding, not less. Every non-deterministic step (LLM classification, news parsing, filing dedupe) needs: typed output validation, fallback to safe default, logging, and an alert when fallbacks fire often.

---

## 8. Proposed Build Order

### Phase 1 — Plumbing the shared foundation (days 1–3)

Goal: three strategies read from one cache; duplicate fetches eliminated.

1. Wire HYDRA's `scan()` to `pre_market_data.fetch_all_premarket_data()`. Delete its `EventScanner` instance.
2. Wire DAWN's `pre_market_scan()` to the same. Delete its `EventScanner` instance and its independent yfinance calls.
3. Consolidate `fetch_top_movers()` — call once in runner at 09:30, pass into `viper.scan(movers=...)`.
4. Fix HYDRA's 08:15 timing: fire `hydra.scan()` at 08:15 using the cache (cache is ready by 08:05).
5. Fix VIPER's `volume_ratio` proxy: read `rel_volume` from `PreMarketSignals`.

**Validation:** compare today's signals before and after the change — they should be near-identical. Any divergence gets investigated before proceeding.

### Phase 2 — Router and DAWN/HYDRA boundary (days 4–5)

1. Build `DawnHydraRouter.route(signal) -> Strategy`.
2. HYDRA publishes its full watchlist; router extracts DAWN-eligible subset; DAWN scans only that subset.
3. Add shadow-entry tracking: every DAWN signal also becomes a HYDRA-SHADOW watchlist item.

**Validation:** on a paper day with strong catalysts, verify known FDA-approval / contract-win stocks route to DAWN and routine dividend-declarations route to HYDRA.

### Phase 3 — Intraday filings watcher (days 6–8)

1. Build `IntradayFilingsWatcher`.
2. Add a filing queue that HYDRA drains each orchestrator tick.
3. Dedupe, backoff, alerting.

**Validation:** kick off with a known past day (2026-04-15 had several intra-day filings); replay NSE endpoint with a mock and verify HYDRA receives each filing once and evaluates within 90s.

### Phase 4 — VIPER tick-driven (days 9–12)

1. Build `ViperTickHandler`; subscribe to existing IntradayBus.
2. Run in parallel with the old 15-min polled VIPER for 5 trading days (double logging, no double execution — only tick-driven executes).
3. Compare signal sets; if tick-driven produces a strict superset with no false positives vs polled, retire polled.

**Validation:** for each breakout signal fired by tick-driven VIPER, check that 30-min forward move is positive in ≥55% of cases on the sample week.

### Phase 5 — Feedback loop (days 13–15)

1. Implement `FeedbackWriter` EOD job.
2. Schema for each strategy's pattern DB; write one day's outcomes.
3. Build monthly rollup report that adjusts thresholds.

**Validation:** run on 30 days of historical trade logs; verify the threshold-adjustment rules produce sensible values (no extreme jumps).

### Phase 6 — Safety layer (days 16–17)

1. Daily PnL kill switch.
2. Broker rate-limit pacer.
3. WebSocket reconnect gap-fill.
4. Circuit-breaker halt detection.

**Validation:** fault-injection tests — simulate broker rejection, WS disconnect, circuit halt; verify system handles each gracefully.

### What to absolutely avoid

Do not rebuild ConvictionEngine. Do not rebuild SlotManager. Do not unify DAWN's 8-layer scoring with ConvictionEngine's 5-layer. Do not touch `runner.py`'s WebSocket/BarBuilder infrastructure — it works and moving it is the biggest risk in the codebase.

---

## 9. Open Questions (need owner decision)

1. **Capital sizing by conviction.** Do you want explicit bands (e.g., 85+ → 100%, 70–84 → 70%, as the rules file implies), or a continuous function? Current rules file states the banded version; confirm this is still the intent.

2. **DAWN live or dry-run?** The audit says DAWN is dry-run-only. The vision says "automatic market order at 9:15". When do we flip the live switch? Recommend: run DAWN dry-run for at least 30 trading days with the new router before going live, and only then with ≤ 10% of normal position size for another 30 days.

3. **VIPER-COIL promotion.** VIPER-COIL (counter-trend) currently dry-run-only. Do you want it promoted to live after the feedback loop validates it, or does COIL stay paper-only forever?

4. **How many DAWN signals per day, maximum?** Currently 5 per the code. Given the strict router rules, true DAWN-eligible days may have 0 or 1 candidates. Is 5 still right? Recommend: 3.

5. **Paid intraday filings feed?** NSE public endpoint at 30s cadence is the proposal. If you want faster, we need a paid feed — tell me the budget and I will compare vendors.

6. **Pre-market Tier1 expansion.** Currently pre_market_data Tier1 is filings with urgency ≥ 5. Should DAWN's router pull from a Tier0 subset (urgency ≥ 8) to avoid scoring the whole Tier1 list?

7. **Backtest investment.** Building a deterministic 2-year replay harness is ~2 weeks of work. Do you want to prioritize this before VIPER tick-driven goes live, or ship live and backfill backtest later?

8. **What counts as "fully automatic"?** My strong recommendation: even in fully-automatic mode, every live order emits a Telegram/email alert *before* submission (not after), with a 5-second human-overrideable delay. This is cheap, catches bugs, and does not cost you opportunity on normal trades.

---

## Verification plan (for the plan itself)

Before any code change lands, verify each claim above still holds:

1. `grep -rn "fetch_top_movers" src/` — confirm the two call sites.
2. `grep -rn "EventScanner" src/` — confirm the two instantiations in HYDRA and DAWN.
3. `grep -rn "volume_ratio" src/strategies/viper.py` — confirm the `abs(pct_change)/2` proxy.
4. `grep -rn "HYDRA_SCAN_TIME" src/runner.py` — confirm the 08:15 constant is not honored.
5. `grep -rn "start_websocket\|KiteTicker\|BarBuilder" src/` — confirm WebSocket is already wired.
6. `ls data/premarket_cache_*.json` — confirm pre-market cache is being written.

After Phase 1 lands, daily regression check:
- Run morning brief on a trading day; verify email subject, body, and number of signals match the previous implementation ±1 signal.
- `diff` the `data/conviction_watchboard_YYYY-MM-DD.json` structure — it should not change.

---

## Final word

This is not a rewrite. Seven files change materially (`runner.py`, `strategies/dawn.py`, `strategies/hydra.py`, `strategies/viper.py`, `data_ingestion/pre_market_data.py`, plus two new files: `strategies/router.py`, `data_ingestion/intraday_filings.py`). Everything else — ConvictionEngine, SlotManager, TradeExecutor, BarBuilder, ExitEngine, TechnicalBody — stays exactly where it is, because it works.

The key insight is that the architecture is 70% already there. The pain points (fake proxies, timing mismatches, duplicate fetches, standalone scoring) are patches-over-patches that accrued because the shared foundation (`pre_market_data.py`) was the last thing built, not the first. Now that it exists, the three strategies need to be bound to it, and a small amount of new machinery (router, filings watcher, tick handler, feedback writer) needs to be added. That's the work.

Smallest correct change. Preserve the system. Verify the result.
