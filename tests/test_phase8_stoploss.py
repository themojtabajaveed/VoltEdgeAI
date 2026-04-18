"""
test_phase8_stoploss.py — Phase 8 stoploss config + accessors + validation.
"""
import pytest

from src import config_loader


FULL_CFG = """\
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

counterfactual:
  target_pct: 2.0
  sl_pct: -1.5
  auto_run_post_market: true

stoploss:
  daily_loss_halt_multiplier: 3.0
  daily_loss_flatten_multiplier: 5.0
  max_stop_pct: 2.5
  conviction_modifiers:
    high_threshold: 85
    high_multiplier: 1.2
    low_threshold: 75
    low_multiplier: 0.8
  by_strategy:
    DAWN:
      initial_atr_multiplier: 1.5
      orb_stop_enabled: true
      breakeven_r: 1.0
      trail_atr_settle: 2.0
      trail_atr_confirm: 1.5
      trail_atr_lock: 1.2
      trail_atr_accelerate: 0.75
      partials_enabled: true
      partial_1_r: 1.5
      partial_1_pct: 0.50
      partial_2_r: 2.5
      partial_2_pct: 0.25
      time_stop_ist: "13:30"
      rr_target: 2.5
    HYDRA:
      initial_atr_multiplier: 1.2
      orb_stop_enabled: false
      breakeven_r: 1.0
      trail_atr_settle: 2.0
      trail_atr_confirm: 1.5
      trail_atr_lock: 1.0
      trail_atr_accelerate: 0.75
      partials_enabled: true
      partial_1_r: 1.5
      partial_1_pct: 0.50
      partial_2_r: 2.5
      partial_2_pct: 0.25
      time_stop_ist: "12:30"
      rr_target: 1.8
    VIPER:
      initial_atr_multiplier: 1.5
      orb_stop_enabled: false
      breakeven_r: 1.0
      trail_atr_settle: 2.0
      trail_atr_confirm: 1.5
      trail_atr_lock: 1.0
      trail_atr_accelerate: 0.75
      partials_enabled: true
      partial_1_r: 2.0
      partial_1_pct: 0.50
      partial_2_r: 3.0
      partial_2_pct: 0.25
      time_stop_ist: "15:20"
      rr_target: 2.5
"""


@pytest.fixture
def full_cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(FULL_CFG)
    monkeypatch.setenv("VOLTEDGE_CONFIG", str(path))
    config_loader.load_config(force_reload=True)
    yield
    config_loader.load_config(force_reload=True)


def _write_cfg(tmp_path, monkeypatch, content: str):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    monkeypatch.setenv("VOLTEDGE_CONFIG", str(path))
    config_loader.load_config(force_reload=True)


# ── Phase 8a: config load + accessor tests ───────────────────────────────

def test_config_has_stoploss_section(full_cfg):
    cfg = config_loader.load_config()
    assert "stoploss" in cfg
    assert "by_strategy" in cfg["stoploss"]
    assert set(cfg["stoploss"]["by_strategy"].keys()) == {"DAWN", "HYDRA", "VIPER"}


def test_get_stoploss_config_dawn(full_cfg):
    cfg = config_loader.get_stoploss_config("DAWN")
    assert cfg["initial_atr_multiplier"] == 1.5
    assert cfg["orb_stop_enabled"] is True
    assert cfg["partial_1_r"] == 1.5
    assert cfg["time_stop_ist"] == "13:30"


def test_get_stoploss_config_hydra(full_cfg):
    cfg = config_loader.get_stoploss_config("HYDRA")
    assert cfg["initial_atr_multiplier"] == 1.2
    assert cfg["orb_stop_enabled"] is False
    assert cfg["rr_target"] == 1.8


def test_get_stoploss_config_viper_legacy_values(full_cfg):
    """VIPER params must match legacy values (no behavior regression)."""
    cfg = config_loader.get_stoploss_config("VIPER")
    assert cfg["partial_1_r"] == 2.0
    assert cfg["partial_2_r"] == 3.0
    assert cfg["time_stop_ist"] == "15:20"


def test_get_stoploss_config_case_insensitive(full_cfg):
    cfg = config_loader.get_stoploss_config("dawn")
    assert cfg["orb_stop_enabled"] is True


