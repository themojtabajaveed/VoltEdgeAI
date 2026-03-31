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
import re
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
      3. Walk the redirect chain manually (allow_redirects=False) until
         request_token appears in a Location header — no ConnectionError dependency
      4. Use KiteConnect.generate_session(request_token) → access_token
      5. Persist to env_file if provided

    Returns:
        The new access_token string, or None on failure.
    """
    # Load from env if not provided
    api_key    = api_key    or os.getenv("ZERODHA_API_KEY", "")
    api_secret = api_secret or os.getenv("ZERODHA_API_SECRET", "")
    user_id    = user_id    or os.getenv("ZERODHA_USER_ID", "")
    password   = password   or os.getenv("ZERODHA_PASSWORD", "")
    totp_secret = totp_secret or os.getenv("ZERODHA_TOTP_SECRET", "")

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        missing = [
            name for name, val in [
                ("ZERODHA_API_KEY", api_key),
                ("ZERODHA_API_SECRET", api_secret),
                ("ZERODHA_USER_ID", user_id),
                ("ZERODHA_PASSWORD", password),
                ("ZERODHA_TOTP_SECRET", totp_secret),
            ] if not val
        ]
        logger.error(f"[AutoLogin] Missing credentials: {', '.join(missing)}")
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
            logger.error(f"[AutoLogin] Step 1 failed: {login_data.get('message', login_data)}")
            return None

        request_id = login_data["data"]["request_id"]
        logger.info("[AutoLogin] Step 1 OK: password accepted")
    except Exception as e:
        logger.error(f"[AutoLogin] Step 1 error: {type(e).__name__}: {e}")
        return None

    # ── Step 2: Submit TOTP for 2FA ──────────────────────────────────────
    try:
        totp_code = _generate_totp(totp_secret)

        twofa_resp = session.post(TWOFA_URL, data={
            "user_id":     user_id,
            "request_id":  request_id,
            "twofa_value": totp_code,
            "twofa_type":  "totp",
        })
        twofa_data = twofa_resp.json()

        if twofa_data.get("status") != "success":
            # TOTP window may have just rolled — wait one cycle and retry once
            logger.warning("[AutoLogin] Step 2: TOTP rejected, retrying in 30s...")
            time.sleep(30)
            totp_code = _generate_totp(totp_secret)
            twofa_resp = session.post(TWOFA_URL, data={
                "user_id":     user_id,
                "request_id":  request_id,
                "twofa_value": totp_code,
                "twofa_type":  "totp",
            })
            twofa_data = twofa_resp.json()
            if twofa_data.get("status") != "success":
                logger.error(f"[AutoLogin] Step 2 failed: {twofa_data.get('message', twofa_data)}")
                return None

        logger.info("[AutoLogin] Step 2 OK: 2FA passed")
    except Exception as e:
        logger.error(f"[AutoLogin] Step 2 error: {type(e).__name__}: {e}")
        return None

    # ── Step 3: Walk redirect chain to extract request_token ─────────────
    #
    # After successful 2FA the session cookie is set. Kite responds to
    # GET /connect/login with a chain of 302s terminating at the registered
    # redirect_uri, which carries request_token as a query parameter.
    #
    # We NEVER follow redirects automatically. On each hop we read the
    # Location header; the moment it contains request_token= we extract it
    # and stop — we never need to reach the redirect_uri host itself.
    # This makes the approach redirect_uri-agnostic and removes all
    # dependency on ConnectionError, which is unreliable on GCP.
    request_token = None
    url = f"{CONNECT_URL}?v=3&api_key={api_key}"
    try:
        for hop in range(10):
            resp = session.get(url, allow_redirects=False, timeout=10)
            location = resp.headers.get("Location", "")

            logger.debug(
                f"[AutoLogin] Step 3 hop {hop + 1}: "
                f"status={resp.status_code} "
                f"location={location[:120] if location else '(none)'}"
            )

            if "request_token=" in location:
                match = re.search(r"request_token=([a-zA-Z0-9]+)", location)
                if match:
                    request_token = match.group(1)
                    logger.info(f"[AutoLogin] Step 3 OK: request_token found at hop {hop + 1}")
                else:
                    logger.error(
                        f"[AutoLogin] Step 3: 'request_token=' in Location but "
                        f"regex found nothing — location={location[:200]!r}"
                    )
                break

            if location:
                url = location
                continue

            # Non-redirect response with no token — log for diagnosis
            logger.error(
                f"[AutoLogin] Step 3 stalled at hop {hop + 1}: "
                f"status={resp.status_code} url={url[:120]} "
                f"body_snippet={resp.text[:300]!r}"
            )
            break
        else:
            logger.error(
                "[AutoLogin] Step 3: exhausted 10 redirect hops without "
                "finding request_token — check redirect_uri in Kite developer console"
            )
    except Exception as e:
        logger.error(f"[AutoLogin] Step 3 error: {type(e).__name__}: {e}")

    if not request_token:
        logger.error("[AutoLogin] Step 3 failed — aborting login")
        return None

    # ── Step 4: Generate access_token ────────────────────────────────────
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data.get("access_token")

        if not access_token:
            logger.error("[AutoLogin] Step 4: generate_session returned no access_token")
            return None

        logger.info(f"[AutoLogin] Step 4 OK: access_token generated ({access_token[:8]}...)")

        # Update environment for this process
        os.environ["ZERODHA_ACCESS_TOKEN"] = access_token

        # ── Step 5 (optional): Persist to env file ───────────────────────
        if env_file and os.path.exists(env_file):
            _update_env_file(env_file, access_token)

        return access_token

    except Exception as e:
        logger.error(f"[AutoLogin] Step 4 error: {type(e).__name__}: {e}")
        return None


def _update_env_file(env_path: str, new_token: str) -> None:
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

        logger.info(f"[AutoLogin] Token persisted to {env_path}")
    except Exception as e:
        logger.warning(f"[AutoLogin] Could not update env file: {type(e).__name__}: {e}")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )

    # Try project .env first, then /tmp shadow
    env_path = ".env"
    for candidate in [".env", "/tmp/voltedge.env"]:
        if os.path.exists(candidate):
            env_path = candidate
            load_dotenv(env_path)
            break

    token = auto_refresh_access_token(env_file=env_path)
    if token:
        logger.info(f"Auto-login successful. New token: {token[:8]}... written to {env_path}")
        sys.exit(0)
    else:
        logger.error(
            "Auto-login failed. Check credentials: "
            "ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET"
        )
        sys.exit(1)
