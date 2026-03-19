import os
import sys
from dotenv import load_dotenv

# Load .env early so config picks it up
load_dotenv()

from kiteconnect import KiteConnect
from src.config.zerodha import load_zerodha_config

def main() -> int:
    cfg = load_zerodha_config()
    kite = KiteConnect(api_key=cfg.api_key)

    # 1) Print login URL
    login_url = kite.login_url()
    print("Open this URL in your browser, log in, and authorize the app:")
    print(login_url)
    print()

    # 2) Ask for request_token
    request_token = input("Paste the `request_token` from the callback URL here: ").strip()
    if not request_token:
        print("No request_token provided, aborting.")
        return 1

    # 3) Exchange for access_token
    try:
        data = kite.generate_session(request_token, api_secret=cfg.api_secret)
    except Exception as e:
        print(f"Error generating access token: {e}")
        return 1

    access_token = data.get("access_token")
    if not access_token:
        print("No access_token returned, something went wrong.")
        return 1

    print()
    print("Your ACCESS TOKEN is:")
    print(access_token)
    print()
    print("Add this line to your .env file:")
    print(f"ZERODHA_ACCESS_TOKEN={access_token}")
    print()
    print("Then restart your app so the new token is loaded.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
