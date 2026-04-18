"""
test_live_execution.py — Phase 7 live execution tests.

Ten tests covering the 5-gate safety system, live order placement,
live trade tracker I/O, and EOD square-off semantics.
"""
from __future__ import annotations

import json
from datetime import datetime, time as dt_time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.trading import executor as executor_mod
from src.trading import live_trade_tracker as tracker_mod
from src.trading.executor import TradeExecutor, run_live_gates
from src.trading.orders import OrderResult, OrderSide


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Redirect live_trade_tracker writes to a temp directory."""
    monkeypatch.setattr(tracker_mod, "DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def trading_hours(monkeypatch):
    """Force IST clock to a safely-inside-window value (11:00)."""
    monkeypatch.setattr(
        executor_mod, "_now_ist_time", lambda: dt_time(11, 0)
    )


@pytest.fixture
def fresh_risk(monkeypatch):
    """Build a TradeExecutor whose live_mode is True and ZerodhaClient is mocked."""
    from src.config.risk import RiskConfig
    from src.trading.daily_risk_state import DailyRiskState

    risk = RiskConfig(
        live_mode=True,
        max_trades_per_day=5,
        per_trade_capital_rupees=100000.0,
        max_open_positions=5,
    )
    state = DailyRiskState(trading_date=datetime.now().date())

    # Bypass real Zerodha auth
    mock_kite_client = MagicMock()
    mock_kite_client.place_equity_order.return_value = OrderResult(
        success=True, broker_order_id="TEST_ORDER_123",
        message="ok", filled_qty=0, avg_price=None,
    )
    with patch.object(executor_mod, "ZerodhaClient") as zc_cls:
        zc_cls.from_env.return_value = mock_kite_client
        ex = TradeExecutor(risk=risk, daily_state=state)
    ex._zerodha = mock_kite_client
    return ex, mock_kite_client


def _pass_all_gates(monkeypatch, tmp_data_dir):
    """Patch config + tracker to make all 5 gates pass."""
    monkeypatch.setattr("src.config_loader.get_dry_run", lambda: False)
    monkeypatch.setattr("src.config_loader.get_conviction_threshold", lambda: 70)
    monkeypatch.setattr("src.config_loader.get_max_trades", lambda: 5)
    monkeypatch.setattr("src.config_loader.get_max_open_positions", lambda: 5)
    monkeypatch.setattr("src.config_loader.get_per_trade_risk", lambda: 100000.0)
    monkeypatch.setattr("src.config_loader.get_live_order_tag", lambda: "VOLTEDGE_AUTO")
    monkeypatch.setattr("src.config_loader.get_email_enabled", lambda: False)


# ── Tests ────────────────────────────────────────────────────────────────

def test_1_all_gates_pass_triggers_order_placement(
    monkeypatch, tmp_data_dir, trading_hours, fresh_risk
):
    """Test 1: All 5 gates pass → order placement attempted."""
    ex, mock_kite = fresh_risk
    _pass_all_gates(monkeypatch, tmp_data_dir)

    result = ex.execute_buy(
        symbol="RELIANCE", ltp=2500.0, qty=40,
        conviction=85.0, route="HYDRA",
    )

    assert result.success is True
    assert mock_kite.place_equity_order.call_count == 1
    req = mock_kite.place_equity_order.call_args.args[0]
    assert req.symbol == "RELIANCE"
    assert req.side == OrderSide.BUY
    assert req.tag == "VOLTEDGE_AUTO"


def test_2_gate1_fail_dry_run_true_blocks_order(
    monkeypatch, tmp_data_dir, trading_hours, fresh_risk, caplog
):
    """Test 2: Gate 1 fail (dry_run=True) → order blocked, no kite call."""
    ex, mock_kite = fresh_risk
    monkeypatch.setattr("src.config_loader.get_dry_run", lambda: True)
    monkeypatch.setattr("src.config_loader.get_conviction_threshold", lambda: 70)
    monkeypatch.setattr("src.config_loader.get_max_trades", lambda: 5)
    monkeypatch.setattr("src.config_loader.get_max_open_positions", lambda: 5)
    monkeypatch.setattr("src.config_loader.get_per_trade_risk", lambda: 100000.0)
    monkeypatch.setattr("src.config_loader.get_live_order_tag", lambda: "VOLTEDGE_AUTO")

    with caplog.at_level("WARNING"):
        result = ex.execute_buy(
            symbol="RELIANCE", ltp=2500.0, qty=40, conviction=90.0
        )

    assert result.success is False
    assert mock_kite.place_equity_order.call_count == 0
    assert any("LIVE GATE 1 FAIL" in m for m in caplog.messages)


def test_3_gate2_fail_low_conviction_blocks_order(
    monkeypatch, tmp_data_dir, trading_hours, fresh_risk, caplog
):
    """Test 3: Gate 2 fail (conviction < 70) → order blocked."""
    ex, mock_kite = fresh_risk
    _pass_all_gates(monkeypatch, tmp_data_dir)

    with caplog.at_level("WARNING"):
        result = ex.execute_buy(
            symbol="TCS", ltp=3500.0, qty=20, conviction=50.0
        )

    assert result.success is False
    assert mock_kite.place_equity_order.call_count == 0
    assert any("LIVE GATE 2 FAIL" in m for m in caplog.messages)


def test_4_gate3_fail_daily_limit_blocks_order(
    monkeypatch, tmp_data_dir, trading_hours, fresh_risk, caplog
):
    """Test 4: Gate 3 fail (5 trades placed, max 5) → order blocked."""
    ex, mock_kite = fresh_risk
    _pass_all_gates(monkeypatch, tmp_data_dir)

    # Pre-seed 5 trades in today's file
    for i in range(5):
        tracker_mod.record_live_trade(
            symbol=f"SYM{i}", order_id=f"OID{i}",
            quantity=10, entry_price=100.0,
        )

    with caplog.at_level("WARNING"):
        result = ex.execute_buy(
            symbol="INFY", ltp=1500.0, qty=30, conviction=90.0
        )

    assert result.success is False
    assert mock_kite.place_equity_order.call_count == 0
    assert any("LIVE GATE 3 FAIL" in m for m in caplog.messages)


def test_5_gate5_fail_outside_market_hours_blocks_order(
    monkeypatch, tmp_data_dir, fresh_risk, caplog
):
    """Test 5: Gate 5 fail (15:30 IST, past 15:15 cutoff) → order blocked."""
    ex, mock_kite = fresh_risk
    _pass_all_gates(monkeypatch, tmp_data_dir)
    monkeypatch.setattr(
        executor_mod, "_now_ist_time", lambda: dt_time(15, 30)
    )

    with caplog.at_level("WARNING"):
        result = ex.execute_buy(
            symbol="HDFCBANK", ltp=1600.0, qty=62, conviction=85.0
        )

    assert result.success is False
    assert mock_kite.place_equity_order.call_count == 0
    assert any("LIVE GATE 5 FAIL" in m for m in caplog.messages)


def test_6_record_live_trade_writes_correct_json(tmp_data_dir):
    """Test 6: record_live_trade writes correct JSON to dated file."""
    tracker_mod.record_live_trade(
        symbol="RELIANCE", order_id="OID123",
        quantity=40, entry_price=2500.0,
        route="HYDRA", confidence=0.85, conviction_score=90.0,
    )

    files = list(Path(tmp_data_dir).glob("live_trades_*.json"))
    assert len(files) == 1
    records = json.loads(files[0].read_text())
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "RELIANCE"
    assert r["order_id"] == "OID123"
    assert r["quantity"] == 40
    assert r["entry_price"] == 2500.0
    assert r["route"] == "HYDRA"
    assert r["conviction_score"] == 90.0
    assert r["status"] == "OPEN"
    assert r["exit_price"] is None
    assert r["pnl_pct"] is None
    assert "placed_at" in r


def test_7_get_trades_placed_today_returns_correct_count(tmp_data_dir):
    """Test 7: get_trades_placed_today returns correct count."""
    assert tracker_mod.get_trades_placed_today() == 0
    for i in range(3):
        tracker_mod.record_live_trade(
            symbol=f"S{i}", order_id=f"O{i}",
            quantity=10, entry_price=500.0,
        )
    assert tracker_mod.get_trades_placed_today() == 3
    assert tracker_mod.get_open_positions() == 3


def test_8_update_trade_status_closes_trade_with_pnl(tmp_data_dir):
    """Test 8: update_trade_status closes trade, computes pnl_pct correctly."""
    tracker_mod.record_live_trade(
        symbol="TCS", order_id="TCS_OID", quantity=10, entry_price=1000.0
    )

    ok = tracker_mod.update_trade_status(
        order_id="TCS_OID", exit_price=1050.0,
        exit_at="2026-04-18T15:15:00+05:30",
    )
    assert ok is True

    files = list(Path(tmp_data_dir).glob("live_trades_*.json"))
    rec = json.loads(files[0].read_text())[0]
    assert rec["status"] == "CLOSED"
    assert rec["exit_price"] == 1050.0
    assert rec["pnl_pct"] == 5.0  # (1050 - 1000) / 1000 * 100


def test_9_place_order_exception_does_not_crash(
    monkeypatch, tmp_data_dir, trading_hours, fresh_risk, caplog
):
    """Test 9: kite.place_equity_order raising → error logged, no crash."""
    ex, mock_kite = fresh_risk
    _pass_all_gates(monkeypatch, tmp_data_dir)
    mock_kite.place_equity_order.side_effect = RuntimeError("kite network error")

    with caplog.at_level("ERROR"):
        result = ex.execute_buy(
            symbol="SBIN", ltp=600.0, qty=166, conviction=80.0
        )

    assert result.success is False
    assert any("LIVE ORDER FAILED" in m for m in caplog.messages)


def test_10_eod_squareoff_places_sell_for_open_position(
    monkeypatch, tmp_data_dir, trading_hours, fresh_risk
):
    """Test 10: 1 open LIVE position → executor.execute_sell called in live path."""
    ex, mock_kite = fresh_risk
    _pass_all_gates(monkeypatch, tmp_data_dir)

    tracker_mod.record_live_trade(
        symbol="RELIANCE", order_id="OPEN_OID",
        quantity=40, entry_price=2500.0,
    )
    assert tracker_mod.get_open_positions() == 1

    open_trades = tracker_mod.get_open_trades_today()
    assert len(open_trades) == 1

    # Simulate the runner's EOD square-off call path:
    result = ex.execute_sell(
        symbol=open_trades[0]["symbol"],
        qty=int(open_trades[0]["quantity"]),
        ltp=2520.0, route="EOD_SQUAREOFF",
    )
    assert result.success is True
    # One sell order placed
    assert mock_kite.place_equity_order.call_count == 1
    req = mock_kite.place_equity_order.call_args.args[0]
    assert req.side == OrderSide.SELL
    assert req.symbol == "RELIANCE"

    # Close tracker entry, verify pnl_pct
    tracker_mod.update_trade_status(
        order_id="OPEN_OID", exit_price=2520.0,
    )
    assert tracker_mod.get_open_positions() == 0