def test_get_stoploss_config_unknown_route_falls_back_to_viper(full_cfg):
    cfg = config_loader.get_stoploss_config("UNKNOWN_STRATEGY")
    viper = config_loader.get_stoploss_config("VIPER")
    assert cfg == viper


def test_get_stoploss_config_returns_fresh_copy(full_cfg):
    a = config_loader.get_stoploss_config("DAWN")
    a["initial_atr_multiplier"] = 999.0
    b = config_loader.get_stoploss_config("DAWN")
    assert b["initial_atr_multiplier"] == 1.5


def test_daily_loss_multipliers(full_cfg):
    assert config_loader.get_daily_loss_halt_multiplier() == 3.0
    assert config_loader.get_daily_loss_flatten_multiplier() == 5.0


def test_max_stop_pct(full_cfg):
    assert config_loader.get_max_stop_pct() == 2.5


def test_conviction_modifiers(full_cfg):
    cm = config_loader.get_conviction_modifiers()
    assert cm["high_threshold"] == 85
    assert cm["high_multiplier"] == 1.2
    assert cm["low_threshold"] == 75
    assert cm["low_multiplier"] == 0.8


def test_apply_conviction_modifier_high(full_cfg):
    # conviction >= 85 → 1.2× (wider)
    assert config_loader.apply_conviction_modifier(1.5, 90) == pytest.approx(1.8)
    assert config_loader.apply_conviction_modifier(1.5, 85) == pytest.approx(1.8)


def test_apply_conviction_modifier_low(full_cfg):
    # conviction < 75 → 0.8× (tighter)
    assert config_loader.apply_conviction_modifier(1.5, 70) == pytest.approx(1.2)


def test_apply_conviction_modifier_mid(full_cfg):
    # 75 <= conviction < 85 → unchanged
    assert config_loader.apply_conviction_modifier(1.5, 80) == pytest.approx(1.5)
    assert config_loader.apply_conviction_modifier(1.5, 75) == pytest.approx(1.5)


# ── Phase 8a: validate_config failure cases ──────────────────────────────

def test_validate_rejects_missing_halt_multiplier(tmp_path, monkeypatch):
    bad = FULL_CFG.replace("daily_loss_halt_multiplier: 3.0", "daily_loss_halt_multiplier: 0")
    _write_cfg(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="daily_loss_halt_multiplier"):
        config_loader.validate_config()
    config_loader.load_config(force_reload=True)


def test_validate_rejects_flatten_lt_halt(tmp_path, monkeypatch):
    bad = FULL_CFG.replace(
        "daily_loss_flatten_multiplier: 5.0",
        "daily_loss_flatten_multiplier: 2.0",
    )
    _write_cfg(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="flatten_multiplier"):
        config_loader.validate_config()
    config_loader.load_config(force_reload=True)


def test_validate_rejects_negative_max_stop_pct(tmp_path, monkeypatch):
    bad = FULL_CFG.replace("max_stop_pct: 2.5", "max_stop_pct: -1.0")
    _write_cfg(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="max_stop_pct"):
        config_loader.validate_config()
    config_loader.load_config(force_reload=True)


def test_validate_rejects_missing_viper_block():
    """Pass a cfg dict directly (bypass defaults merge) to confirm guard fires."""
    valid_block = {
        "initial_atr_multiplier": 1.5, "orb_stop_enabled": False, "breakeven_r": 1.0,
        "trail_atr_settle": 2.0, "trail_atr_confirm": 1.5, "trail_atr_lock": 1.0,
        "trail_atr_accelerate": 0.75, "partials_enabled": True,
        "partial_1_r": 1.5, "partial_1_pct": 0.5, "partial_2_r": 2.5, "partial_2_pct": 0.25,
        "time_stop_ist": "15:20", "rr_target": 2.0,
    }
    bad_cfg = {
        "execution": {"dry_run": True, "max_trades_per_day": 5, "per_trade_risk_inr": 100000,
                      "max_open_positions": 5, "conviction_threshold": 70},
        "router": {"dawn_confidence_min": 0.85, "hydra_confidence_min": 0.0, "router_enabled": True},
        "counterfactual": {"target_pct": 2.0, "sl_pct": -1.5, "auto_run_post_market": True},
        "stoploss": {
            "daily_loss_halt_multiplier": 3.0,
            "daily_loss_flatten_multiplier": 5.0,
            "max_stop_pct": 2.5,
            "conviction_modifiers": {
                "high_threshold": 85, "high_multiplier": 1.2,
                "low_threshold": 75, "low_multiplier": 0.8,
            },
            "by_strategy": {
                "DAWN": dict(valid_block),
                "HYDRA": dict(valid_block),
                # VIPER missing entirely
            },
        },
    }
    with pytest.raises(ValueError, match="VIPER"):
        config_loader.validate_config(bad_cfg)


