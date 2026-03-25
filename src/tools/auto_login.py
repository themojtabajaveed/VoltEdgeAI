"""
auto_login.py
-------------
Fully automated Zerodha KiteConnect login using pyotp for TOTP generation.

This eliminates the need to manually open a browser, login, and paste the
request_token every morning. It runs headlessly via HTTP requests.

Requirements:
  pip install kiteconnect pyotp requests

Environment Variables:
  ZERODHA_API_KEY       - Your Kite Connect API Key
  ZERODHA_API_SECRET    - Your Kite Connect API Secret
  ZERODHA_USER_ID       - Your Zerodha Client ID (e.g. AB1234)
  ZERODHA_PASSWORD      - Your Zerodha login password
  ZERODHA_TOTP_SECRET   - Your TOTP secret key (from Kite 2FA settings)

Usage:
  # Standalone:
  PYTHONPATH=. python src/tools/auto_login.py

  # As a module (called by runner.py at startup):
  from src.tools.auto_login import auto_refresh_access_token
  token = auto_refresh_access_token()
"""
import os
import logging
import time
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger(__name__)

# Zerodha login endpoints
LOGIN_URL = "https://kite.zerodha.com/api/login"
TWOFA_URL = "https://kite.zerodha.com/api/twofa"
CONNECT_URL = "https://kite.trade/connect/login"


def _generate_totp(secret: str) -> str:
    """Generate current TOTP using pyotp."""
    try:
        import pyotp
        totp = pyotp.TOTP(secret)
        return totp.now()
    except ImportError:
        raise RuntimeError(
            "pyotp is not installed. Run: pip install pyotp\n"
            "Then set ZERODHA_TOTP_SECRET in your .env file."
        )


