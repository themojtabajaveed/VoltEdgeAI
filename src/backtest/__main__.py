"""
__main__.py — Backtest CLI entry point
Run: python -m src.backtest [--days 90] [--no-persist]
"""
import argparse
import logging

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
    args = parser.parse_args()

    days = args.days if args.days is not None else get_backtest_days()
    persist = not args.no_persist

    summary = run_backtest(days=days, persist=persist)
    print_backtest_report(summary)


if __name__ == "__main__":
    main()