def test_validate_rejects_bad_time_stop_format(tmp_path, monkeypatch):
    bad = FULL_CFG.replace('time_stop_ist: "13:30"', 'time_stop_ist: "badtime"')
    _write_cfg(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="time_stop_ist"):
        config_loader.validate_config()
    config_loader.load_config(force_reload=True)


def test_validate_accepts_valid_config(full_cfg):
    assert config_loader.validate_config() is True


# ── Phase 8a: defaults fallback when stoploss section missing ────────────

def test_missing_stoploss_section_uses_defaults(tmp_path, monkeypatch):
    """Config without stoploss: block falls back to _DEFAULTS so we never crash."""
    minimal = """\
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

counterfactual:
  target_pct: 2.0
  sl_pct: -1.5
  auto_run_post_market: true
"""
    _write_cfg(tmp_path, monkeypatch, minimal)
    # Accessors return defaults
    assert config_loader.get_daily_loss_halt_multiplier() == 3.0
    assert config_loader.get_daily_loss_flatten_multiplier() == 5.0
    assert config_loader.get_max_stop_pct() == 2.5
    dawn = config_loader.get_stoploss_config("DAWN")
    assert dawn["initial_atr_multiplier"] == 1.5
    assert dawn["orb_stop_enabled"] is True
    config_loader.load_config(force_reload=True)


# ── Phase 8b: Position dataclass + PositionBook extensions ──────────────

def test_position_has_phase8_fields():
    from src.trading.positions import Position
    from datetime import datetime, time as dt_time
    pos = Position(
        id="x", symbol="INFY", side="LONG", total_qty=10, avg_price=1500.0,
        realized_pnl=0.0, entry_time=datetime.now(), last_update_time=datetime.now(),
        mode="INTRADAY", strategy="DAWN", broker_order_ids=[],
    )
    # defaults
    assert pos.strategy_config == {}
    assert pos.time_stop_ist is None
    assert pos.orb_stop_low is None
    assert pos.peak_r == 0.0
    assert pos.partial_1_done is False
    assert pos.partial_2_done is False


def test_position_book_on_buy_fill_carries_strategy_config(full_cfg):
    from src.trading.positions import PositionBook
    book = PositionBook()
    cfg = config_loader.get_stoploss_config("DAWN")
    pos = book.on_buy_fill(
        symbol="INFY", qty=10, price=1500.0, mode="INTRADAY",
        strategy="DAWN", initial_stop_price=1477.5, atr=15.0,
        strategy_config=cfg, time_stop_ist=cfg["time_stop_ist"],
        orb_stop_low=1480.0, conviction_at_entry=88.0,
    )
    assert pos.strategy_config["initial_atr_multiplier"] == 1.5
    assert pos.strategy_config["orb_stop_enabled"] is True
    assert pos.time_stop_ist is not None
    assert pos.time_stop_ist.strftime("%H:%M") == "13:30"
    assert pos.orb_stop_low == 1480.0
    assert pos.conviction_at_entry == 88.0
    assert pos.initial_stop_price == 1477.5
    assert pos.atr == 15.0


def test_position_book_backwards_compatible_on_buy_fill():
    """Old call signature (no Phase 8 kwargs) still works — VIPER-path regression guard."""
    from src.trading.positions import PositionBook
    book = PositionBook()
    pos = book.on_buy_fill(
        symbol="TCS", qty=5, price=3000.0, mode="INTRADAY",
        strategy="VIPER", initial_stop_price=2955.0, atr=30.0,
    )
    assert pos.strategy_config == {}
    assert pos.time_stop_ist is None
    assert pos.orb_stop_low is None


