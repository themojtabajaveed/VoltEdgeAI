"""
config_loader.py — thread-safe YAML config singleton.

Loads config.yaml from repo root. Typed accessors return values with
safe defaults if keys are missing so callers never crash on partial configs.
Validate with validate_config() for fail-loud sanity checks (raises ValueError).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CONFIG_LOCK = threading.Lock()
_CONFIG_CACHE: Optional[Dict[str, Any]] = None

_DEFAULTS: Dict[str, Any] = {
    "execution": {
        "dry_run": True,
        "max_trades_per_day": 5,
        "per_trade_risk_inr": 100000,
        "max_open_positions": 5,
        "conviction_threshold": 70,
    },
    "router": {
        "dawn_confidence_min": 0.85,
        "hydra_confidence_min": 0.0,
        "router_enabled": True,
    },
    "dawn": {
        "pre_market_scan_enabled": True,
        "scan_time_ist": "08:30",
        "select_time_ist": "08:45",
    },
    "hydra": {
        "scan_time_ist": "08:15",
        "shadows_persist": True,
        "shadows_dir": "data/",
    },
    "market": {
        "open_time_ist": "09:15",
        "close_time_ist": "15:30",
        "intraday_interval_minutes": 15,
    },
    "reporting": {
        "post_market_report_enabled": True,
        "router_performance_section": True,
        "email_enabled": True,
    },
    "logging": {
        "conviction_gate_log": True,
        "router_filter_log": True,
        "dry_run_log": True,
    },
    "counterfactual": {
        "target_pct": 2.0,
        "sl_pct": -1.5,
        "auto_run_post_market": True,
    },
    "backtest": {
        "default_days": 90,
        "target_pct": 2.0,
        "sl_pct": 1.5,
        "throttle_seconds": 0.35,
        "auto_run_weekly": False,
    },
}


def _config_path() -> Path:
    env_path = os.getenv("VOLTEDGE_CONFIG")
    if env_path:
        return Path(env_path)
    here = Path(__file__).resolve().parent.parent
    return here / "config.yaml"


def load_config(force_reload: bool = False) -> Dict[str, Any]:
    """Load config.yaml once, cache under lock. On error, fall back to defaults."""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is not None and not force_reload:
            return _CONFIG_CACHE
        path = _config_path()
        cfg: Dict[str, Any] = {}
        if path.exists():
            try:
                import yaml
                with open(path) as f:
                    loaded = yaml.safe_load(f) or {}
                if isinstance(loaded, dict):
                    cfg = loaded
            except Exception as e:
                logger.error(f"[config_loader] Failed to read {path}: {e} — using defaults")
        else:
            logger.warning(f"[config_loader] {path} not found — using defaults")
        merged = _deep_merge(_DEFAULTS, cfg)
        _CONFIG_CACHE = merged
        return _CONFIG_CACHE


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _require(cfg: Dict[str, Any], section: str, key: str) -> Any:
    return cfg.get(section, {}).get(key)


def validate_config(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """
    Validate critical config invariants. Raises ValueError on violation so
    the service fails loudly instead of trading with bad parameters.
    """
    cfg = cfg if cfg is not None else load_config()

    ct = _require(cfg, "execution", "conviction_threshold")
    if not isinstance(ct, int) or isinstance(ct, bool) or not (50 <= ct <= 100):
        raise ValueError(
            f"execution.conviction_threshold={ct!r} invalid — must be int in [50, 100]"
        )

    mt = _require(cfg, "execution", "max_trades_per_day")
    if not isinstance(mt, int) or isinstance(mt, bool) or not (1 <= mt <= 20):
        raise ValueError(
            f"execution.max_trades_per_day={mt!r} invalid — must be int in [1, 20]"
        )

    ptr = _require(cfg, "execution", "per_trade_risk_inr")
    if not isinstance(ptr, (int, float)) or isinstance(ptr, bool) or ptr <= 0:
        raise ValueError(
            f"execution.per_trade_risk_inr={ptr!r} invalid — must be float > 0"
        )

    mop = _require(cfg, "execution", "max_open_positions")
    if not isinstance(mop, int) or isinstance(mop, bool) or not (1 <= mop <= 20):
        raise ValueError(
            f"execution.max_open_positions={mop!r} invalid — must be int in [1, 20]"
        )

    dry = _require(cfg, "execution", "dry_run")
    if not isinstance(dry, bool):
        raise ValueError(f"execution.dry_run={dry!r} invalid — must be bool")

    dcm = _require(cfg, "router", "dawn_confidence_min")
    if not isinstance(dcm, (int, float)) or isinstance(dcm, bool) or not (0.0 <= dcm <= 1.0):
        raise ValueError(
            f"router.dawn_confidence_min={dcm!r} invalid — must be float in [0.0, 1.0]"
        )

    hcm = _require(cfg, "router", "hydra_confidence_min")
    if not isinstance(hcm, (int, float)) or isinstance(hcm, bool) or not (0.0 <= hcm <= 1.0):
        raise ValueError(
            f"router.hydra_confidence_min={hcm!r} invalid — must be float in [0.0, 1.0]"
        )

    cf = cfg.get("counterfactual", {})
    for k in ("target_pct", "sl_pct"):
        v = cf.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"counterfactual.{k}={v!r} invalid — must be numeric")
    if not isinstance(cf.get("auto_run_post_market"), bool):
        raise ValueError(
            f"counterfactual.auto_run_post_market={cf.get('auto_run_post_market')!r} "
            "invalid — must be bool"
        )

    return True


# ── Typed accessors — existing (Phase 5) ───────────────────────────────────

def get_cf_target_pct() -> float:
    return float(load_config().get("counterfactual", {}).get("target_pct", 2.0))


def get_cf_sl_pct() -> float:
    return float(load_config().get("counterfactual", {}).get("sl_pct", -1.5))


def get_cf_auto_run() -> bool:
    return bool(load_config().get("counterfactual", {}).get("auto_run_post_market", True))


def get_router_dawn_confidence_min() -> float:
    return float(load_config().get("router", {}).get("dawn_confidence_min", 0.85))


# ── Typed accessors — execution ────────────────────────────────────────────

def get_conviction_threshold() -> int:
    return int(load_config().get("execution", {}).get("conviction_threshold", 70))


def get_max_trades() -> int:
    return int(load_config().get("execution", {}).get("max_trades_per_day", 5))


def get_per_trade_risk() -> float:
    return float(load_config().get("execution", {}).get("per_trade_risk_inr", 100000))


def get_max_open_positions() -> int:
    return int(load_config().get("execution", {}).get("max_open_positions", 5))


def get_dry_run() -> bool:
    return bool(load_config().get("execution", {}).get("dry_run", True))


# ── Typed accessors — router ───────────────────────────────────────────────

def get_router_enabled() -> bool:
    return bool(load_config().get("router", {}).get("router_enabled", True))


def get_hydra_confidence_min() -> float:
    return float(load_config().get("router", {}).get("hydra_confidence_min", 0.0))


# ── Typed accessors — hydra ────────────────────────────────────────────────

def get_shadows_persist() -> bool:
    return bool(load_config().get("hydra", {}).get("shadows_persist", True))


def get_shadows_dir() -> str:
    return str(load_config().get("hydra", {}).get("shadows_dir", "data/"))


# ── Typed accessors — reporting ────────────────────────────────────────────

def get_router_performance_section() -> bool:
    return bool(load_config().get("reporting", {}).get("router_performance_section", True))


def get_email_enabled() -> bool:
    return bool(load_config().get("reporting", {}).get("email_enabled", True))


# ── Typed accessors — logging flags ────────────────────────────────────────

def get_conviction_gate_log() -> bool:
    return bool(load_config().get("logging", {}).get("conviction_gate_log", True))


def get_router_filter_log() -> bool:
    return bool(load_config().get("logging", {}).get("router_filter_log", True))


def get_dry_run_log() -> bool:
    return bool(load_config().get("logging", {}).get("dry_run_log", True))


# ── Typed accessors — backtest ─────────────────────────────────────────────

def get_backtest_days() -> int:
    return int(load_config().get("backtest", {}).get("default_days", 90))


def get_backtest_target_pct() -> float:
    return float(load_config().get("backtest", {}).get("target_pct", 2.0))


def get_backtest_sl_pct() -> float:
    return float(load_config().get("backtest", {}).get("sl_pct", 1.5))


def get_backtest_throttle() -> float:
    return float(load_config().get("backtest", {}).get("throttle_seconds", 0.35))


def get_backtest_auto_run_weekly() -> bool:
    return bool(load_config().get("backtest", {}).get("auto_run_weekly", False))
