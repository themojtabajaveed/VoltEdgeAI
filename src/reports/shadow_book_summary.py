"""
shadow_book_summary.py — Daily operator heartbeat report.

Generates a 5-section JSON summary and one greppable INFO log line.

Runnable as:
    python3 -m src.reports.shadow_book_summary            # today IST
    python3 -m src.reports.shadow_book_summary 2026-04-25 # specific date
    python3 -m src.reports.shadow_book_summary --dry-run  # print JSON, no write
"""
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz  # type: ignore
    IST = pytz.timezone("Asia/Kolkata")

REPORTS_DIR = Path("data/reports")
SIGNALS_DIR = Path("logs/conviction_signals")
DATA_DIR = Path("data")
_DB_PATHS = ["voltedgeai.db", "data/voltedge.db"]


def _today_ist() -> date:
    return datetime.now(IST).date()


def _get_db_conn() -> Optional[sqlite3.Connection]:
    """Return a raw sqlite3 connection to voltedgeai.db, or None if not found."""
    for path in _DB_PATHS:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return sqlite3.connect(path)
    return None


# ── Section 1: today's paper trades ──────────────────────────────────────────

def _section_trades(target_date: date) -> Dict[str, Any]:
    empty: Dict[str, Any] = {
        "total": 0, "by_strategy": {}, "wins": 0, "losses": 0,
        "win_rate_pct": 0.0, "pnl_inr": 0.0, "pnl_per_trade_inr": 0.0,
        "best": None, "worst": None, "exit_reasons": {},
    }
    try:
        conn = _get_db_conn()
        if conn is None:
            return empty
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT symbol, pnl, strategy, exit_reason FROM trade_records"
            " WHERE date(exit_time) = ?",
            (target_date.isoformat(),),
        ).fetchall()
        conn.close()

        if not rows:
            return empty

        wins = losses = 0
        pnl_vals: List[tuple] = []
        by_strategy: Dict[str, int] = {}
        exit_reasons: Dict[str, int] = {}

        for symbol, pnl, strategy, exit_reason in rows:
            pnl = float(pnl or 0)
            pnl_vals.append((symbol, pnl, strategy or "UNKNOWN"))
            wins += pnl > 0
            losses += pnl <= 0
            s = strategy or "UNKNOWN"
            by_strategy[s] = by_strategy.get(s, 0) + 1
            r = exit_reason or "UNKNOWN"
            exit_reasons[r] = exit_reasons.get(r, 0) + 1

        total = len(pnl_vals)
        pnl_total = sum(p for _, p, _ in pnl_vals)
        best_row = max(pnl_vals, key=lambda x: x[1])
        worst_row = min(pnl_vals, key=lambda x: x[1])

        return {
            "total": total,
            "by_strategy": by_strategy,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / total * 100, 1),
            "pnl_inr": round(pnl_total, 2),
            "pnl_per_trade_inr": round(pnl_total / total, 2),
            "best": {"symbol": best_row[0], "pnl_inr": round(best_row[1], 2), "strategy": best_row[2]},
            "worst": {"symbol": worst_row[0], "pnl_inr": round(worst_row[1], 2), "strategy": worst_row[2]},
            "exit_reasons": exit_reasons,
        }
    except Exception as e:
        logger.warning(f"[shadow_summary] section_trades failed: {e}")
        return empty


# ── Section 2: conviction signals ─────────────────────────────────────────────

def _section_signals(target_date: date) -> Dict[str, Any]:
    empty: Dict[str, Any] = {
        "total": 0, "conviction_mean": 0.0, "conviction_max": 0.0,
        "above_55": 0, "above_62": 0, "above_70": 0,
        "gate_hits": 0, "juror_pipeline_alive": False, "v2_disabled": True,
    }
    try:
        signals_path = SIGNALS_DIR / f"{target_date.isoformat()}_signals.json"
        convictions: List[float] = []
        gate_hits = 0

        if signals_path.exists():
            try:
                with open(signals_path) as f:
                    signals = json.load(f) or []
                for s in signals:
                    c = float(s.get("last_conviction") or 0)
                    convictions.append(c)
                    if s.get("status") == "TRIGGERED":
                        gate_hits += 1
            except Exception as sig_e:
                logger.warning(f"[shadow_summary] signals JSON parse failed: {sig_e}")

        total = len(convictions)
        conv_mean = round(sum(convictions) / total, 1) if total else 0.0
        conv_max = round(max(convictions), 1) if convictions else 0.0

        # Juror pipeline liveness
        juror_alive = False
        try:
            conn = _get_db_conn()
            if conn is not None:
                n = conn.execute(
                    "SELECT COUNT(*) FROM decision_records WHERE date(created_at) = ?",
                    (target_date.isoformat(),),
                ).fetchone()[0]
                conn.close()
                juror_alive = n > 0
        except Exception:
            juror_alive = False

        v2_disabled = True
        try:
            from src.config_loader import get_v2_discovery_enabled
            v2_disabled = not get_v2_discovery_enabled()
        except Exception:
            v2_disabled = True

        return {
            "total": total,
            "conviction_mean": conv_mean,
            "conviction_max": conv_max,
            "above_55": sum(1 for c in convictions if c >= 55),
            "above_62": sum(1 for c in convictions if c >= 62),
            "above_70": sum(1 for c in convictions if c >= 70),
            "gate_hits": gate_hits,
            "juror_pipeline_alive": juror_alive,
            "v2_disabled": v2_disabled,
        }
    except Exception as e:
        logger.warning(f"[shadow_summary] section_signals failed: {e}")
        return empty


# ── Section 3: cumulative all-time stats ──────────────────────────────────────

