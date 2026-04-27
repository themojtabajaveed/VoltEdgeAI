"""
cache/data_cache.py — Shared File-Based Data Cache
---------------------------------------------------
Thread-safe JSON caches for LTP and candle data.
Used by VoltEdgeAI internally and readable by external services (e.g. TimesFM dry-run).

All cache files are written with 644 permissions (world-readable).
File locking uses fcntl (Linux-native, no extra deps).

Token status (valid/expired) is also managed here so both the standalone
token_refresher.py and the running service can read/write it atomically.
"""
import fcntl
import json
import logging
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import List, Optional
import zoneinfo

logger = logging.getLogger(__name__)

_CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
LTP_CACHE_PATH = os.path.join(_CACHE_DIR, "ltp_cache.json")
CANDLES_CACHE_PATH = os.path.join(_CACHE_DIR, "candles_cache.json")
TOKEN_STATUS_PATH = os.path.join(_CACHE_DIR, "token_status.json")

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def _ist_now() -> str:
    return datetime.now(IST).isoformat()


def _read_json_locked(path: str) -> dict:
    """Read a JSON file with shared lock. Returns {} on any failure."""
    try:
        with open(path, "r") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                data = json.load(fh)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[Cache] Read failed for {path}: {e}")
        return {}


def _write_json_locked(path: str, data: dict) -> None:
    """Write JSON atomically with exclusive lock. chmod 644 after write."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                json.dump(data, fh, indent=2)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    except Exception as e:
        logger.warning(f"[Cache] Write failed for {path}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


# ── CacheManager ─────────────────────────────────────────────────────────

class CacheManager:
    """
    Thread-safe file-backed cache for LTP and candle data.

    Cache file format:
      ltp_cache.json:
        {"RELIANCE": {"price": 2345.5, "timestamp": "2026-04-03T09:15:00+05:30"}, ...}
      candles_cache.json:
        {"RELIANCE:1d": {"candles": [...], "timestamp": "..."}, ...}
    """

    def write_ltp(self, symbol: str, price: float, timestamp: Optional[str] = None) -> None:
        """Write last traded price for a symbol to ltp_cache.json."""
        data = _read_json_locked(LTP_CACHE_PATH)
        data[symbol] = {
            "price": price,
            "timestamp": timestamp or _ist_now(),
        }
        _write_json_locked(LTP_CACHE_PATH, data)
        logger.debug(f"[Cache] LTP write: {symbol}={price}")

    def write_candles(
        self,
        symbol: str,
        interval: str,
        candles_list: List[dict],
        timestamp: Optional[str] = None,
    ) -> None:
        """Write candle list for a symbol+interval to candles_cache.json."""
        data = _read_json_locked(CANDLES_CACHE_PATH)
        key = f"{symbol}:{interval}"
        data[key] = {
            "candles": candles_list,
            "timestamp": timestamp or _ist_now(),
        }
        _write_json_locked(CANDLES_CACHE_PATH, data)
        logger.debug(f"[Cache] Candles write: {key} ({len(candles_list)} bars)")

    def read_ltp(self, symbol: str, max_age_seconds: int = 60) -> Optional[float]:
        """Return cached price if fresh (within max_age_seconds), else None."""
        data = _read_json_locked(LTP_CACHE_PATH)
        entry = data.get(symbol)
        if not entry:
            logger.debug(f"[Cache] LTP miss: {symbol}")
            return None
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            age = (datetime.now(IST) - ts).total_seconds()
            if age <= max_age_seconds:
                logger.debug(f"[Cache] LTP hit: {symbol}={entry['price']} (age={age:.0f}s)")
                return float(entry["price"])
            logger.debug(f"[Cache] LTP stale: {symbol} (age={age:.0f}s > {max_age_seconds}s)")
        except Exception as e:
            logger.warning(f"[Cache] LTP read error for {symbol}: {e}")
        return None

    def read_candles(
        self,
        symbol: str,
        interval: str,
        max_age_seconds: int = 300,
    ) -> Optional[List[dict]]:
        """Return cached candles if fresh, else None."""
        data = _read_json_locked(CANDLES_CACHE_PATH)
        key = f"{symbol}:{interval}"
        entry = data.get(key)
        if not entry:
            logger.debug(f"[Cache] Candles miss: {key}")
            return None
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            age = (datetime.now(IST) - ts).total_seconds()
            if age <= max_age_seconds:
                logger.debug(
                    f"[Cache] Candles hit: {key} ({len(entry['candles'])} bars, age={age:.0f}s)"
                )
                return entry["candles"]
            logger.debug(f"[Cache] Candles stale: {key} (age={age:.0f}s > {max_age_seconds}s)")
        except Exception as e:
            logger.warning(f"[Cache] Candles read error for {key}: {e}")
        return None


# ── Token Status Management ───────────────────────────────────────────────

def write_token_status(status: str) -> None:
    """Write token status ('valid'/'expired') to cache/token_status.json."""
    _write_json_locked(TOKEN_STATUS_PATH, {
        "status": status,
        "timestamp": _ist_now(),
    })
    logger.info(f"[Cache] Token status set to: {status}")


def read_token_status() -> str:
    """Return current token status: 'valid', 'expired', or 'unknown'."""
    data = _read_json_locked(TOKEN_STATUS_PATH)
    return data.get("status", "unknown")


def handle_token_expiry(context: str = "") -> None:
    """
    Call when a Kite TokenException is caught anywhere in the codebase.
    - Writes 'expired' to token_status.json
    - Sends URGENT email alert
    - Logs the event
    Never raises — safe to call inside any except block.
    """
    msg = f"Kite token expired/invalid — context: {context}" if context else "Kite token expired/invalid."
    logger.error(f"[TokenGuard] {msg}")
    write_token_status("expired")

    try:
        smtp_host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("REPORT_SMTP_PORT", "587"))
        smtp_user = os.getenv("REPORT_SMTP_USER")
        smtp_pass = os.getenv("REPORT_SMTP_PASSWORD")
        to_addr   = os.getenv("REPORT_EMAIL_TO")

        if not all([smtp_user, smtp_pass, to_addr]):
            logger.warning("[TokenGuard] Email credentials not configured — skipping alert email")
            return

        now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        body = (
            f"VoltEdgeAI detected an expired Kite access token at {now_str}.\n\n"
            f"Context: {context}\n\n"
            f"All trading operations have been paused gracefully.\n"
            f"The token_refresher.py cron job will run at 08:00 IST on the next weekday "
            f"to restore the token automatically.\n\n"
            f"To restore manually:\n"
            f"  cd ~/VoltEdgeAI && .venv/bin/python scripts/token_refresher.py"
        )

        email_msg = EmailMessage()
        email_msg["Subject"] = "URGENT: Kite Token Expired — VoltEdgeAI Paused"
        email_msg["From"] = smtp_user
        email_msg["To"] = to_addr
        email_msg.set_content(body)

        with smtplib.SMTP(smtp_host, smtp_port) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(email_msg)

        logger.info(f"[TokenGuard] Token expiry alert email sent to {to_addr}")
    except Exception as e:
        logger.warning(f"[TokenGuard] Could not send token expiry email: {e}")
