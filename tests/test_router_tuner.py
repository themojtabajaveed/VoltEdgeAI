"""
test_router_tuner.py — Phase 5 router tuner unit tests.
"""
import json
from pathlib import Path

import pytest

from src.analysis import router_tuner as rt


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


def _cf(date, accuracy, missed, correct, neutral=0):
    return {
        "date": date,
        "total_shadows": correct + missed + neutral,
        "correct_routes": correct,
        "missed_opportunities": missed,
        "neutral": neutral,
        "router_accuracy": accuracy,
        "details": [],
    }


def test_no_change_when_accuracy_high():
    history = [_cf("2026-04-14", 0.80, 1, 4),
               _cf("2026-04-15", 0.85, 0, 5),
               _cf("2026-04-16", 0.75, 1, 3)]
    sugg = rt.compute_tuning_suggestion(history)
    assert sugg["action"] == "NO_CHANGE"
    assert sugg["suggested_value"] == sugg["current_value"]
    assert sugg["avg_router_accuracy"] >= 0.70


def test_lower_dawn_confidence_when_missed_dominates():
    history = [_cf("2026-04-14", 0.50, 5, 1),
               _cf("2026-04-15", 0.60, 4, 2),
               _cf("2026-04-16", 0.70, 3, 2)]
    sugg = rt.compute_tuning_suggestion(history)
    assert sugg["action"] == "LOWER_DAWN_CONFIDENCE"
    assert sugg["suggested_value"] == pytest.approx(sugg["current_value"] - 0.05, abs=1e-6)
    assert sugg["total_missed"] > sugg["total_correct"]


def test_raise_dawn_confidence_when_correct_dominates():
    history = [_cf("2026-04-14", 0.50, 1, 5),
               _cf("2026-04-15", 0.60, 2, 4),
               _cf("2026-04-16", 0.55, 0, 3)]
    sugg = rt.compute_tuning_suggestion(history)
    assert sugg["action"] == "RAISE_DAWN_CONFIDENCE"
    assert sugg["suggested_value"] == pytest.approx(sugg["current_value"] + 0.05, abs=1e-6)
    assert sugg["total_correct"] >= sugg["total_missed"]


def test_load_cf_history_no_files(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "_DATA_DIR", tmp_path)
    assert rt.load_cf_history(days=5) == []


def test_load_cf_history_reads_recent_files(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "_DATA_DIR", tmp_path)
    for day in ("2026-04-14", "2026-04-15", "2026-04-16"):
        (tmp_path / f"counterfactual_{day}.json").write_text(json.dumps(_cf(day, 0.8, 1, 4)))
    hist = rt.load_cf_history(days=5)
    assert [h["date"] for h in hist] == ["2026-04-14", "2026-04-15", "2026-04-16"]


def test_empty_history_returns_no_change():
    sugg = rt.compute_tuning_suggestion([])
    assert sugg["action"] == "NO_CHANGE"
    assert sugg["days_analyzed"] == 0
