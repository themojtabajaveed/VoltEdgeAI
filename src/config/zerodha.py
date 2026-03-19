import os
from dataclasses import dataclass

@dataclass
class ZerodhaConfig:
    api_key: str
    api_secret: str
    access_token: str | None = None

def load_zerodha_config() -> ZerodhaConfig:
    """
    Load Zerodha credentials from environment variables:
    - ZERODHA_API_KEY
    - ZERODHA_API_SECRET
    - ZERODHA_ACCESS_TOKEN (optional, can be empty initially)
    """
    api_key = os.getenv("ZERODHA_API_KEY")
    api_secret = os.getenv("ZERODHA_API_SECRET")
    access_token = os.getenv("ZERODHA_ACCESS_TOKEN")

    if not api_key:
        raise ValueError("Missing required environment variable: ZERODHA_API_KEY")
    
    if not api_secret:
        raise ValueError("Missing required environment variable: ZERODHA_API_SECRET")

    # Treat empty strings as None
    if access_token == "":
        access_token = None

    return ZerodhaConfig(
        api_key=api_key,
        api_secret=api_secret,
        access_token=access_token
    )
