"""Self-contained configuration for the event-intelligence pipeline.

Reads from environment (`.env` loaded by python-dotenv elsewhere) plus a
small defaults dict. Kept separate from `src/config_loader.py` to avoid
collisions with in-flight changes there. All values are read once on
import; restart the service to pick up env changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EventIntelConfig:
    # TrueData
    truedata_api_key: str
    truedata_ws_url: str
    truedata_reconnect_initial_s: float = 5.0
    truedata_reconnect_max_s: float = 300.0

    # NSE/BSE verifier
    verifier_poll_initial_s: float = 30.0
    verifier_poll_followup_s: float = 60.0
    verifier_timeout_s: float = 1200.0   # 20 minutes

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-pro"
    gemini_daily_budget: int = 1000
    gemini_request_timeout_s: float = 30.0

    # Pipeline
    queue_max_items: int = 1000
    data_dir: str = "data"
    log_dir: str = "logs/event_intel"

    # Soak / behavior flags
    shadow_only: bool = True             # v1: never write to live router path

    # Plan B — source selection
    source_mode: str = "official"        # "official" | "truedata" | "dual"
    bse_enabled: bool = True             # OfficialClient runs BSEPoller alongside NSE


def _getenv(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _getenv_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _getenv_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def load_config() -> EventIntelConfig:
    """Read configuration from environment variables."""
    return EventIntelConfig(
        truedata_api_key=_getenv("TRUEDATA_API_KEY"),
        truedata_ws_url=_getenv("TRUEDATA_WS_URL"),
        truedata_reconnect_initial_s=_getenv_float("EVENT_INTEL_TD_RECONNECT_INITIAL_S", 5.0),
        truedata_reconnect_max_s=_getenv_float("EVENT_INTEL_TD_RECONNECT_MAX_S", 300.0),
        verifier_poll_initial_s=_getenv_float("EVENT_INTEL_VERIFIER_POLL_INITIAL_S", 30.0),
        verifier_poll_followup_s=_getenv_float("EVENT_INTEL_VERIFIER_POLL_FOLLOWUP_S", 60.0),
        verifier_timeout_s=_getenv_float("EVENT_INTEL_VERIFIER_TIMEOUT_S", 1200.0),
        gemini_api_key=_getenv("GEMINI_API_KEY"),
        gemini_model=_getenv("EVENT_INTEL_GEMINI_MODEL", "gemini-2.5-pro"),
        gemini_daily_budget=_getenv_int("EVENT_INTEL_GEMINI_DAILY_BUDGET", 1000),
        gemini_request_timeout_s=_getenv_float("EVENT_INTEL_GEMINI_TIMEOUT_S", 30.0),
        queue_max_items=_getenv_int("EVENT_INTEL_QUEUE_MAX", 1000),
        data_dir=_getenv("EVENT_INTEL_DATA_DIR", "data"),
        log_dir=_getenv("EVENT_INTEL_LOG_DIR", "logs/event_intel"),
        shadow_only=_getenv("EVENT_INTEL_SHADOW_ONLY", "true").lower() != "false",
        source_mode=_getenv("EVENT_INTEL_SOURCE_MODE", "official").lower(),
        bse_enabled=_getenv("EVENT_INTEL_BSE_ENABLED", "true").lower() != "false",
    )
