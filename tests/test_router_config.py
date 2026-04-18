"""
test_router_config.py — Phase 4 router ↔ config.yaml integration.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import config_loader
from src.data_ingestion.pre_market_data import PreMarketSignals
from src.strategies.base import WatchlistEntry
from src.strategies.router import route_candidate

IST = ZoneInfo("Asia/Kolkata")


_BASE_CFG = """\
execution:
  dry_run: true
  max_trades_per_day: 5
  per_trade_risk_inr: 100000
  max_open_positions: 5
  conviction_threshold: 70

router:
  dawn_confidence_min: {dawn_min}
  hydra_confidence_min: 0.0
  router_enabled: {enabled}

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
"""


@pytest.fixture(autouse=True)
def _isolate_config():
    yield
    config_loader.load_config(force_reload=True)


def _write(tmp_path, monkeypatch, *, dawn_min: float = 0.85, enabled: bool = True):
    path = tmp_path / "config.yaml"
    path.write_text(
        _BASE_CFG.format(dawn_min=dawn_min, enabled=str(enabled).lower())
    )
    monkeypatch.setenv("VOLTEDGE_CONFIG", str(path))
    config_loader.load_config(force_reload=True)


def _dawn_eligible_entry() -> WatchlistEntry:
    return WatchlistEntry(
        symbol="RAILTEL",
        direction="BUY",
        filing_category="ORDER_WIN",
        filing_urgency=9.0,
        avg_volume_20d=1_000_000,
        filed_at=datetime.now(IST).isoformat(),
        gap_pct=3.5,
    )


def _dawn_eligible_premarket() -> PreMarketSignals:
    try:
        return PreMarketSignals(
            symbol="RAILTEL",
            as_of=datetime.now(IST),
            filing_category="ORDER_WIN",
            filing_urgency=9.0,
            filing_impact_score=8.5,
            gap_pct=3.5,
            avg_volume_20d=1_000_000,
        )
    except TypeError:
        pm = PreMarketSignals(symbol="RAILTEL")
        for k, v in dict(
            as_of=datetime.now(IST), filing_category="ORDER_WIN",
            filing_urgency=9.0, filing_impact_score=8.5,
            gap_pct=3.5, avg_volume_20d=1_000_000,
        ).items():
            setattr(pm, k, v)
        return pm


def test_router_disabled_routes_unrouted(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, enabled=False)
    entry = _dawn_eligible_entry()
    pm = _dawn_eligible_premarket()
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "UNROUTED"
    assert decision.confidence == 0.0
    assert entry.route == "UNROUTED"


def test_dawn_confidence_gate_demotes_to_hydra(tmp_path, monkeypatch):
    """dawn_confidence_min above achievable confidence → DAWN demoted to HYDRA."""
    _write(tmp_path, monkeypatch, dawn_min=1.01)
    entry = _dawn_eligible_entry()
    pm = _dawn_eligible_premarket()
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "HYDRA"
    assert "demoted" in decision.reasoning


def test_dawn_eligible_routes_to_dawn_when_enabled(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, dawn_min=0.85, enabled=True)
    entry = _dawn_eligible_entry()
    pm = _dawn_eligible_premarket()
    decision = route_candidate(entry, pm, pattern_db=None)
    assert decision.route == "DAWN"
    assert decision.confidence == 1.0
