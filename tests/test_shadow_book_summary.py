"""
test_shadow_book_summary.py — C2 shadow_book_summary 5-section report.

All file I/O uses tmp_path. Real DBs and subprocess calls are mocked.
"""
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.reports import shadow_book_summary as sbs

TARGET_DATE = date(2026, 4, 28)
DATE_STR = "2026-04-28"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_dirs(tmp_path, monkeypatch):
    """Redirect all file-path globals to tmp_path sub-dirs."""
    monkeypatch.setattr(sbs, "REPORTS_DIR", tmp_path / "data/reports")
    monkeypatch.setattr(sbs, "SIGNALS_DIR", tmp_path / "logs/conviction_signals")
    monkeypatch.setattr(sbs, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "logs/conviction_signals").mkdir(parents=True)


@pytest.fixture()
def empty_db(tmp_path):
    """voltedgeai.db with empty trade_records and decision_records tables."""
    db_path = tmp_path / "voltedgeai.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, pnl REAL, strategy TEXT,"
        " exit_reason TEXT, exit_time TEXT)"
    )
    conn.execute(
        "CREATE TABLE decision_records (id INTEGER PRIMARY KEY, created_at TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


def _write_trades(db_path: Path, rows: list) -> None:
    conn = sqlite3.connect(str(db_path))
    for r in rows:
        conn.execute(
            "INSERT INTO trade_records (symbol, pnl, strategy, exit_reason, exit_time)"
            " VALUES (?, ?, ?, ?, ?)",
            r,
        )
    conn.commit()
    conn.close()


def _mock_db_conn(db_path: Path, monkeypatch):
    monkeypatch.setattr(sbs, "_get_db_conn", lambda: sqlite3.connect(str(db_path)))


def _mock_subprocess(monkeypatch, is_active: bool = True, error_count: int = 0):
    def fake_run(cmd, **kw):
        result = MagicMock()
        if "is-active" in cmd:
            result.stdout = "active\n" if is_active else "inactive\n"
        elif "journalctl" in cmd:
            result.stdout = " ERROR " * error_count
        else:
            result.stdout = ""
        return result
    monkeypatch.setattr(sbs.subprocess, "run", fake_run)


# ── Test: empty DB, no signals ────────────────────────────────────────────────

def test_empty_db_returns_safe_defaults(tmp_path, empty_db, monkeypatch):
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=False)
    assert result is not None
    out = json.loads(Path(result).read_text())

    assert out["date"] == DATE_STR
    t = out["trades"]
    assert t["total"] == 0 and t["wins"] == 0 and t["pnl_inr"] == 0.0
    assert out["signals"]["total"] == 0
    assert out["cumulative"]["trades_to_date"] == 0
    assert out["cumulative"]["trades_remaining_to_gate"] == 30
    assert out["tomorrow_watch"]["near_gate_signals"] == []


# ── Test: single winning trade ────────────────────────────────────────────────

def test_single_winning_trade(tmp_path, empty_db, monkeypatch):
    _write_trades(empty_db, [("TCS", 850.0, "DAWN", "TARGET", f"{DATE_STR} 15:00:00")])
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=False)
    out = json.loads(Path(result).read_text())

    t = out["trades"]
    assert t["total"] == 1
    assert t["wins"] == 1
    assert t["losses"] == 0
    assert t["win_rate_pct"] == 100.0
    assert t["pnl_inr"] == 850.0
    assert t["best"]["symbol"] == "TCS"
    assert t["exit_reasons"].get("TARGET") == 1


# ── Test: single losing trade ────────────────────────────────────────────────

def test_single_losing_trade(tmp_path, empty_db, monkeypatch):
    _write_trades(empty_db, [("WIPRO", -420.0, "VIPER", "STOP_LOSS", f"{DATE_STR} 11:00:00")])
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=False)
    out = json.loads(Path(result).read_text())

    t = out["trades"]
    assert t["total"] == 1
    assert t["wins"] == 0
    assert t["losses"] == 1
    assert t["win_rate_pct"] == 0.0
    assert t["pnl_inr"] == -420.0
    assert t["worst"]["symbol"] == "WIPRO"


# ── Test: mixed trades win rate ───────────────────────────────────────────────

