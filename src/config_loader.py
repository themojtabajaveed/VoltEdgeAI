"""
config_loader.py — thread-safe YAML config singleton.

Loads config.yaml from repo root. Typed accessors return values with
safe defaults if keys are missing so callers never crash on partial configs.
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
    "router": {
        "dawn_confidence_min": 0.85,
    },
    "counterfactual": {
        "target_pct": 2.0,
        "sl_pct": -1.5,
        "auto_run_post_market": True,
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


def validate_config(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Light sanity check on the counterfactual + router sections."""
    cfg = cfg if cfg is not None else load_config()
    try:
        cf = cfg.get("counterfactual", {})
        if not isinstance(cf.get("target_pct"), (int, float)):
            return False
        if not isinstance(cf.get("sl_pct"), (int, float)):
            return False
        if not isinstance(cf.get("auto_run_post_market"), bool):
            return False
        r = cfg.get("router", {})
        if not isinstance(r.get("dawn_confidence_min"), (int, float)):
            return False
    except Exception:
        return False
    return True


# ── Typed accessors ────────────────────────────────────────────────────────

def get_cf_target_pct() -> float:
    return float(load_config().get("counterfactual", {}).get("target_pct", 2.0))


def get_cf_sl_pct() -> float:
    return float(load_config().get("counterfactual", {}).get("sl_pct", -1.5))


def get_cf_auto_run() -> bool:
    return bool(load_config().get("counterfactual", {}).get("auto_run_post_market", True))


def get_router_dawn_confidence_min() -> float:
    return float(load_config().get("router", {}).get("dawn_confidence_min", 0.85))
