"""
test_pattern_db_metadata.py — C1 entry_price fallback chain in record_eod_outcomes.

Tests that the expired-signal branch resolves entry_price via the 4-priority chain:
  1. metadata["entry_price"]
  2. metadata["detection_price"]
  3. price_map[symbol]
  4. 0 → NO_DATA_EXPIRED + WARNING logged
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.trading.conviction_engine import ActiveSignal, ConvictionEngine


def _make_signal(symbol: str = "TEST", metadata: Optional[Dict[str, Any]] = None) -> ActiveSignal:
    sig = ActiveSignal(
        symbol=symbol,
        direction="BUY",
        strategy="VIPER",
        layer_c_score=60.0,
        event_summary="test gap momentum",
        metadata=metadata or {},
    )
    sig.status = "EXPIRED"
    return sig


def _run_record_eod(signal: ActiveSignal, price_map: Dict[str, float]) -> list:
    """
    Run record_eod_outcomes with a single expired signal and a mock PatternDB.
    Returns the list of PatternOutcome objects passed to record_outcome.
    """
    recorded: list = []

    engine = ConvictionEngine.__new__(ConvictionEngine)
    engine._watchboard = {f"{signal.symbol}_{signal.direction}": signal}
    engine._prev_snapshot = None
    engine._phase_state = MagicMock()
    engine._phase_state.current_phase.value = "choppy"

    mock_pdb = MagicMock()
    mock_pdb.record_outcome.side_effect = lambda o: recorded.append(o)
    engine._pattern_db = mock_pdb

    engine.record_eod_outcomes([], price_map=price_map)
    return recorded


# ── Test 1: explicit entry_price in metadata ──────────────────────────────────

def test_entry_price_from_metadata():
    """Uses metadata['entry_price'] — most direct path."""
    sig = _make_signal(metadata={"entry_price": 1000.0})
    price_map = {"TEST": 1020.0}  # EOD moves +2%
    recorded = _run_record_eod(sig, price_map)
    assert len(recorded) == 1
    assert recorded[0].outcome in {"CORRECT_NO_TRADE", "WRONG_NO_TRADE"}
    assert abs(recorded[0].pnl_pct - 2.0) < 0.01


# ── Test 2: detection_price fallback ─────────────────────────────────────────

def test_entry_price_from_detection_price():
    """Falls back to metadata['detection_price'] when entry_price absent."""
    sig = _make_signal(metadata={"detection_price": 1000.0})
    price_map = {"TEST": 980.0}  # EOD moves -2%
    recorded = _run_record_eod(sig, price_map)
    assert len(recorded) == 1
    assert recorded[0].outcome == "WRONG_NO_TRADE"


# ── Test 3: price_map fallback ────────────────────────────────────────────────

def test_entry_price_from_price_map():
    """Falls back to price_map[symbol] when both metadata keys absent."""
    sig = _make_signal(metadata={})
    price_map = {"TEST": 500.0}
    recorded = _run_record_eod(sig, price_map)
    assert len(recorded) == 1
    # entry == current_price when using price_map fallback → move_pct == 0 → CORRECT_NO_TRADE
    assert recorded[0].outcome == "CORRECT_NO_TRADE"
    assert recorded[0].pnl_pct == 0.0


# ── Test 4: all absent → NO_DATA_EXPIRED + WARNING ───────────────────────────

def test_no_price_resolved_produces_no_data_expired(caplog):
    """When all fallbacks miss, writes NO_DATA_EXPIRED and logs a WARNING."""
    sig = _make_signal(metadata={})
    price_map = {}  # symbol not in map
    with caplog.at_level(logging.WARNING, logger="src.trading.conviction_engine"):
        recorded = _run_record_eod(sig, price_map)
    assert len(recorded) == 1
    assert recorded[0].outcome == "NO_DATA_EXPIRED"
    assert any("no price resolved" in r.message for r in caplog.records)


# ── Test 5: entry_price takes priority over detection_price ──────────────────

def test_entry_price_takes_priority_over_detection_price():
    """entry_price in metadata wins over detection_price."""
    sig = _make_signal(metadata={"entry_price": 200.0, "detection_price": 999.0})
    price_map = {"TEST": 210.0}  # +5% from entry_price
    recorded = _run_record_eod(sig, price_map)
    assert len(recorded) == 1
    assert abs(recorded[0].pnl_pct - 5.0) < 0.1


# ── Test 6: detection_price takes priority over price_map ────────────────────

def test_detection_price_takes_priority_over_price_map():
    """detection_price wins over price_map when entry_price absent."""
    sig = _make_signal(metadata={"detection_price": 100.0})
    price_map = {"TEST": 150.0}  # price_map has different value
    recorded = _run_record_eod(sig, price_map)
    assert len(recorded) == 1
    # entry = 100, current = 150 → +50%
    assert recorded[0].pnl_pct == pytest.approx(50.0, abs=0.1)
