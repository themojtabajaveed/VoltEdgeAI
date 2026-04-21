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


# ── Test 1: momentum_score computed correctly ─────────────────

def test_build_entry_momentum_score():
    """(open - prev_close) / prev_close = 0.02 for prev_close=100, open=102."""
    prev = _candle(close_p=100.0)
    curr = _candle(open_p=102.0)
    entry = build_watchlist_entry("TEST", curr, [prev])
    assert abs(entry.momentum_score - 0.02) < 0.001
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
    assert result["pnl_pct"] < result["gross_pnl_pct"]
    assert result["cost_pct"] > 0
    assert abs(result["gross_pnl_pct"] - 2.0) < 0.01
    assert abs(result["pnl_pct"] - (result["gross_pnl_pct"] - result["cost_pct"] - result["slip_pct"])) < 0.001


def test_replay_day_cost_always_positive():
    """Any completed trade (target, stop, neutral) must have cost_pct > 0 and < 0.5%."""
    candle = _candle(open_p=100, high_p=103, low_p=99, close_p=102)
    result = replay_day("TEST", candle, [], _MockRouterDAWN(), _scorer(80.0))
    assert result["cost_pct"] > 0
    assert result["cost_pct"] < 0.5


# ── Test: Slippage logic ──────────────────────────────────────────────────

def test_slippage_stop_worse_than_target():
    """Stop slippage is wider than target slippage, causing worse asymmetric net P&L."""
    # Synthetic target hit w/ gross P&L = +0.0%
    candle_target = _candle(open_p=100, high_p=100, low_p=100, close_p=100)
    # We will override target_price and sl_price through config inside engine,
    # but that's cumbersome. Let's just create a target match and SL match with 0 gross move.
    
    # Actually wait, sl_pct=1.5 and target_pct=2.0 in tests.
    candle_target = _candle(open_p=100.0, high_p=102.0, low_p=100.0, close_p=100.0) # gross pnl = 2.0%
    result_target = replay_day("TEST", candle_target, [], _MockRouterDAWN(), _scorer(80.0))
    
    candle_stop = _candle(open_p=100.0, high_p=100.0, low_p=98.5, close_p=100.0) # gross pnl = -1.5%
    result_stop = replay_day("TEST", candle_stop, [], _MockRouterDAWN(), _scorer(80.0))
    
    # target total gross - net difference (cost + slippage)
    target_fric = result_target["gross_pnl_pct"] - result_target["pnl_pct"]
    stop_fric = result_stop["gross_pnl_pct"] - result_stop["pnl_pct"]
    
    # Stop friction should be strictly worse because stop slippage > target slippage
    assert stop_fric > target_fric


def test_slippage_values_from_config(monkeypatch):
    from src.backtest import signal_replayer
    
    monkeypatch.setattr(signal_replayer, "get_backtest_slippage_config", lambda: {
        "slippage_entry_bps": 10,
        "slippage_exit_target_bps": 5,
        "slippage_exit_stop_bps": 20,
        "slippage_exit_neutral_bps": 3,
    })
    
    # Target Hit -> 10 + 5 = 15 bps (0.15%)
    candle_tgt = _candle(open_p=100, high_p=105, low_p=99, close_p=102)
    res_tgt = replay_day("TEST", candle_tgt, [], _MockRouterDAWN(), _scorer(80.0))
    assert res_tgt["slip_pct"] == pytest.approx(0.15)
    
    # SL Hit -> 10 + 20 = 30 bps (0.30%)
    candle_sl = _candle(open_p=100, high_p=100.5, low_p=90, close_p=95)
    res_sl = replay_day("TEST", candle_sl, [], _MockRouterDAWN(), _scorer(80.0))
    assert res_sl["slip_pct"] == pytest.approx(0.30)
    
    # Neutral -> 10 + 3 = 13 bps (0.13%)
    candle_neu = _candle(open_p=100, high_p=101, low_p=99, close_p=100)
    res_neu = replay_day("TEST", candle_neu, [], _MockRouterDAWN(), _scorer(80.0))
    assert res_neu["slip_pct"] == pytest.approx(0.13)


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


# ── Test 9: get_kite_client() returns None without auth ─────────────────