def test_position_book_on_short_fill_carries_strategy_config(full_cfg):
    from src.trading.positions import PositionBook
    book = PositionBook()
    cfg = config_loader.get_stoploss_config("HYDRA")
    pos = book.on_short_fill(
        symbol="SBIN", qty=20, price=500.0, mode="INTRADAY",
        strategy="HYDRA", initial_stop_price=507.0, atr=5.0,
        strategy_config=cfg, time_stop_ist=cfg["time_stop_ist"],
        conviction_at_entry=82.0,
    )
    assert pos.strategy_config["rr_target"] == 1.8
    assert pos.time_stop_ist.strftime("%H:%M") == "12:30"
    assert pos.conviction_at_entry == 82.0


# ── Phase 8c: executor sizing (risk-at-stop) ────────────────────────────

def test_compute_live_quantity_risk_at_stop(full_cfg):
    """qty = int(per_trade_risk / stop_distance). ₹100k / ₹10 = 10,000 shares."""
    from src.trading.executor import _compute_live_quantity
    qty = _compute_live_quantity("INFY", entry_price=1500.0, stop_distance=10.0)
    # per_trade_risk = 100000 → qty = 10000, but notional cap = 10×100k = 1M
    # 1M / 1500 = 666 shares max under notional cap
    assert qty == 666


def test_compute_live_quantity_falls_back_to_max_stop_pct(full_cfg):
    """stop_distance=0 → fall back to entry_price × max_stop_pct(=2.5%)."""
    from src.trading.executor import _compute_live_quantity
    qty = _compute_live_quantity("TCS", entry_price=3000.0, stop_distance=0.0)
    # fallback stop = 3000 × 2.5% = 75 → qty = 100000 / 75 ≈ 1333
    # notional cap 10×100k = 1M → 1M / 3000 = 333 max
    assert qty == 333


def test_compute_live_quantity_zero_entry_returns_zero(full_cfg):
    from src.trading.executor import _compute_live_quantity
    assert _compute_live_quantity("X", entry_price=0, stop_distance=10.0) == 0


def test_compute_live_quantity_too_wide_stop_returns_zero(full_cfg):
    """stop > per_trade_risk → qty=0 (never risk more than configured)."""
    from src.trading.executor import _compute_live_quantity
    qty = _compute_live_quantity("X", entry_price=100.0, stop_distance=200000.0)
    assert qty == 0


def test_compute_live_quantity_notional_cap_applied(full_cfg):
    """Very tight stop would size huge qty — notional cap must kick in."""
    from src.trading.executor import _compute_live_quantity
    # ₹100k risk / ₹1 stop = 100k shares @ ₹500 = ₹50M notional (way too big)
    # Cap: 10× per_trade_risk = ₹1M / ₹500 = 2000 shares
    qty = _compute_live_quantity("BIGSTOCK", entry_price=500.0, stop_distance=1.0)
    assert qty == 2000


def test_position_to_dict_roundtrip_preserves_phase8_fields():
    """Serialize/deserialize must preserve Phase 8 fields (for shadow persistence)."""
    from src.trading.positions import Position
    from datetime import datetime, time as dt_time
    pos = Position(
        id="y", symbol="HDFC", side="LONG", total_qty=8, avg_price=2800.0,
        realized_pnl=0.0, entry_time=datetime.now(), last_update_time=datetime.now(),
        mode="INTRADAY", strategy="DAWN", broker_order_ids=[],
        strategy_config={"initial_atr_multiplier": 1.5, "orb_stop_enabled": True},
        time_stop_ist=dt_time(13, 30), orb_stop_low=2790.0, peak_r=1.8,
    )
    d = pos.to_dict()
    assert d["time_stop_ist"] == "13:30"
    assert d["orb_stop_low"] == 2790.0
    assert d["peak_r"] == 1.8
    pos2 = Position.from_dict(d)
    assert pos2.time_stop_ist == dt_time(13, 30)
    assert pos2.orb_stop_low == 2790.0
    assert pos2.strategy_config == {"initial_atr_multiplier": 1.5, "orb_stop_enabled": True}


