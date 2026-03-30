# Architecture Rules — VoltEdgeAI

## Dragon Architecture

- All strategy heads MUST inherit from `StrategyHead` (in `src/strategies/base.py`)
- Each head MUST implement: `scan() → List[WatchlistEntry]` and `evaluate() → ConvictionScore`
- Heads share a single `TechnicalBody` instance — DO NOT recompute TA inside heads
- Heads communicate through `SlotManager` only — no direct cross-head calls
- New strategy heads follow the pattern: `src/strategies/{name}.py` with `{Name}Strategy` class

## Module Boundaries

- `strategies/` — trade decision logic ONLY (no broker calls)
- `trading/` — execution and risk management ONLY (no LLM calls)
- `data_ingestion/` — external data fetching ONLY (no trading logic)
- `llm/` — LLM API clients ONLY (no business logic in prompts beyond scoring)
- `reports/` — read-only analysis and reporting (no state mutations)
- `runner.py` — orchestrator that wires everything together

## Adding New Components

### New Strategy Head
1. Create `src/strategies/{name}.py` with class inheriting `StrategyHead`
2. Implement `scan()` and `evaluate()` methods
3. Add `reset_daily()` override if head has custom state
4. Wire into `runner.py`: instantiate, call in market loop, handle exits
5. Register with `SlotManager` using strategy name string

### New Data Source
1. Create `src/data_ingestion/{source}_client.py`
2. Return structured dataclass objects, not raw dicts
3. Handle rate limiting internally
4. Cache results where appropriate (see `market_history.py` for SQLite cache pattern)

### New LLM Integration
1. Create `src/llm/{provider}_client.py`
2. Follow the lazy-init pattern (`_get_client()` function)
3. Always parse JSON response with fallback regex extraction
4. Strip `<think>` blocks from reasoning models
5. Document daily budget in CLAUDE.md

## Runner Architecture

- `runner.py` is a `while True` loop with 60-second sleep
- Scheduled jobs use `_should_fire_scheduled_job()` with cascade prevention
- Daily state resets happen when `current_date != risk_state.trading_date`
- WebSocket runs in a separate daemon thread (`bar_builder_worker`)
