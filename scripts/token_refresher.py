#!/usr/bin/env python3
"""
scripts/token_refresher.py — Daily Kite Access Token Refresher
---------------------------------------------------------------
Designed to run as a cron job at 08:00 IST (02:30 UTC) on weekdays.

Steps:
  1. Load credentials from ~/VoltEdgeAI/.env
  2. Generate TOTP from ZERODHA_TOTP_SECRET via pyotp
  3. Auto-login to Zerodha (HTTP-based, no browser needed)
  4. Extract new access_token, update .env and os.environ
  5. Write cache/token_status.json → {"status": "valid", ...}
  6. Restart voltedge.service via systemctl
  7. On any failure: send URGENT email alert, write "expired" status, exit 1

Logging: ~/VoltEdgeAI/logs/token_refresh.log with IST timestamps
Cron:    30 2 * * 1-5  (Monday–Friday at 08:00 IST = 02:30 UTC)

Usage:
  /home/mujtabasiddiqui/VoltEdgeAI/.venv/bin/python \\
      /home/mujtabasiddiqui/VoltEdgeAI/scripts/token_refresher.py
"""
import logging
import os
import smtplib
import subprocess
import sys
from datetime import datetime
from email.message import EmailMessage

import zoneinfo

# ── Path setup: make src/ and cache/ importable ───────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
LOG_PATH = os.path.join(_PROJECT_ROOT, "logs", "token_refresh.log")
ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")


# ── Logging setup ─────────────────────────────────────────────────────────

class _ISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=IST)
        return dt.strftime("%Y-%m-%d %H:%M:%S IST")


def _setup_logging() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    fmt = _ISTFormatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


logger = logging.getLogger(__name__)


# ── Email alert ───────────────────────────────────────────────────────────

def _send_failure_email(error_detail: str) -> None:
    """Send URGENT email on token refresh failure. Never raises."""
    try:
        smtp_host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("REPORT_SMTP_PORT", "587"))
        smtp_user = os.getenv("REPORT_SMTP_USER")
        smtp_pass = os.getenv("REPORT_SMTP_PASSWORD")
        to_addr   = os.getenv("REPORT_EMAIL_TO")

        if not all([smtp_user, smtp_pass, to_addr]):
            logger.warning("[TokenRefresher] Email credentials missing — cannot send failure alert")
            return

        now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        body = (
            f"VoltEdgeAI token_refresher.py failed at {now_str}.\n\n"
            f"Error detail:\n{error_detail}\n\n"
            f"The Kite access token may be expired. Trading is paused.\n"
            f"To restore manually:\n"
            f"  cd ~/VoltEdgeAI\n"
            f"  .venv/bin/python scripts/token_refresher.py\n\n"
            f"Verify credentials in .env:\n"
            f"  ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET"
        )
        msg = EmailMessage()
        msg["Subject"] = "URGENT: Kite Token Refresh Failed — VoltEdgeAI Paused"
        msg["From"] = smtp_user
        msg["To"] = to_addr
        msg.set_content(body)

        with smtplib.SMTP(smtp_host, smtp_port) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)

        logger.info(f"[TokenRefresher] Failure alert email sent to {to_addr}")
    except Exception as e:
        logger.error(f"[TokenRefresher] Could not send failure email: {e}")


# ── Service restart ───────────────────────────────────────────────────────

def _restart_service() -> bool:
    """Restart voltedge.service via systemctl. Returns True if successful."""
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "voltedge.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("[TokenRefresher] voltedge.service restarted successfully")
            return True
        else:
            logger.error(
                f"[TokenRefresher] systemctl restart failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
            return False
    except subprocess.TimeoutExpired:
        logger.error("[TokenRefresher] systemctl restart timed out after 30s")
        return False
    except Exception as e:
        logger.error(f"[TokenRefresher] Service restart error: {type(e).__name__}: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> int:
    """
    Returns 0 on success, 1 on failure.
    """
    _setup_logging()
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    logger.info(f"[TokenRefresher] ===== Starting token refresh at {now_str} =====")

    # Load .env
    try:
        from dotenv import load_dotenv
        if os.path.exists(ENV_PATH):
            load_dotenv(ENV_PATH)
            logger.info(f"[TokenRefresher] Loaded .env from {ENV_PATH}")
        else:
            logger.error(f"[TokenRefresher] .env not found at {ENV_PATH}")
            _send_failure_email(f".env file not found at {ENV_PATH}")
            return 1
    except ImportError:
        logger.error("[TokenRefresher] python-dotenv not installed")
        _send_failure_email("python-dotenv not installed in venv")
        return 1

    # Run auto-login
    try:
        from src.tools.auto_login import auto_refresh_access_token
    except ImportError as e:
        logger.error(f"[TokenRefresher] Cannot import auto_login: {e}")
        _send_failure_email(f"Import error: {e}")
        return 1

    logger.info("[TokenRefresher] Calling auto_refresh_access_token()...")
    new_token = auto_refresh_access_token(env_file=ENV_PATH)

    if not new_token:
        error_msg = (
            "auto_refresh_access_token() returned None. "
            "Check ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET in .env"
        )
        logger.error(f"[TokenRefresher] {error_msg}")
        # Write expired status
        try:
            from cache.data_cache import write_token_status
            write_token_status("expired")
        except Exception as cache_e:
            logger.warning(f"[TokenRefresher] Could not write token status: {cache_e}")
        _send_failure_email(error_msg)
        return 1

    logger.info(f"[TokenRefresher] New token obtained: {new_token[:8]}...")

    # Write valid status to cache/token_status.json
    try:
        from cache.data_cache import write_token_status
        write_token_status("valid")
        logger.info("[TokenRefresher] token_status.json → valid")
    except Exception as e:
        logger.warning(f"[TokenRefresher] Could not write token_status.json: {e}")

    # Restart voltedge.service
    restart_ok = _restart_service()
    if not restart_ok:
        logger.warning(
            "[TokenRefresher] Service restart failed — new token is in .env, "
            "but service may still hold the old token in memory."
        )

    end_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    logger.info(f"[TokenRefresher] ===== Token refresh complete at {end_str} =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