def test_get_kite_client_returns_none_without_auth(monkeypatch):
    """Missing ZERODHA_API_KEY / ACCESS_TOKEN → None, no crash."""
    from src.backtest import data_loader
    monkeypatch.delenv("ZERODHA_API_KEY", raising=False)
    monkeypatch.delenv("ZERODHA_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(data_loader, "_NO_AUTH_WARNED", False)
    # Prevent load_dotenv from repopulating env from a real .env file.
    monkeypatch.setattr(data_loader, "load_dotenv", lambda *a, **kw: None, raising=False)
    import sys
    # Shim: make dotenv.load_dotenv a no-op inside the function scope.
    fake_dotenv = type(sys)("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    assert data_loader.get_kite_client() is None


# ── Test 10: warm_cache() with mocked fetch returns correct count ───────

def test_warm_cache_counts_symbols_with_data(monkeypatch):
    """warm_cache returns the count of symbols with ≥ 5 candles in cache."""
    from src.backtest import data_loader

    monkeypatch.setattr(data_loader, "get_backtest_universe", lambda: ["A", "B", "C"])
    # Make fetch return 5 candles for A, 3 for B (degraded), 5 for C
    def _fake_fetch(sym, f, t, kite_client=None):
        n = {"A": 5, "B": 3, "C": 5}.get(sym, 0)
        return [{"date": f"2026-01-{i+1:02d}", "open": 100, "high": 101,
                 "low": 99, "close": 100, "volume": 1000} for i in range(n)]
    monkeypatch.setattr(data_loader, "fetch_historical_ohlcv", _fake_fetch)
    monkeypatch.setattr(data_loader, "get_kite_client", lambda: None)
    monkeypatch.setattr("src.config_loader.get_backtest_throttle", lambda: 0.0)

    fetched = data_loader.warm_cache("2025-10-01", "2026-01-01")
    assert fetched == 2  # only A and C have ≥5


# ── Test 11: data quality fields present in run_backtest summary ────────

def test_run_backtest_summary_has_data_quality_fields(monkeypatch):
    """Summary must include symbols_with_data, symbols_cache_only, data_quality_pct."""
    from src.backtest.engine import run_backtest

    # 2 symbols: one with 5 candles (good), one with 2 candles (degraded)
    good_candles = [
        _candle(open_p=100, high_p=103, low_p=99, close_p=102, date=f"2026-01-0{i+1}")
        for i in range(5)
    ]
    bad_candles = good_candles[:2]
    monkeypatch.setattr(
        "src.backtest.engine.fetch_universe_historical",
        lambda from_d, to_d: {"GOOD": good_candles, "BAD": bad_candles},
    )

    result = run_backtest(
        days=5, persist=False,
        _router_override=_MockRouterDAWN(),
        _scorer_override=_scorer(80.0),
    )
    assert result["symbols_with_data"] == 1
    assert result["symbols_cache_only"] == 1
    assert result["data_quality_pct"] == pytest.approx(50.0, abs=0.01)


# ── Walk-Forward tests ────────────────────────────────────────────────────

def test_walk_forward_split_sizes(monkeypatch):
    from src.backtest.walk_forward import run_walk_forward
    from datetime import date, timedelta
    
    calls = []
    def fake_run_backtest(days, persist, end_date=None, _router_override=None, _scorer_override=None):
        calls.append((days, end_date))
        return {"win_rate": 0.5, "avg_pnl_pct": 1.0}
        
    import src.backtest.walk_forward
    monkeypatch.setattr(src.backtest.walk_forward, "run_backtest", fake_run_backtest)
    
    run_walk_forward(total_days=90, split_pct=0.67, persist=False)
    
    assert len(calls) == 2
    is_days, is_end_date = calls[0]
    oos_days, oos_end_date = calls[1]
    
    assert is_days == 60
    assert oos_days == 30
    assert is_days + oos_days == 90
    assert is_end_date is None
    assert oos_end_date == date.today() - timedelta(days=60)


def test_walk_forward_returns_delta(monkeypatch):
    from src.backtest.walk_forward import run_walk_forward
    
    def fake_run_backtest(days, persist, end_date=None, _router_override=None, _scorer_override=None):
        if end_date is None:  # IS
            return {"win_rate": 0.60, "avg_pnl_pct": 2.0}
        else: # OOS
            return {"win_rate": 0.50, "avg_pnl_pct": 1.0}
            
    import src.backtest.walk_forward
    monkeypatch.setattr(src.backtest.walk_forward, "run_backtest", fake_run_backtest)
    
    summary = run_walk_forward(total_days=90, split_pct=0.67, persist=False)
    delta = summary["delta"]
    assert delta["win_rate_delta"] == pytest.approx(0.10)
    assert delta["avg_pnl_delta"] == pytest.approx(1.0)


def test_end_date_parameter(monkeypatch):
    from src.backtest.engine import run_backtest
    from datetime import date, timedelta
    
    called_with_from = None
    called_with_to = None
    
    def fake_fetch(from_d, to_d):
        nonlocal called_with_from, called_with_to
        called_with_from = from_d
        called_with_to = to_d
        return {}
        
    monkeypatch.setattr("src.backtest.engine.fetch_universe_historical", fake_fetch)
    
    end_date = date.today() - timedelta(days=30)
    run_backtest(days=10, persist=False, end_date=end_date)
    
    assert called_with_to == end_date.isoformat()
    assert called_with_from == (end_date - timedelta(days=10)).isoformat()


# ── Test: Scorer Freebie Removed ──────────────────────────────────────────

def test_scorer_no_free_base():
    from src.backtest.engine import BacktestConvictionScorer
    from src.strategies.base import WatchlistEntry
    
    entry = WatchlistEntry(symbol="TEST", direction="BUY", gap_pct=0.0, volume_signal="NORMAL", route="HYDRA")
    scorer = BacktestConvictionScorer()
    score = scorer.score(entry)
    assert score.total == 0.0

def test_scorer_dawn_route_bonus():
    from src.backtest.engine import BacktestConvictionScorer
    from src.strategies.base import WatchlistEntry
    
    entry = WatchlistEntry(symbol="TEST", direction="BUY", gap_pct=0.0, volume_signal="NORMAL", route="DAWN")
    scorer = BacktestConvictionScorer()
    score = scorer.score(entry)
    assert score.total == 15.0

def test_scorer_threshold_requires_gap():
    from src.backtest.engine import BacktestConvictionScorer
    from src.strategies.base import WatchlistEntry
    
    entry = WatchlistEntry(symbol="TEST", direction="BUY", gap_pct=2.0, volume_signal="SURGE", route="DAWN")
    scorer = BacktestConvictionScorer()
    score = scorer.score(entry)
    assert score.total == 43.0  # 8.0 + 20.0 + 15.0

def test_scorer_strong_signal_passes():
    from src.backtest.engine import BacktestConvictionScorer
    from src.strategies.base import WatchlistEntry
    
    entry = WatchlistEntry(symbol="TEST", direction="BUY", gap_pct=10.0, volume_signal="SURGE", route="DAWN")
    scorer = BacktestConvictionScorer()
    score = scorer.score(entry)
    assert score.total == 75.0  # 40.0 + 20.0 + 15.0