def _section_cumulative() -> Dict[str, Any]:
    empty: Dict[str, Any] = {
        "trades_to_date": 0, "win_rate_pct": 0.0,
        "pnl_inr": 0.0, "trades_remaining_to_gate": 30,
    }
    try:
        conn = _get_db_conn()
        if conn is None:
            return empty
        rows = conn.execute(
            "SELECT pnl FROM trade_records WHERE exit_time IS NOT NULL"
        ).fetchall()
        conn.close()

        total = len(rows)
        if total == 0:
            return empty

        pnl_values = [float(r[0] or 0) for r in rows]
        wins = sum(1 for p in pnl_values if p > 0)

        return {
            "trades_to_date": total,
            "win_rate_pct": round(wins / total * 100, 1),
            "pnl_inr": round(sum(pnl_values), 2),
            "trades_remaining_to_gate": max(0, 30 - total),
        }
    except Exception as e:
        logger.warning(f"[shadow_summary] section_cumulative failed: {e}")
        return empty


# ── Section 4: system health ──────────────────────────────────────────────────

def _section_system(target_date: date) -> Dict[str, Any]:
    empty: Dict[str, Any] = {
        "service_active": False, "error_count_today": 0,
        "pattern_db_rows_added_today": 0, "shadow_book_writes_today": 0,
    }
    try:
        service_active = False
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "voltedge.service"],
                capture_output=True, text=True, timeout=5,
            )
            service_active = result.stdout.strip() == "active"
        except Exception:
            pass

        error_count = 0
        try:
            result = subprocess.run(
                ["journalctl", "-u", "voltedge.service",
                 "--since", target_date.isoformat(), "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            error_count = result.stdout.upper().count(" ERROR ")
        except Exception:
            pass

        pdb_today = 0
        try:
            pdb_path = DATA_DIR / "pattern_db.json"
            if pdb_path.exists():
                with open(pdb_path) as f:
                    pdb = json.load(f)
                pdb_today = sum(
                    1 for r in pdb
                    if isinstance(r, dict) and r.get("date") == target_date.isoformat()
                )
        except Exception:
            pass

        shadow_writes = 0
        try:
            shadow_path = DATA_DIR / f"sl_shadow_{target_date.isoformat()}.json"
            if shadow_path.exists():
                with open(shadow_path) as f:
                    shadow_writes = len(json.load(f).get("closed", []))
        except Exception:
            pass

        return {
            "service_active": service_active,
            "error_count_today": error_count,
            "pattern_db_rows_added_today": pdb_today,
            "shadow_book_writes_today": shadow_writes,
        }
    except Exception as e:
        logger.warning(f"[shadow_summary] section_system failed: {e}")
        return empty


# ── Section 5: tomorrow watch ─────────────────────────────────────────────────

def _section_tomorrow_watch(target_date: date) -> Dict[str, Any]:
    empty: Dict[str, Any] = {"near_gate_signals": []}
    try:
        signals_path = SIGNALS_DIR / f"{target_date.isoformat()}_signals.json"
        if not signals_path.exists():
            return empty
        with open(signals_path) as f:
            signals = json.load(f) or []
        expired = [s for s in signals if s.get("status") == "EXPIRED"]
        top3 = sorted(expired, key=lambda s: float(s.get("last_conviction") or 0), reverse=True)[:3]
        return {
            "near_gate_signals": [
                {
                    "symbol": s.get("symbol", ""),
                    "conviction": round(float(s.get("last_conviction") or 0), 1),
                    "strategy": s.get("strategy", ""),
                }
                for s in top3
            ]
        }
    except Exception as e:
        logger.warning(f"[shadow_summary] section_tomorrow_watch failed: {e}")
        return empty


# ── Public entry point ────────────────────────────────────────────────────────

def generate_shadow_summary(
    date_str: Optional[str] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Build and persist the daily shadow_book_summary JSON.

    Args:
        date_str: YYYY-MM-DD. Defaults to today IST.
        dry_run:  print JSON to stdout, skip writing file.

    Returns:
        Absolute path of written file, or None on error / dry-run.
    """
    try:
        target_date = date.fromisoformat(date_str) if date_str else _today_ist()
    except (ValueError, TypeError):
        logger.warning(f"[shadow_summary] invalid date '{date_str}', using today IST")
        target_date = _today_ist()

    trades = _section_trades(target_date)
    signals = _section_signals(target_date)
    cumulative = _section_cumulative()
    system = _section_system(target_date)
    tomorrow = _section_tomorrow_watch(target_date)

    payload: Dict[str, Any] = {
        "date": target_date.isoformat(),
        "generated_at": datetime.now(IST).isoformat(),
        "trades": trades,
        "signals": signals,
        "cumulative": cumulative,
        "system": system,
        "tomorrow_watch": tomorrow,
    }

    log_line = (
        f"[shadow] date={target_date.isoformat()} "
        f"trades={trades['total']} "
        f"wins={trades['wins']} "
        f"win_rate={trades['win_rate_pct']}% "
        f"pnl=Rs{trades['pnl_inr']:.0f} "
        f"conv_mean={signals['conviction_mean']} "
        f"conv_max={signals['conviction_max']} "
        f"gate_hits={signals['gate_hits']} "
        f"cum_trades={cumulative['trades_to_date']}/30"
    )
    logger.info(log_line)
    print(log_line)

    if dry_run:
        print(json.dumps(payload, indent=2))
        return None

    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORTS_DIR / f"shadow_summary_{target_date.isoformat()}.json"
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info(f"[shadow_summary] Written to {out_path}")
        return str(out_path)
    except Exception as write_e:
        logger.error(f"[shadow_summary] write failed: {write_e}")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    date_arg = next((a for a in args if not a.startswith("--")), None)
    generate_shadow_summary(date_str=date_arg, dry_run=dry_run)
