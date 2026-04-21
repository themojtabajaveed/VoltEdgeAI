"""
__main__.py — Backtest CLI entry point
Run: python -m src.backtest [--days 90] [--no-persist] [--warm-cache]
"""
import argparse
import logging
from datetime import date, timedelta

from src.backtest.engine import print_backtest_report, run_backtest
from src.config_loader import get_backtest_days


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="VoltEdgeAI backtesting harness")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Calendar days to backtest (default: backtest.default_days in config.yaml)",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Print report but do not write JSON to data/",
    )
    parser.add_argument(
        "--warm-cache",
        action="store_true",
        help="Populate the SQLite history cache before running the backtest",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Run train/test walk-forward split instead of normal backtest",
    )
    args = parser.parse_args()

    days = args.days if args.days is not None else get_backtest_days()
    persist = not args.no_persist

    if args.warm_cache:
        from src.backtest.data_loader import warm_cache
        today = date.today()
        to_d = today.isoformat()
        from_d = (today - timedelta(days=days)).isoformat()
        print(f"🔥 Warming cache for {from_d} → {to_d} ...")
        fetched = warm_cache(from_d, to_d)
        print(f"✅ Cache warm complete: {fetched} symbols have sufficient data")

    if args.walk_forward:
        from src.backtest.walk_forward import run_walk_forward
        summary = run_walk_forward(total_days=days, persist=persist)
        print("════════════════════════════════════")
        print("VoltEdgeAI Walk-Forward Backtest")
        print("════════════════════════════════════")
        print(f"IS Win Rate : {summary['is_result'].get('win_rate', 0):.2%}")
        print(f"OOS Win Rate: {summary['oos_result'].get('win_rate', 0):.2%}")
        print(f"IS Avg P&L  : {summary['is_result'].get('avg_pnl_pct', 0):+.2f}%")
        print(f"OOS Avg P&L : {summary['oos_result'].get('avg_pnl_pct', 0):+.2f}%")
        print("────────────────────────────────────")
        print("Delta (IS - OOS):")
        print(f"Win Rate Delta: {summary['delta']['win_rate_delta']:+.2%}")
        print(f"Avg P&L Delta : {summary['delta']['avg_pnl_delta']:+.2f}%")
        print("════════════════════════════════════")
    else:
        summary = run_backtest(days=days, persist=persist)
        print_backtest_report(summary)


if __name__ == "__main__":
    main()