# ── Phase 8d: ExitEngine per-strategy config injection ──────────────────

def _make_position(
    symbol: str = "INFY",
    side: str = "LONG",
    avg_price: float = 1500.0,
    initial_stop: float = 1477.5,
    atr: float = 15.0,
    strategy: str = "DAWN",
    strategy_config: dict = None,
    time_stop_ist=None,
    orb_stop_low: float = None,
    conviction: float = 80.0,
) -> "Position":
    from src.trading.positions import Position
    from datetime import datetime
    return Position(
        id="test-id", symbol=symbol, side=side, total_qty=10,
        avg_price=avg_price, realized_pnl=0.0,
        entry_time=datetime.now(), last_update_time=datetime.now(),
        mode="INTRADAY", strategy=strategy, broker_order_ids=[],
        initial_stop_price=initial_stop, trailing_stop_price=initial_stop,
        highest_price=avg_price, lowest_price=avg_price,
        atr=atr, original_qty=10,
        strategy_config=dict(strategy_config) if strategy_config else {},
        time_stop_ist=time_stop_ist, orb_stop_low=orb_stop_low,
        conviction_at_entry=conviction,
    )


def _get_phase_multiplier_for(pos, mins_open: float) -> float:
    from src.trading.exit_engine import ExitEngine
    from unittest.mock import MagicMock
    from src.config.risk import RiskConfig
    risk = MagicMock(spec=RiskConfig)
    risk.intraday_exit_time = "15:20"
    engine = ExitEngine(MagicMock(), MagicMock(), risk)
    return engine._get_phase_multiplier(pos, mins_open)


@pytest.mark.parametrize("strategy,settle,lock,accel", [
    ("DAWN",  2.0, 1.2, 0.75),
    ("HYDRA", 2.0, 1.0, 0.75),
    ("VIPER", 2.0, 1.0, 0.75),
])
def test_exit_engine_phase_multiplier_reads_strategy_config(full_cfg, strategy, settle, lock, accel):
    cfg = config_loader.get_stoploss_config(strategy)
    pos = _make_position(strategy=strategy, strategy_config=cfg)

    # settle phase (0 min, 0R)
    assert _get_phase_multiplier_for(pos, 0) == pytest.approx(settle)

    # lock phase (1.5R+): set highest_price to 1R×lock_threshold above entry
    pos.highest_price = pos.avg_price + pos.risk_unit() * 1.6
    assert _get_phase_multiplier_for(pos, 0) == pytest.approx(lock)

    # accelerate phase (2.5R+)
    pos.highest_price = pos.avg_price + pos.risk_unit() * 2.6
    assert _get_phase_multiplier_for(pos, 0) == pytest.approx(accel)


def test_exit_engine_time_stop_per_position(full_cfg):
    """ExitEngine must use pos.time_stop_ist, not the global exit time."""
    from src.trading.exit_engine import ExitEngine
    from unittest.mock import MagicMock
    from datetime import datetime, time as dt_time
    from src.config.risk import RiskConfig

    cfg = config_loader.get_stoploss_config("DAWN")
    pos = _make_position(
        strategy="DAWN", strategy_config=cfg, time_stop_ist=dt_time(13, 30)
    )

    book = MagicMock()
    book.get_open_positions.return_value = [pos]
    live_client = MagicMock()
    tick = MagicMock(); tick.ltp = 1510.0
    live_client.get_last_tick.return_value = tick
    risk = MagicMock(spec=RiskConfig)
    risk.intraday_exit_time = "15:20"

    engine = ExitEngine(book, live_client, risk)
    # Pass naive IST datetime (13:35) — past DAWN's 13:30 stop, well before global 15:20
    now = datetime(2026, 4, 18, 13, 35, 0)
    signals = engine.tick(now)
    assert any(s.reason == "TIME_EXIT" for s in signals), (
        "Expected TIME_EXIT at 13:35 for DAWN (time_stop=13:30)"
    )