def auto_refresh_access_token(
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    user_id: Optional[str] = None,
    password: Optional[str] = None,
    totp_secret: Optional[str] = None,
    env_file: Optional[str] = None,
) -> Optional[str]:
    """
    Automatically login to Zerodha and return a fresh access_token.

    Steps:
      1. POST user_id + password to /api/login → get request_id
      2. POST request_id + TOTP to /api/twofa → get redirect with request_token
      3. Use KiteConnect.generate_session(request_token) → access_token
      4. Save to env file (if provided)

    Returns:
        The new access_token string, or None on failure.
    """
    # Load from env if not provided
    api_key = api_key or os.getenv("ZERODHA_API_KEY", "")
    api_secret = api_secret or os.getenv("ZERODHA_API_SECRET", "")
    user_id = user_id or os.getenv("ZERODHA_USER_ID", "")
    password = password or os.getenv("ZERODHA_PASSWORD", "")
    totp_secret = totp_secret or os.getenv("ZERODHA_TOTP_SECRET", "")

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        missing = []
        if not api_key: missing.append("ZERODHA_API_KEY")
        if not api_secret: missing.append("ZERODHA_API_SECRET")
        if not user_id: missing.append("ZERODHA_USER_ID")
        if not password: missing.append("ZERODHA_PASSWORD")
        if not totp_secret: missing.append("ZERODHA_TOTP_SECRET")
        logger.error(f"Auto-login missing credentials: {', '.join(missing)}")
        print(f"❌ Auto-login missing: {', '.join(missing)}")
        print("   Set these in your .env file for fully automated login.")
        return None

    session = requests.Session()

    # ── Step 1: Login with user_id and password ──────────────────────────
    try:
        login_resp = session.post(LOGIN_URL, data={
            "user_id": user_id,
            "password": password,
        })
        login_data = login_resp.json()

        if login_data.get("status") != "success":
            logger.error(f"Auto-login Step 1 failed: {login_data}")
            print(f"❌ Login failed: {login_data.get('message', 'Unknown error')}")
            return None

        request_id = login_data["data"]["request_id"]
        logger.info(f"Auto-login Step 1 OK: got request_id")
    except Exception as e:
        logger.error(f"Auto-login Step 1 error: {e}")
        print(f"❌ Login request failed: {e}")
        return None

    # ── Step 2: Submit TOTP for 2FA ──────────────────────────────────────
    try:
        totp_code = _generate_totp(totp_secret)

        twofa_resp = session.post(TWOFA_URL, data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp",
        })
        twofa_data = twofa_resp.json()

        if twofa_data.get("status") != "success":
            # TOTP might have just expired — wait and retry once
            logger.warning("TOTP may have expired, retrying in 30s...")
            time.sleep(30)
            totp_code = _generate_totp(totp_secret)
            twofa_resp = session.post(TWOFA_URL, data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": "totp",
            })
            twofa_data = twofa_resp.json()
            if twofa_data.get("status") != "success":
                logger.error(f"Auto-login Step 2 failed: {twofa_data}")
                print(f"❌ 2FA failed: {twofa_data.get('message', 'Unknown')}")
                return None

        logger.info("Auto-login Step 2 OK: 2FA passed")
    except Exception as e:
        logger.error(f"Auto-login Step 2 error: {e}")
        print(f"❌ 2FA request failed: {e}")
        return None

    # ── Step 3: Get request_token from redirect ──────────────────────────
    request_token = None
    try:
        # Kite redirects: kite.trade → kite.zerodha.com → <redirect_uri>?request_token=...
        # The redirect_uri is typically 127.0.0.1 or localhost, which won't be running.
        # Strategy: follow redirects and catch the ConnectionError when it hits localhost.
        # The request_token is embedded in THAT final URL.
        try:
            redirect_resp = session.get(
                f"{CONNECT_URL}?v=3&api_key={api_key}",
                allow_redirects=True,
                timeout=10,
            )
            # If we somehow get a successful response, parse its URL
            final_url = redirect_resp.url
            parsed = urlparse(final_url)
            params = parse_qs(parsed.query)
            request_token = params.get("request_token", [None])[0]
        except requests.exceptions.ConnectionError as ce:
            # Expected! The final redirect to 127.0.0.1 will fail.
            # Extract the request_token from the error's URL.
            error_str = str(ce)
            if "request_token=" in error_str:
                import re
                match = re.search(r'request_token=([a-zA-Z0-9]+)', error_str)
                if match:
                    request_token = match.group(1)
                    logger.info(f"Auto-login Step 3 OK: extracted request_token from redirect error")

        if not request_token:
            # Fallback: try with allow_redirects=False and check Location header chain
            r = session.get(f"{CONNECT_URL}?v=3&api_key={api_key}", allow_redirects=False, timeout=10)
            loc = r.headers.get("Location", "")
            if "request_token=" in loc:
                parsed = urlparse(loc)
                params = parse_qs(parsed.query)
                request_token = params.get("request_token", [None])[0]

        if not request_token:
            logger.error("Auto-login Step 3: Could not extract request_token from any redirect")
            print("❌ No request_token found in redirect chain")
            return None

        logger.info(f"Auto-login Step 3 OK: got request_token")
    except Exception as e:
        logger.error(f"Auto-login Step 3 error: {e}")
        print(f"❌ Redirect/token extraction failed: {e}")
        return None

    # ── Step 4: Generate access_token ────────────────────────────────────
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data.get("access_token")

        if not access_token:
            logger.error("Auto-login Step 4: No access_token in session data")
            print("❌ No access_token returned from generate_session")
            return None

        logger.info("Auto-login Step 4 OK: access_token generated")
        print(f"✅ Auto-login successful! Token: {access_token[:8]}...")

        # Update environment for this process
        os.environ["ZERODHA_ACCESS_TOKEN"] = access_token

        # ── Step 5 (optional): Write back to env file ────────────────
        if env_file and os.path.exists(env_file):
            _update_env_file(env_file, access_token)

        return access_token

    except Exception as e:
        logger.error(f"Auto-login Step 4 error: {e}")
        print(f"❌ generate_session failed: {e}")
        return None


def _update_env_file(env_path: str, new_token: str):
    """Update ZERODHA_ACCESS_TOKEN in the .env file."""
    try:
        lines = []
        found = False
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith("ZERODHA_ACCESS_TOKEN="):
                    lines.append(f"ZERODHA_ACCESS_TOKEN={new_token}\n")
                    found = True
                else:
                    lines.append(line)

        if not found:
            lines.append(f"\nZERODHA_ACCESS_TOKEN={new_token}\n")

        with open(env_path, "w") as f:
            f.writelines(lines)

        logger.info(f"Updated access_token in {env_path}")
    except Exception as e:
        logger.warning(f"Could not update env file: {e}")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    # Try project .env first, then /tmp shadow
    for env_path in [".env", "/tmp/voltedge.env"]:
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break

    token = auto_refresh_access_token(env_file=env_path)
    if token:
        print(f"\nNew ZERODHA_ACCESS_TOKEN={token}")
        print("Token has been saved to your .env file.")
        sys.exit(0)
    else:
        print("\n⚠️ Auto-login failed. Please check your credentials.")
        print("Required env vars: ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET")
        sys.exit(1)
