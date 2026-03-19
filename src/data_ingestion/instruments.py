from dataclasses import dataclass
from typing import Dict, List, Optional
import os
import pandas as pd

DEFAULT_INSTRUMENTS_PATH = os.path.join("data", "zerodha_instruments.csv")

def load_instruments_csv(path: str = DEFAULT_INSTRUMENTS_PATH) -> pd.DataFrame:
    """Load the Zerodha instruments CSV into a DataFrame.
    Expect the standard Kite columns (instrument_token, exchange, tradingsymbol, name, segment, etc.).
    Raise FileNotFoundError with a clear message if the file is missing.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Instruments file not found at {path}")
        
    return pd.read_csv(path)

def build_symbol_token_map(
    df: pd.DataFrame,
    exchange: str = "NSE",
    segment_prefix: str = "EQ"
) -> Dict[str, int]:
    """
    From the full instruments DataFrame, build a mapping of
    tradingsymbol -> instrument_token for NSE cash equities.
    Filter rows where:
    - exchange == exchange (default "NSE")
    - segment contains "NSE" and "EQ" (or segment_prefix)
    Return a dict like {"RELIANCE": 738561, ...}.
    """
    # Filter by exchange
    filtered = df[df['exchange'] == exchange]
    
    # Filter by segment/instrument_type
    if 'instrument_type' in filtered.columns:
        filtered = filtered[filtered['instrument_type'] == segment_prefix]
    elif 'segment' in filtered.columns:
        filtered = filtered[filtered['segment'].str.contains(segment_prefix, na=False)]
        
    return dict(zip(filtered['tradingsymbol'], filtered['instrument_token']))
