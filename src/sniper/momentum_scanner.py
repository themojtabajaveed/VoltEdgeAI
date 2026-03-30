"""
momentum_scanner.py
-------------------
Runs once at 09:30 AM during market hours.
Uses the Kite Connect REST API to scan **all NSE-traded stocks** and
returns the top 10 gainers and top 10 losers by intraday % change,
filtered by a minimum volume threshold to avoid thin-air spikes.
"""
import os
import logging
from dataclasses import dataclass
from typing import List, Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# --- Thresholds ---
MIN_VOLUME           = 500_000      # Minimum shares traded so far today (thin stocks excluded)
MIN_PRICE_RUPEES     = 50.0         # Penny stock filter
TOP_N                = 10           # Top N gainers + Top N losers


@dataclass
class CandidateStock:
    symbol: str
    last_price: float
    prev_close: float
    pct_change: float   # positive = gainer, negative = loser
    volume: int
    direction: str      # "LONG" | "SHORT"


def fetch_top_movers(kite_client=None, access_token: Optional[str] = None) -> dict:
    """
    Query Kite Connect's quote API over all NSE instruments.

    Returns:
        {
            "gainers": List[CandidateStock],   # Top 10 by % change (highest first)
            "losers":  List[CandidateStock],   # Top 10 by % change (most negative first)
        }
    """
    try:
        if kite_client:
            kite = kite_client
        else:
            load_dotenv()
            api_key      = os.getenv("ZERODHA_API_KEY")
            access_token = access_token or os.getenv("ZERODHA_ACCESS_TOKEN")

            if not api_key or not access_token:
                logger.error("Kite credentials missing — cannot run momentum_scanner.")
                return {"gainers": [], "losers": []}

            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=api_key)
            kite.set_access_token(access_token)

        # Fetch all NSE instruments
        instruments = kite.instruments("NSE")
        # Keep only EQ (equity) instruments above the price floor
        eq_symbols = [
            f"NSE:{i['tradingsymbol']}"
            for i in instruments
            if i.get("instrument_type") == "EQ"
        ]

        logger.info(f"Scanner: fetching quotes for {len(eq_symbols)} NSE EQ instruments…")

        # Kite allows up to 500 symbols per quote call; batch them
        all_quotes = {}
        BATCH = 450
        for i in range(0, len(eq_symbols), BATCH):
            batch = eq_symbols[i : i + BATCH]
            try:
                q = kite.quote(batch)
                all_quotes.update(q)
            except Exception as e:
                logger.warning(f"Quote batch {i}–{i+BATCH} failed: {e}")

        candidates: List[CandidateStock] = []
        for key, q in all_quotes.items():
            symbol     = key.replace("NSE:", "")
            last_price = q.get("last_price", 0.0)
            prev_close = q.get("ohlc", {}).get("close", 0.0)
            volume     = q.get("volume", 0)

            if prev_close == 0 or last_price < MIN_PRICE_RUPEES or volume < MIN_VOLUME:
                continue

            pct_change = (last_price - prev_close) / prev_close * 100

            candidates.append(CandidateStock(
                symbol=symbol,
                last_price=last_price,
                prev_close=prev_close,
                pct_change=round(pct_change, 2),
                volume=volume,
                direction="LONG" if pct_change > 0 else "SHORT",
            ))

        # Sort and slice
        gainers = sorted(
            [c for c in candidates if c.pct_change > 0],
            key=lambda x: x.pct_change, reverse=True
        )[:TOP_N]

        losers = sorted(
            [c for c in candidates if c.pct_change < 0],
            key=lambda x: x.pct_change
        )[:TOP_N]

        logger.info(
            f"Scanner complete | Gainers: {[g.symbol for g in gainers]} | "
            f"Losers: {[l.symbol for l in losers]}"
        )
        return {"gainers": gainers, "losers": losers}

    except Exception as e:
        logger.error(f"momentum_scanner failed: {e}")
        return {"gainers": [], "losers": []}
