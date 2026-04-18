"""
test_backtest.py — Phase 6 backtesting harness unit tests.
"""
import pytest

from src.strategies.base import ConvictionScore
from src.backtest.signal_replayer import build_watchlist_entry, replay_day


# ── Config fixture (prevents loading live config.yaml) ────────────────────

@pytest.fixture(autouse=True)
def _bt_config(tmp_path, monkeypatch):
    cfg = (
        "execution:\n  conviction_threshold: 70\n"
        "backtest:\n  target_pct: 2.0\n  sl_pct: 1.5\n"
        "  throttle_seconds: 0.35\n  auto_run_weekly: false\n  default_days: 90\n"
        "router:\n  dawn_confidence_min: 0.85\n  hydra_confidence_min: 0.0\n"
        "  router_enabled: true\n"
    )
    (tmp_path / "config.yaml").write_text(cfg)
    monkeypatch.setenv("VOLTEDGE_CONFIG", str(tmp_path / "config.yaml"))
    from src import config_loader
    config_loader.load_config(force_reload=True)
    yield
    config_loader.load_config(force_reload=True)


# ── Helpers ────────────────────────────────────────────────────────────────

def _candle(open_p=100.0, high_p=105.0, low_p=99.0, close_p=103.0, vol=50000, date="2026-01-01"):
    return {
        "open": open_p, "high": high_p, "low": low_p,
        "close": close_p, "volume": vol, "date": date,
    }


class _MockRouterDAWN:
    def classify(self, entry): return ("DAWN", 1.0)


class _MockRouterUnrouted:
    def classify(self, entry): return ("UNROUTED", 0.0)


def _scorer(total: float):
    class _S:
        def score(self, entry):
            return ConvictionScore(
                strategy="BACKTEST", symbol=entry.symbol,
                direction="BUY", total=total,
            )
    return _S()


# ── Test 1: momentum_score computed correctly (cold start) ─────────────────

def test_build_entry_momentum_score_cold_start():
    """(close - open) / open = 0.03 for open=100, close=103."""
    entry = build_watchlist_entry("TEST", _candle(open_p=100, close_p=103), [])
    assert abs(entry.momentum_score - 0.03) < 0.001
    assert entry.avg_volume_20d == 0  # fewer than 5 prev candles → 0


# ── Test 2: avg_volume_20d from prev_candles ───────────────────────────────

def test_build_entry_avg_volume_20d():
    """20 prev candles each with volume=10000 → avg_volume_20d=10000."""
    prev = [_candle(vol=10000, date=f"2025-12-{i:02d}") for i in range(1, 21)]
    entry = build_watchlist_entry("TEST", _candle(), prev)
    assert entry.avg_volume_20d == 10000


# ── Test 3: replay_day — TARGET_HIT ───────────────────────────────────────

def test_replay_day_target_hit():
    """high=103 >= open*1.02=102 → TARGET_HIT with pnl=+2.0%."""
    candle = _candle(open_p=100, high_p=103, low_p=99, close_p=102)
    result = replay_day("TEST", candle, [], _MockRouterDAWN(), _scorer(80.0))
    assert result["result"] == "TARGET_HIT"
    assert abs(result["pnl_pct"] - 2.0) < 0.01


# ── Test 4: replay_day — SL_HIT ───────────────────────────────────────────

def test_replay_day_sl_hit():
    """low=98 <= open*0.985=98.5 → SL_HIT with negative pnl."""
    candle = _candle(open_p=100, high_p=100.5, low_p=98.0, close_p=99)
    result = replay_day("TEST", candle, [], _MockRouterDAWN(), _scorer(80.0))
    assert result["result"] == "SL_HIT"
    assert result["pnl_pct"] < 0


# ── Test 5: conviction gate skip ──────────────────────────────────────────

def test_replay_day_conviction_gate():
    """conviction=65 < threshold=70 → skipped with skip_reason=CONVICTION_GATE."""
    result = replay_day("TEST", _candle(), [], _MockRouterDAWN(), _scorer(65.0))
    assert result["skipped"] is True
    assert result["skip_reason"] == "CONVICTION_GATE"


# ── Test 6: UNROUTED skip ─────────────────────────────────────────────────

def test_replay_day_unrouted():
    """router returns UNROUTED → skipped with route=UNROUTED."""
    result = replay_day("TEST", _candle(), [], _MockRouterUnrouted(), _scorer(80.0))
    assert result["skipped"] is True
    assert result["route"] == "UNROUTED"


# ── Test 7: run_backtest aggregation ──────────────────────────────────────

def test_run_backtest_aggregation(monkeypatch):
    """1 symbol × 5 candles: 3 TARGET_HIT, 1 SL_HIT, 1 NEUTRAL → win_rate=0.60."""
    from src.backtest.engine import run_backtest

    candles = [
        _candle(open_p=100, high_p=103, low_p=99, close_p=102, date="2026-01-01"),
        _candle(open_p=100, high_p=103, low_p=99, close_p=102, date="2026-01-02"),
        _candle(open_p=100, high_p=103, low_p=99, close_p=102, date="2026-01-03"),
        _candle(open_p=100, high_p=100.5, low_p=98.0, close_p=99, date="2026-01-04"),
        _candle(open_p=100, high_p=101.0, low_p=99.5, close_p=100.5, date="2026-01-05"),
    ]

    monkeypatch.setattr(
        "src.backtest.engine.fetch_universe_historical",
        lambda from_d, to_d: {"SYM1": candles},
    )

    result = run_backtest(
        days=5,
        persist=False,
        _router_override=_MockRouterDAWN(),
        _scorer_override=_scorer(80.0),
    )

    assert result["win_rate"] == pytest.approx(3 / 5, abs=0.01)
    assert result["by_route"]["DAWN"]["trades"] == 5


# ── Test 8: empty universe → no crash, trades_taken=0 ────────────────────

def test_run_backtest_empty_universe(monkeypatch):
    """Empty universe must not crash and must report trades_taken=0."""
    from src.backtest.engine import run_backtest

    monkeypatch.setattr(
        "src.backtest.engine.fetch_universe_historical",
        lambda from_d, to_d: {},
    )

    result = run_backtest(days=5, persist=False)
    assert result["trades_taken"] == 0