def test_exit_engine_time_stop_not_fired_before_threshold(full_cfg):
    """Before DAWN's 13:30 stop, no TIME_EXIT should fire."""
    from src.trading.exit_engine import ExitEngine
    from unittest.mock import MagicMock
    from datetime import datetime, time as dt_time

    cfg = config_loader.get_stoploss_config("DAWN")
    pos = _make_position(strategy="DAWN", strategy_config=cfg, time_stop_ist=dt_time(13, 30))
    book = MagicMock()
    book.get_open_positions.return_value = [pos]
    live_client = MagicMock()
    tick = MagicMock(); tick.ltp = 1510.0
    live_client.get_last_tick.return_value = tick
    risk = MagicMock()
    risk.intraday_exit_time = "15:20"

    engine = ExitEngine(book, live_client, risk)
    # 12:55 IST — before DAWN's 13:30 stop
    now = datetime(2026, 4, 18, 12, 55, 0)
    signals = engine.tick(now)
    assert not any(s.reason == "TIME_EXIT" for s in signals)


def test_exit_engine_orb_stop_fires_when_ltp_breaches_orb_low(full_cfg):
    """ORB stop must fire for DAWN LONG when ltp drops below orb_stop_low."""
    from src.trading.exit_engine import ExitEngine
    from unittest.mock import MagicMock
    from datetime import datetime

    cfg = config_loader.get_stoploss_config("DAWN")
    pos = _make_position(
        strategy="DAWN", strategy_config=cfg,
        avg_price=1500.0, initial_stop=1460.0,
        orb_stop_low=1480.0,
    )
    book = MagicMock()
    book.get_open_positions.return_value = [pos]
    live_client = MagicMock()
    tick = MagicMock(); tick.ltp = 1475.0  # below ORB low
    live_client.get_last_tick.return_value = tick
    risk = MagicMock(); risk.intraday_exit_time = "15:20"

    engine = ExitEngine(book, live_client, risk)
    now = datetime(2026, 4, 18, 11, 0, 0)  # 11:00 IST (naive)
    signals = engine.tick(now)
    assert any(s.reason == "ORB_STOP" for s in signals)


def test_exit_engine_orb_stop_skipped_for_hydra(full_cfg):
    """orb_stop_enabled=False for HYDRA → no ORB_STOP signal even if orb_stop_low set."""
    from src.trading.exit_engine import ExitEngine
    from unittest.mock import MagicMock
    from datetime import datetime

    cfg = config_loader.get_stoploss_config("HYDRA")
    assert cfg["orb_stop_enabled"] is False
    pos = _make_position(
        strategy="HYDRA", strategy_config=cfg,
        avg_price=500.0, initial_stop=480.0,
        orb_stop_low=488.0,
    )
    book = MagicMock()
    book.get_open_positions.return_value = [pos]
    live_client = MagicMock()
    tick = MagicMock(); tick.ltp = 485.0  # below orb_stop_low but HYDRA ignores it
    live_client.get_last_tick.return_value = tick
    risk = MagicMock(); risk.intraday_exit_time = "15:20"

    engine = ExitEngine(book, live_client, risk)
    now = datetime(2026, 4, 18, 11, 0, 0)
    signals = engine.tick(now)
    assert not any(s.reason == "ORB_STOP" for s in signals)


@pytest.mark.parametrize("strategy,expected_p1_r,expected_p2_r", [
    ("DAWN",  1.5, 2.5),
    ("HYDRA", 1.5, 2.5),
    ("VIPER", 2.0, 3.0),
])
def test_exit_engine_partials_per_strategy(full_cfg, strategy, expected_p1_r, expected_p2_r):
    """Partial exit targets must match strategy config."""
    from src.trading.exit_engine import ExitEngine
    from unittest.mock import MagicMock
    from datetime import datetime

    cfg = config_loader.get_stoploss_config(strategy)
    # Entry: 1500, stop: 1470 → 1R = 30 pts
    pos = _make_position(
        strategy=strategy, strategy_config=cfg,
        avg_price=1500.0, initial_stop=1470.0, atr=20.0,
    )
    pos.highest_price = 1500.0 + 30.0 * expected_p1_r + 1.0  # just past partial_1_r
    pos.breakeven_activated = True
    book = MagicMock()
    book.get_open_positions.return_value = [pos]
    live_client = MagicMock()
    ltp = 1500.0 + 30.0 * expected_p1_r + 0.5  # ltp at partial_1_r threshold
    tick = MagicMock(); tick.ltp = ltp
    live_client.get_last_tick.return_value = tick
    risk = MagicMock(); risk.intraday_exit_time = "15:20"

    engine = ExitEngine(book, live_client, risk)
    now = datetime(2026, 4, 18, 11, 0, 0)
    signals = engine.tick(now)
    assert any(s.reason == "PARTIAL_1" for s in signals), (
        f"{strategy}: expected PARTIAL_1 at {expected_p1_r}R"
    )


