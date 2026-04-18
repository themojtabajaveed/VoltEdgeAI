"""
test_config_loader.py — Phase 4 config loader unit tests.
"""
import pytest

from src import config_loader


EXPECTED_CONFIG = """\
execution:
  dry_run: true
  max_trades_per_day: 5
  per_trade_risk_inr: 100000
  max_open_positions: 5
  conviction_threshold: 70

router:
  dawn_confidence_min: 0.85
  hydra_confidence_min: 0.0
  router_enabled: true

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


@pytest.fixture
def full_cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(EXPECTED_CONFIG)
    monkeypatch.setenv("VOLTEDGE_CONFIG", str(path))
    config_loader.load_config(force_reload=True)
    yield
    config_loader.load_config(force_reload=True)


def _write_cfg(tmp_path, monkeypatch, content: str):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    monkeypatch.setenv("VOLTEDGE_CONFIG", str(path))
    config_loader.load_config(force_reload=True)


# ── Load + structure ─────────────────────────────────────────────────────

def test_load_config_has_all_eight_sections(full_cfg):
    cfg = config_loader.load_config()
    for section in (
        "execution", "router", "dawn", "hydra",
        "market", "reporting", "logging", "counterfactual",
    ):
        assert section in cfg, f"missing section: {section}"


def test_get_conviction_threshold_is_70(full_cfg):
    assert config_loader.get_conviction_threshold() == 70


def test_get_dry_run_is_true(full_cfg):
    assert config_loader.get_dry_run() is True


def test_get_max_trades_is_5(full_cfg):
    assert config_loader.get_max_trades() == 5


def test_get_per_trade_risk_is_100000(full_cfg):
    assert config_loader.get_per_trade_risk() == 100000.0


def test_get_router_enabled_true(full_cfg):
    assert config_loader.get_router_enabled() is True


def test_get_shadows_persist_true(full_cfg):
    assert config_loader.get_shadows_persist() is True


def test_get_email_enabled_true(full_cfg):
    assert config_loader.get_email_enabled() is True


# ── validate_config: raise on bad values ─────────────────────────────────

def test_validate_rejects_low_conviction_threshold(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, EXPECTED_CONFIG.replace(
        "conviction_threshold: 70", "conviction_threshold: 45"))
    with pytest.raises(ValueError, match="conviction_threshold"):
        config_loader.validate_config()
    config_loader.load_config(force_reload=True)


def test_validate_rejects_dawn_confidence_above_one(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, EXPECTED_CONFIG.replace(
        "dawn_confidence_min: 0.85", "dawn_confidence_min: 1.5"))
    with pytest.raises(ValueError, match="dawn_confidence_min"):
        config_loader.validate_config()
    config_loader.load_config(force_reload=True)


def test_validate_rejects_zero_max_trades(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, EXPECTED_CONFIG.replace(
        "max_trades_per_day: 5", "max_trades_per_day: 0"))
    with pytest.raises(ValueError, match="max_trades_per_day"):
        config_loader.validate_config()
    config_loader.load_config(force_reload=True)


# ── Defaults + singleton ─────────────────────────────────────────────────

def test_load_config_missing_file_uses_defaults(tmp_path, monkeypatch):
    """Nonexistent config path → defaults returned, no crash."""
    monkeypatch.setenv("VOLTEDGE_CONFIG", str(tmp_path / "does_not_exist.yaml"))
    config_loader.load_config(force_reload=True)
    cfg = config_loader.load_config()
    assert cfg["execution"]["conviction_threshold"] == 70
    assert cfg["router"]["router_enabled"] is True
    config_loader.load_config(force_reload=True)


def test_load_config_is_singleton(full_cfg):
    a = config_loader.load_config()
    b = config_loader.load_config()
    assert id(a) == id(b)


# ── Additional accessor coverage ─────────────────────────────────────────

def test_accessors_consistent(full_cfg):
    assert config_loader.get_max_open_positions() == 5
    assert config_loader.get_hydra_confidence_min() == 0.0
    assert config_loader.get_shadows_dir() == "data/"
    assert config_loader.get_router_performance_section() is True
    assert config_loader.get_conviction_gate_log() is True
    assert config_loader.get_router_filter_log() is True
    assert config_loader.get_dry_run_log() is True
    assert config_loader.get_router_dawn_confidence_min() == 0.85
