"""
populate_history.py — One-time backfill of the SQLite history cache
--------------------------------------------------------------------
Fetches 90 days of daily OHLCV for every symbol in the backtest universe
via the existing KiteConnect utility.  Safe to run multiple times
(SQLite INSERT OR IGNORE prevents duplicates).

Usage:
    python scripts/populate_history.py [--days 90]

Requires ZERODHA_API_KEY + ZERODHA_ACCESS_TOKEN in .env.  Approximate
runtime: 201 symbols × 0.35 s throttle = ~70 seconds.
"""
import argparse
import logging
import sys
from datetime import date, timedelta


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Populate KiteConnect history cache")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Calendar days to backfill (default: backtest.default_days in config.yaml)",
    )
    args = parser.parse_args()

    from src.backtest.data_loader import get_kite_client, warm_cache
    from src.config_loader import get_backtest_days

    days = args.days if args.days is not None else get_backtest_days()

    kite = get_kite_client()
    if kite is None:
        print("❌ No Kite auth available. Set ZERODHA_API_KEY + ZERODHA_ACCESS_TOKEN in .env")
        print("   Tip: run `python -m src.tools.auto_login` first to refresh the token.")
        return 1

    today = date.today()
    to_d = today.isoformat()
    from_d = (today - timedelta(days=days)).isoformat()

    print(f"🔥 Populating history cache: {from_d} → {to_d} ({days} days)")
    fetched = warm_cache(from_d, to_d)
    print(f"✅ Done: {fetched} symbols have ≥5 daily candles cached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
