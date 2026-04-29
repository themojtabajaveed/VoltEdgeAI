"""Atomic JSON artifact writers.

Mirrors the pattern used elsewhere in VoltEdge for daily-rotated files:
write to a temp file, then `os.replace()` for an atomic rename. Avoids
half-written JSON if the process is killed mid-write.

Output files use the IST date in their filename. Callers append records
to a list-of-records artifact via `append_record()`, which is read-modify-
write — fine for the volumes we expect (50–500 events/day) and avoids a
separate database for v1.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_FILE_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    """Return a per-path lock so concurrent writers to the same artifact
    serialize, but writes to different artifacts proceed in parallel."""
    with _LOCKS_LOCK:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = threading.Lock()
        return _FILE_LOCKS[path]


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def daily_path(prefix: str, data_dir: str = "data", date_str: Optional[str] = None) -> str:
    """Return `data/{prefix}_YYYY-MM-DD.json` using IST date by default."""
    if date_str is None:
        date_str = datetime.now(IST).strftime("%Y-%m-%d")
    return os.path.join(data_dir, f"{prefix}_{date_str}.json")


def atomic_write_json(path: str, payload: Any) -> None:
    """Write `payload` to `path` atomically (temp + os.replace)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    serializable = _to_jsonable(payload)
    with _lock_for(path):
        with open(tmp, "w") as f:
            json.dump(serializable, f, indent=2, default=str)
        os.replace(tmp, path)


def read_records(path: str) -> List[Dict[str, Any]]:
    """Read a list-of-records artifact. Returns [] if absent or malformed."""
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            return data["records"]
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[Writers] Failed to read %s: %s", path, e)
        return []


def append_record(prefix: str, record: Any, data_dir: str = "data") -> str:
    """Append `record` to `data/{prefix}_YYYY-MM-DD.json`. Returns path."""
    path = daily_path(prefix, data_dir)
    with _lock_for(path):
        existing = read_records(path)
        existing.append(_to_jsonable(record))
        # Atomic write under the same lock.
        os.makedirs(data_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(existing, f, indent=2, default=str)
        os.replace(tmp, path)
    return path


def write_heartbeat(data_dir: str = "data") -> None:
    """Write a freshness sentinel at `data/event_intel_heartbeat.json`.
    External monitors detect process stalls when this stops updating.
    """
    path = os.path.join(data_dir, "event_intel_heartbeat.json")
    payload = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "ts_ist": datetime.now(IST).isoformat(),
    }
    atomic_write_json(path, payload)
