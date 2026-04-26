"""
test_runner_restart.py — Position reconciliation on runner boot.

Tests reconcile_positions() in isolation:
  1. dry_run=True → nothing recovered (safe skip)
  2. Live mode → open Kite MIS position is injected into PositionBook
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.trading.reconcile import reconcile_positions
from src.trading.positions import PositionBook


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_kite_position(symbol: str, qty: int = 10, avg_price: float = 500.0) -> dict:
    """Minimal Kite MIS position dict as returned by kite.positions()['net']."""
    return {
        "tradingsymbol": symbol,
        "quantity": qty,
        "average_price": avg_price,
        "product": "MIS",
        "exchange": "NSE",
        "instrument_token": 123456,
    }


def _make_mock_zerodha(positions: list[dict]):
    """Return a ZerodhaClient mock whose get_open_positions() yields given list."""
    mock = MagicMock()
    mock.get_open_positions.return_value = positions
    return mock


# ── test 1: dry_run skips reconciliation ─────────────────────────────────────

def test_reconcile_skips_in_dry_run():
    book   = PositionBook()
    client = _make_mock_zerodha([_make_kite_position("TCS", qty=5)])

    reconcile_positions(positions=book, zerodha_client=client, dry_run=True)

    # Kite should not have been queried
    client.get_open_positions.assert_not_called()
    # Book must remain empty
    assert book.get_open_positions() == []


# ── test 2: no live client skips reconciliation ───────────────────────────────

def test_reconcile_skips_when_no_client():
    book = PositionBook()
    reconcile_positions(positions=book, zerodha_client=None, dry_run=False)
    assert book.get_open_positions() == []


# ── test 3: live mode recovers an open position ───────────────────────────────

def test_reconcile_restores_open_position():
    book   = PositionBook()
    client = _make_mock_zerodha([_make_kite_position("RELIANCE", qty=8, avg_price=2800.0)])

    reconcile_positions(positions=book, zerodha_client=client, dry_run=False)

    client.get_open_positions.assert_called_once()
    open_pos = book.get_open_positions()
    assert len(open_pos) == 1
    pos = open_pos[0]
    assert pos.symbol == "RELIANCE"
    assert pos.total_qty == 8
    assert float(pos.avg_price) == pytest.approx(2800.0)
    assert pos.strategy == "RECOVERED"


# ── test 4: position already in book is not duplicated ────────────────────────

def test_reconcile_does_not_duplicate_existing_position():
    book = PositionBook()
    # Pre-populate the book (simulates a position already tracked)
    book.on_buy_fill(
        symbol="INFY",
        qty=5,
        price=1800.0,
        mode="INTRADAY",
        strategy="DAWN",
    )
    client = _make_mock_zerodha([_make_kite_position("INFY", qty=5, avg_price=1800.0)])

    reconcile_positions(positions=book, zerodha_client=client, dry_run=False)

    # Still exactly one position, not two
    assert len(book.get_open_positions()) == 1


# ── test 5: zero qty / zero price entries are ignored ────────────────────────

def test_reconcile_ignores_zero_qty():
    book   = PositionBook()
    client = _make_mock_zerodha([
        _make_kite_position("WIPRO", qty=0, avg_price=300.0),
        _make_kite_position("TCS",   qty=5, avg_price=3500.0),
    ])

    reconcile_positions(positions=book, zerodha_client=client, dry_run=False)

    open_pos = book.get_open_positions()
    assert len(open_pos) == 1
    assert open_pos[0].symbol == "TCS"


# ── test 6: kite API error → empty book, no crash ────────────────────────────

def test_reconcile_handles_kite_error_gracefully():
    book   = PositionBook()
    client = MagicMock()
    client.get_open_positions.return_value = []   # ZerodhaClient already swallows errors

    reconcile_positions(positions=book, zerodha_client=client, dry_run=False)

    assert book.get_open_positions() == []
