"""
pcr_tracker.py
--------------
Computes Nifty Put-Call Ratio (PCR) from Kite option chain data.

PCR = Total Put OI / Total Call OI (for near-expiry Nifty options)

Interpretation (contrarian indicator):
  PCR > 1.2  → Extreme fear → Market likely near bottom (bullish)
  PCR 0.8-1.2 → Balanced → Neutral
  PCR < 0.7  → Extreme greed → Market likely near top (bearish)

Uses Kite's kite.quote() or kite.ltp() for option chain data.
Falls back gracefully if options data is unavailable.
"""
import logging
from datetime import datetime, date, timedelta
from typing import Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PCRData:
    total_put_oi: int
    total_call_oi: int
    pcr: float
    signal: str          # "bullish" | "neutral" | "bearish"
    strikes_analyzed: int
    expiry: str
    timestamp: datetime

    @property
    def summary(self) -> str:
        return f"PCR={self.pcr:.2f} ({self.signal}) | PutOI={self.total_put_oi:,} CallOI={self.total_call_oi:,} | {self.strikes_analyzed} strikes"


def _get_next_thursday(from_date: date = None) -> date:
    """Get next Thursday (weekly Nifty expiry day)."""
    d = from_date or date.today()
    days_ahead = 3 - d.weekday()  # Thursday = 3
    if days_ahead <= 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


def _build_option_symbols(spot_price: float, expiry: date, num_strikes: int = 10) -> Tuple[list, list]:
    """
    Build option symbol strings for Kite API.
    Format: NFO:NIFTY{YY}{MON}{DD}{STRIKE}CE / PE
    Example: NFO:NIFTY2504324500CE (Nifty 2025 Apr 03 24500 Call)
    """
    # Round spot to nearest 50
    atm = round(spot_price / 50) * 50

    year = expiry.strftime("%y")
    month = expiry.strftime("%b").upper()[:3]  # APR, MAR, etc.
    day = expiry.strftime("%d")

    # For monthly expiry, Kite uses YYMDD format
    # For weekly, uses YY+month_code+DD
    # Simplified: use YY+M+DD format
    month_map = {
        "JAN": "1", "FEB": "2", "MAR": "3", "APR": "4", "MAY": "5", "JUN": "6",
        "JUL": "7", "AUG": "8", "SEP": "9", "OCT": "O", "NOV": "N", "DEC": "D"
    }
    month_code = month_map.get(month, month[0])
    expiry_str = f"{year}{month_code}{day}"

    calls = []
    puts = []
    for i in range(-num_strikes, num_strikes + 1):
        strike = atm + (i * 50)
        calls.append(f"NFO:NIFTY{expiry_str}{strike}CE")
        puts.append(f"NFO:NIFTY{expiry_str}{strike}PE")

    return calls, puts


def compute_pcr(kite_client, spot_price: float = None) -> Optional[PCRData]:
    """
    Compute Nifty PCR from live option chain OI data.

    Args:
        kite_client: KiteConnect instance (must have access_token set)
        spot_price: Current Nifty spot price. If None, fetched from Kite.

    Returns:
        PCRData object or None if options data unavailable.
    """
    if kite_client is None:
        logger.warning("PCR: No Kite client available")
        return None

    try:
        # Get Nifty spot if not provided
        if spot_price is None:
            nifty_ltp = kite_client.ltp("NSE:NIFTY 50")
            if "NSE:NIFTY 50" in nifty_ltp:
                spot_price = nifty_ltp["NSE:NIFTY 50"]["last_price"]
            else:
                logger.warning("PCR: Could not fetch Nifty spot price")
                return None

        # Get near-expiry date
        expiry = _get_next_thursday()
        if expiry == date.today():
            # It's expiry day — also check next week's
            pass  # Use today's expiry

        # Build option symbols for 10 strikes above and below ATM
        calls, puts = _build_option_symbols(spot_price, expiry, num_strikes=10)

        # Fetch OI for all options (batch call)
        all_symbols = calls + puts
        try:
            quotes = kite_client.quote(all_symbols)
        except Exception as e:
            logger.warning(f"PCR: Option chain fetch failed (may need subscription): {e}")
            return None

        total_call_oi = 0
        total_put_oi = 0
        strikes_found = 0

        for sym in calls:
            if sym in quotes:
                total_call_oi += quotes[sym].get("oi", 0)
                strikes_found += 1

        for sym in puts:
            if sym in quotes:
                total_put_oi += quotes[sym].get("oi", 0)
                strikes_found += 1

        if total_call_oi == 0:
            logger.warning("PCR: No call OI data found — subscription may not support options")
            return None

        pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 0.0

        # Signal
        if pcr > 1.2:
            signal = "bullish"      # Extreme fear → contrarian bullish
        elif pcr > 1.0:
            signal = "mildly_bullish"
        elif pcr > 0.7:
            signal = "neutral"
        elif pcr > 0.5:
            signal = "mildly_bearish"
        else:
            signal = "bearish"      # Extreme greed → contrarian bearish

        return PCRData(
            total_put_oi=total_put_oi,
            total_call_oi=total_call_oi,
            pcr=round(pcr, 3),
            signal=signal,
            strikes_analyzed=strikes_found,
            expiry=expiry.isoformat(),
            timestamp=datetime.now(),
        )

    except Exception as e:
        logger.warning(f"PCR computation failed: {e}")
        return None


def get_pcr_score_modifier(pcr_data: Optional[PCRData]) -> float:
    """
    Returns a score modifier based on PCR.
    Extreme fear (PCR > 1.3) → boost longs by 10%
    Extreme greed (PCR < 0.5) → dampen longs by 15%
    """
    if pcr_data is None:
        return 1.0

    if pcr_data.pcr > 1.3:
        return 1.10  # Everyone is hedging → contrarian long opportunity
    elif pcr_data.pcr > 1.1:
        return 1.05
    elif pcr_data.pcr < 0.5:
        return 0.85  # Everyone is bullish → caution
    elif pcr_data.pcr < 0.7:
        return 0.92
    return 1.0
