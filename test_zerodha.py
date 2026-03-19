import os
from dotenv import load_dotenv

# Load .env early
load_dotenv()

from config.zerodha import load_zerodha_config
from data_ingestion.market_live import make_default_live_client

def main():
    print("Testing config loading...")
    try:
        cfg = load_zerodha_config()
        print(f"Loaded config: api_key={cfg.api_key}, api_secret={'***' if cfg.api_secret else 'None'}, token={cfg.access_token}")
        client = make_default_live_client()
        print("Live client created successfully.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