def test_exit_engine_legacy_viper_partial_targets_unchanged():
    """Pre-Phase-8 VIPER Positions (empty strategy_config) must still get 2.0R/3.0R."""
    from src.trading.exit_engine import ExitEngine, VIPER_PARTIAL_1_R, VIPER_PARTIAL_2_R
    from unittest.mock import MagicMock
    from datetime import datetime

    # Legacy position: no strategy_config set
    pos = _make_position(
        strategy="VIPER", strategy_config={},
        avg_price=1500.0, initial_stop=1470.0, atr=20.0,
    )
    pos.highest_price = 1500.0 + 30.0 * VIPER_PARTIAL_1_R + 1.0
    pos.breakeven_activated = True
    book = MagicMock()
    book.get_open_positions.return_value = [pos]
    live_client = MagicMock()
    ltp = 1500.0 + 30.0 * VIPER_PARTIAL_1_R + 0.5
    tick = MagicMock(); tick.ltp = ltp
    live_client.get_last_tick.return_value = tick
    risk = MagicMock(); risk.intraday_exit_time = "15:20"

    engine = ExitEngine(book, live_client, risk)
    signals = engine.tick(datetime(2026, 4, 18, 11, 0, 0))
    assert any(s.reason == "PARTIAL_1" for s in signals)


def test_exit_engine_flatten_all():
    """flatten_all must return one exit signal per open position."""
    from src.trading.exit_engine import ExitEngine
    from unittest.mock import MagicMock
    from datetime import datetime

    pos1 = _make_position(symbol="INFY")
    pos2 = _make_position(symbol="TCS", strategy="HYDRA")
    book = MagicMock()
    book.get_open_positions.return_value = [pos1, pos2]
    live_client = MagicMock()
    tick = MagicMock(); tick.ltp = 1510.0
    live_client.get_last_tick.return_value = tick
    risk = MagicMock(); risk.intraday_exit_time = "15:20"

    engine = ExitEngine(book, live_client, risk)
    signals = engine.flatten_all("DAILY_LOSS_FLATTEN")
    assert len(signals) == 2
    assert all(s.reason == "DAILY_LOSS_FLATTEN" for s in signals)


# ── Phase 8f: DailyRiskState circuit breaker ────────────────────────────

def test_check_halt_no_action_when_pnl_ok(full_cfg):
    from src.trading.daily_risk_state import DailyRiskState
    from datetime import date
    state = DailyRiskState(trading_date=date.today())
    state.daily_pnl = -50000.0  # loss of 0.5× per_trade_risk — within bounds
    should_flatten, reason = state.check_halt(per_trade_risk=100000.0)
    assert not should_flatten
    assert not state.is_halted
    assert reason == ""


def test_check_halt_triggers_halt_at_3x(full_cfg):
    from src.trading.daily_risk_state import DailyRiskState
    from datetime import date
    state = DailyRiskState(trading_date=date.today())
    state.daily_pnl = -305000.0  # just past -3× ₹100k
    should_flatten, reason = state.check_halt(per_trade_risk=100000.0)
    assert not should_flatten        # halt, not flatten yet
    assert state.is_halted
    assert "DAILY_HALT" in reason


def test_check_halt_triggers_flatten_at_5x(full_cfg):
    from src.trading.daily_risk_state import DailyRiskState
    from datetime import date
    state = DailyRiskState(trading_date=date.today())
    state.daily_pnl = -510000.0  # past -5× ₹100k
    should_flatten, reason = state.check_halt(per_trade_risk=100000.0)
    assert should_flatten
    assert state.is_halted
    assert "FLATTEN" in reason


