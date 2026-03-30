# Coding Style Rules — VoltEdgeAI

## Python Style

- **Python 3.11+** — use modern syntax (match/case OK, `type[X]` OK)
- **Type hints on all public functions** — return types are mandatory
- Private methods: prefix with `_`
- Use `@dataclass` for structured data containers, not raw dicts
- Use `Enum` for fixed sets of values (see `MoveType`, `TradeMode`)

## Naming

- Files: `snake_case.py` → `{domain}_{function}.py`
- Classes: `PascalCase` → `HydraStrategy`, `TechnicalBody`
- Functions: `snake_case` → `compute_atr()`, `should_allow_new_entry()`
- Constants: `UPPER_SNAKE` → `MARKET_START`, `CONFLUENCE_BONUS`
- Booleans: prefix `is_`, `should_`, `has_`, `meets_`

## Error Handling

- **Every external call** (API, file I/O, LLM) must be wrapped in try-except
- **Never crash the runner** — log the error, return a safe default, continue
- Pattern:
  ```python
  try:
      result = external_call()
  except Exception as e:
      logging.error(f"Context: {e}")
      result = safe_default
  ```

## Logging

- Module-level: `logger = logging.getLogger(__name__)`
- Strategy heads prefix: `[HYDRA]`, `[VIPER]`, `[SlotManager]`
- Runner uses `print()` with IST timestamps + emoji for operator visibility
- Both `print()` AND `logging.info()` for important events

## Imports

- Order: stdlib → third-party → `src.*`
- Lazy imports for heavy/optional deps (inside functions):
  ```python
  def my_func():
      from src.llm.grok_client import grok_conviction_analysis
  ```

## Data Flow

- Always pass structured dataclass objects between modules, not raw dicts
- Pipeline: `MarketEvent → WatchlistEntry → ConvictionScore → TradeSlot`
- Scoring objects must have `.to_dict()` for serialization
