"""
test_counterfactual.py — Phase 5 counterfactual engine unit tests.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.analysis import counterfactual as cf


@pytest.fixture(autouse=True)
def _reload_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "router:\n  dawn_confidence_min: 0.85\n"
        "counterfactual:\n  target_pct: 2.0\n  sl_pct: -1.5\n  auto_run_post_market: true\n"
    )
    monkeypatch.setenv("VOLTEDGE_CONFIG", str(cfg_path))
    from src import config_loader
    config_loader.load_config(force_reload=True)
    yield
    config_loader.load_config(force_reload=True)


def _shadow(symbol="ABC", confidence=0.5):
    return {
        "symbol": symbol,
        "confidence": confidence,
        "route": "HYDRA",
        "filing_category": "PRESS_RELEASE",
    }


def _eod(symbol="ABC", open_p=100.0, close_p=100.0, date_str="2026-04-18"):
    return {
        "symbol": symbol,
        "date": date_str,
        "open": open_p,
        "high": max(open_p, close_p),
        "low": min(open_p, close_p),
        "close": close_p,
        "volume": 100000,
    }


def test_score_shadow_missed_opportunity():
    """pct_move = +3.0 → MISSED_OPPORTUNITY."""
    scored = cf.score_shadow(_shadow(), _eod(open_p=100.0, close_p=103.0))
    assert scored["pct_move"] == pytest.approx(3.0, abs=0.01)
    assert scored["direction"] == "UP"
    assert scored["would_have_hit_target"] is True
    assert scored["would_have_hit_sl"] is False
    assert scored["verdict"] == "MISSED_OPPORTUNITY"


def test_score_shadow_correct_route():
    """pct_move = -2.0 → CORRECT_ROUTE."""
    scored = cf.score_shadow(_shadow(), _eod(open_p=100.0, close_p=98.0))
    assert scored["pct_move"] == pytest.approx(-2.0, abs=0.01)
    assert scored["direction"] == "DOWN"
    assert scored["would_have_hit_sl"] is True
    assert scored["verdict"] == "CORRECT_ROUTE"


def test_score_shadow_neutral():
    """pct_move = +0.5 → NEUTRAL."""
    scored = cf.score_shadow(_shadow(), _eod(open_p=100.0, close_p=100.5))
    assert scored["verdict"] == "NEUTRAL"
    assert scored["would_have_hit_target"] is False
    assert scored["would_have_hit_sl"] is False


def test_run_counterfactual_zero_shadows(monkeypatch, tmp_path):
    """0 shadows → router_accuracy=0.0, no crash."""
    monkeypatch.setattr(cf, "_DATA_DIR", tmp_path)
    result = cf.run_counterfactual("2026-04-18")
    assert result["total_shadows"] == 0
    assert result["router_accuracy"] == 0.0
    assert result["details"] == []


def test_load_shadows_missing_file(monkeypatch, tmp_path):
    """Missing shadow file → [] without crashing."""
    monkeypatch.setattr(cf, "_DATA_DIR", tmp_path)
    assert cf.load_shadows("2026-04-18") == []


def test_run_counterfactual_summary_counts(monkeypatch, tmp_path):
    """3 shadows: 1 MISSED, 1 CORRECT, 1 NEUTRAL → accuracy=1/3."""
    monkeypatch.setattr(cf, "_DATA_DIR", tmp_path)
    date_str = "2026-04-18"
    shadows = [_shadow("AAA"), _shadow("BBB"), _shadow("CCC")]
    (tmp_path / f"hydra_shadows_{date_str}.json").write_text(json.dumps(shadows))

    eod_map = {
        "AAA": _eod("AAA", 100.0, 103.0, date_str),  # +3% MISSED_OPPORTUNITY
        "BBB": _eod("BBB", 100.0, 98.0, date_str),   # -2% CORRECT_ROUTE
        "CCC": _eod("CCC", 100.0, 100.5, date_str),  # +0.5% NEUTRAL
    }

    def _fake_fetch(symbol: str, date_str_: str):
        return eod_map[symbol]

    with patch.object(cf, "fetch_eod_result", side_effect=_fake_fetch):
        result = cf.run_counterfactual(date_str)

    assert result["total_shadows"] == 3
    assert result["missed_opportunities"] == 1
    assert result["correct_routes"] == 1
    assert result["neutral"] == 1
    assert result["router_accuracy"] == pytest.approx(1 / 3, abs=1e-4)
    assert len(result["details"]) == 3


def test_score_shadow_fetch_failed_does_not_crash():
    """EOD fetch error → verdict UNKNOWN, no exception."""
    scored = cf.score_shadow(_shadow(), {"symbol": "ABC", "error": "fetch_failed"})
    assert scored["verdict"] == "UNKNOWN"
    assert scored["pct_move"] is None


def test_persist_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "_DATA_DIR", tmp_path)
    summary = {
        "date": "2026-04-18",
        "total_shadows": 1,
        "correct_routes": 1,
        "missed_opportunities": 0,
        "neutral": 0,
        "router_accuracy": 1.0,
        "details": [],
    }
    path = cf.persist_counterfactual(summary)
    assert path is not None
    loaded = cf.load_counterfactual("2026-04-18")
    assert loaded == summary