def test_check_halt_resets_next_day(full_cfg):
    from src.trading.daily_risk_state import DailyRiskState
    from datetime import date, timedelta
    state = DailyRiskState(trading_date=date.today())
    state.daily_pnl = -510000.0
    state.check_halt(per_trade_risk=100000.0)
    assert state.is_halted

    state.reset_for_new_day(date.today() + timedelta(days=1))
    assert not state.is_halted
    assert state.halt_reason == ""
    assert state.daily_pnl == 0.0


def test_check_halt_idempotent(full_cfg):
    from src.trading.daily_risk_state import DailyRiskState
    from datetime import date
    state = DailyRiskState(trading_date=date.today())
    state.daily_pnl = -310000.0
    state.check_halt(per_trade_risk=100000.0)
    first_reason = state.halt_reason
    state.check_halt(per_trade_risk=100000.0)  # call again
    assert state.halt_reason == first_reason   # same reason, no duplicate


# ── Phase 8h: ShadowBook ────────────────────────────────────────────────

def test_shadow_book_entry_and_exit(tmp_path, monkeypatch):
    from src.trading.shadow_book import ShadowBook
    import src.trading.shadow_book as sb_mod
    monkeypatch.setattr(sb_mod, "_SHADOWS_DIR", tmp_path)

    from datetime import date
    book = ShadowBook(shadow_date=date.today())

    book.on_entry(
        symbol="INFY", qty=10, price=1500.0, strategy="DAWN",
        initial_stop_price=1477.5, atr=15.0,
        strategy_config={"rr_target": 2.5, "orb_stop_enabled": True},
        conviction_at_entry=85.0,
    )
    assert len(book.get_open()) == 1
    assert len(book.get_closed()) == 0

    book.on_exit(symbol="INFY", qty=10, exit_price=1537.5, exit_reason="PARTIAL_1")
    assert len(book.get_open()) == 0
    assert len(book.get_closed()) == 1
    rec = book.get_closed()[0]
    assert rec["exit_reason"] == "PARTIAL_1"
    assert rec["symbol"] == "INFY"
    # 1R = 1500 - 1477.5 = 22.5; exit gain = 1537.5 - 1500 = 37.5 → 37.5/22.5 ≈ 1.67R
    assert rec["r_captured"] == pytest.approx(37.5 / 22.5, rel=1e-3)


def test_shadow_book_persists_to_disk(tmp_path, monkeypatch):
    from src.trading.shadow_book import ShadowBook
    import src.trading.shadow_book as sb_mod
    import json
    monkeypatch.setattr(sb_mod, "_SHADOWS_DIR", tmp_path)

    from datetime import date
    today = date.today()
    book = ShadowBook(shadow_date=today)
    book.on_entry(
        symbol="TCS", qty=5, price=3000.0, strategy="HYDRA",
        initial_stop_price=2955.0, atr=30.0,
        strategy_config={"rr_target": 1.8},
        conviction_at_entry=80.0,
    )
    book.on_exit(symbol="TCS", qty=5, exit_price=3075.0, exit_reason="TRAILING_STOP")

    path = tmp_path / f"sl_shadow_{today.isoformat()}.json"
    assert path.exists()
    d = json.loads(path.read_text())
    assert len(d["closed"]) == 1
    assert d["closed"][0]["symbol"] == "TCS"


def test_shadow_book_report_section(tmp_path, monkeypatch):
    from src.trading.shadow_book import ShadowBook, generate_shadow_report_section
    import src.trading.shadow_book as sb_mod
    monkeypatch.setattr(sb_mod, "_SHADOWS_DIR", tmp_path)

    from datetime import date
    today = date.today()
    book = ShadowBook(shadow_date=today)
    book.on_entry(
        symbol="INFY", qty=10, price=1500.0, strategy="DAWN",
        initial_stop_price=1477.5, atr=15.0,
        strategy_config={"rr_target": 2.5}, conviction_at_entry=85.0,
    )
    book.on_exit(symbol="INFY", qty=10, exit_price=1537.5, exit_reason="PARTIAL_1")

    report = generate_shadow_report_section(today)
    assert "## 14." in report
    assert "PARTIAL_1" in report
    assert "Avg R captured" in report