def test_mixed_trades_win_rate(tmp_path, empty_db, monkeypatch):
    rows = [
        ("TCS",   800.0, "DAWN",  "TARGET",    f"{DATE_STR} 14:00:00"),
        ("WIPRO", -300.0, "VIPER", "STOP_LOSS",  f"{DATE_STR} 11:00:00"),
        ("INFY",   100.0, "HYDRA", "TIME_EXIT",  f"{DATE_STR} 15:25:00"),
        ("HDFCB", -200.0, "VIPER", "STOP_LOSS",  f"{DATE_STR} 10:30:00"),
    ]
    _write_trades(empty_db, rows)
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=False)
    out = json.loads(Path(result).read_text())

    t = out["trades"]
    assert t["total"] == 4
    assert t["wins"] == 2
    assert t["losses"] == 2
    assert t["win_rate_pct"] == 50.0
    assert t["pnl_inr"] == pytest.approx(400.0)


# ── Test: missing signals JSON ────────────────────────────────────────────────

def test_missing_signals_json_returns_zeros(tmp_path, empty_db, monkeypatch):
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=False)
    out = json.loads(Path(result).read_text())

    s = out["signals"]
    assert s["total"] == 0
    assert s["conviction_mean"] == 0.0
    assert s["gate_hits"] == 0


# ── Test: signals JSON present ────────────────────────────────────────────────

def test_signals_section_computed(tmp_path, empty_db, monkeypatch):
    signals_path = Path(sbs.SIGNALS_DIR) / f"{DATE_STR}_signals.json"
    signals = [
        {"symbol": "TCS",   "last_conviction": 75.0, "status": "TRIGGERED", "strategy": "HYDRA"},
        {"symbol": "WIPRO",  "last_conviction": 63.0, "status": "EXPIRED",   "strategy": "VIPER"},
        {"symbol": "INFY",   "last_conviction": 50.0, "status": "EXPIRED",   "strategy": "VIPER"},
    ]
    signals_path.write_text(json.dumps(signals))
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=False)
    out = json.loads(Path(result).read_text())

    s = out["signals"]
    assert s["total"] == 3
    assert s["gate_hits"] == 1
    assert s["above_70"] == 1
    assert s["above_62"] == 2
    assert s["above_55"] == 2  # 50.0 < 55, so only TCS(75) and WIPRO(63) qualify
    assert s["conviction_max"] == 75.0


# ── Test: cumulative counts all historical trades ────────────────────────────

def test_cumulative_counts_all_time(tmp_path, empty_db, monkeypatch):
    rows = [
        ("TCS",  500.0, "DAWN", "TARGET", "2026-04-22 14:00:00"),
        ("INFY", -200.0, "VIPER", "STOP_LOSS", "2026-04-23 11:00:00"),
        ("TCS",  300.0, "HYDRA", "TARGET", f"{DATE_STR} 14:30:00"),
    ]
    _write_trades(empty_db, rows)
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=False)
    out = json.loads(Path(result).read_text())

    c = out["cumulative"]
    assert c["trades_to_date"] == 3
    assert c["win_rate_pct"] == pytest.approx(66.7, abs=0.1)
    assert c["pnl_inr"] == pytest.approx(600.0)
    assert c["trades_remaining_to_gate"] == 27


# ── Test: dry-run prints JSON, no file written ───────────────────────────────

def test_dry_run_no_file_written(tmp_path, empty_db, monkeypatch, capsys):
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=True)
    assert result is None
    captured = capsys.readouterr()
    assert "shadow_summary" not in str(list((tmp_path / "data/reports").iterdir())) if (tmp_path / "data/reports").exists() else True
    parsed = json.loads(captured.out.split("\n", 1)[1])  # skip the log_line
    assert parsed["date"] == DATE_STR


# ── Test: system section service_active ──────────────────────────────────────

def test_system_section_service_active(tmp_path, empty_db, monkeypatch):
    _mock_db_conn(empty_db, monkeypatch)
    _mock_subprocess(monkeypatch, is_active=True, error_count=2)

    result = sbs.generate_shadow_summary(date_str=DATE_STR, dry_run=False)
    out = json.loads(Path(result).read_text())

    assert out["system"]["service_active"] is True
    assert out["system"]["error_count_today"] == 2
