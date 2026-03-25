"""
sector_guard.py
---------------
Prevents sector concentration risk.

Rule: Max 2 positions in the same sector.
If we're already long INFY and TCS (both IT), reject WIPRO.
This prevents one sector-wide news event from killing multiple positions.
"""
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

# NSE sector classification (subset — top ~100 stocks)
SECTOR_MAP: Dict[str, str] = {
    # IT
    "INFY": "IT", "TCS": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTIM": "IT", "MPHASIS": "IT", "COFORGE": "IT",
    "PERSISTENT": "IT",
    # Banking
    "HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "KOTAKBANK": "BANKING",
    "SBIN": "BANKING", "AXISBANK": "BANKING", "INDUSINDBK": "BANKING",
    "BANDHANBNK": "BANKING", "PNB": "BANKING", "BANKBARODA": "BANKING",
    "IDFCFIRSTB": "BANKING", "FEDERALBNK": "BANKING",
    # NBFC / Financial
    "BAJFINANCE": "NBFC", "BAJAJFINSV": "NBFC", "SBILIFE": "NBFC",
    "HDFCLIFE": "NBFC", "SHRIRAMFIN": "NBFC", "CHOLAFIN": "NBFC",
    "MUTHOOTFIN": "NBFC",
    # Energy / Oil
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "BPCL": "ENERGY",
    "IOC": "ENERGY", "GAIL": "ENERGY", "HINDPETRO": "ENERGY",
    "ADANIGREEN": "ENERGY", "TATAPOWER": "ENERGY", "NTPC": "ENERGY",
    "POWERGRID": "ENERGY",
    # Auto
    "MARUTI": "AUTO", "TATAMOTORS": "AUTO", "M&M": "AUTO",
    "BAJAJ-AUTO": "AUTO", "HEROMOTOCO": "AUTO", "EICHERMOT": "AUTO",
    "ASHOKLEY": "AUTO", "TVSMOTOR": "AUTO",
    # Pharma
    "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA",
    "DIVISLAB": "PHARMA", "APOLLOHOSP": "PHARMA", "BIOCON": "PHARMA",
    "LUPIN": "PHARMA", "AUROPHARMA": "PHARMA",
    # Metals
    "TATASTEEL": "METALS", "JSWSTEEL": "METALS", "HINDALCO": "METALS",
    "VEDL": "METALS", "COALINDIA": "METALS", "NMDC": "METALS",
    "SAIL": "METALS",
    # FMCG
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    "TATACONSUM": "FMCG", "GODREJCP": "FMCG",
    # Telecom
    "BHARTIARTL": "TELECOM", "IDEA": "TELECOM",
    # Infra / Cement
    "LT": "INFRA", "ULTRACEMCO": "INFRA", "GRASIM": "INFRA",
    "ADANIENT": "INFRA", "ADANIPORTS": "INFRA", "AMBUJACEM": "INFRA",
    "ACC": "INFRA", "DLF": "INFRA",
    # Insurance
    "SBILIFE": "INSURANCE", "HDFCLIFE": "INSURANCE", "ICICIPRULI": "INSURANCE",
    # Misc
    "TITAN": "CONSUMER", "ASIANPAINT": "CONSUMER", "PIDILITIND": "CONSUMER",
    "UPL": "AGRI",
}

MAX_PER_SECTOR = 2


def get_sector(symbol: str) -> str:
    """Return the sector for a symbol. Unknown symbols get 'OTHER'."""
    return SECTOR_MAP.get(symbol.upper(), "OTHER")


def check_sector_concentration(
    symbol: str,
    open_position_symbols: List[str],
    max_per_sector: int = MAX_PER_SECTOR,
) -> tuple:
    """
    Check if adding this symbol would breach sector concentration limits.

    Args:
        symbol: the stock we want to trade
        open_position_symbols: list of symbols we already have positions in
        max_per_sector: max positions per sector (default 2)

    Returns:
        (allowed, reason)
    """
    new_sector = get_sector(symbol)

    if new_sector == "OTHER":
        return True, f"{symbol} has no sector mapping — allowed"

    same_sector = [s for s in open_position_symbols if get_sector(s) == new_sector]

    if len(same_sector) >= max_per_sector:
        return False, (
            f"Sector concentration: already have {len(same_sector)} {new_sector} "
            f"positions ({', '.join(same_sector)}). Max={max_per_sector}."
        )

    return True, f"{new_sector} sector: {len(same_sector)}/{max_per_sector} slots used"
