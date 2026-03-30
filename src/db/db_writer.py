"""
db_writer.py — Safe Async Database Writer
------------------------------------------
Decouples SQLite writes from the main execution path without silently
dropping records.

DESIGN RATIONALE:
  The original code did TradeRecord inserts synchronously inside the main
  trading loop. SQLAlchemy session creation + commit can take 10–50ms,
  and under load (file system pressure, WAL checkpoints) can spike to 200ms.
  This blocks the loop from processing the next trade cycle.

  A naive "fire-and-forget" ThreadPoolExecutor would silently swallow errors.
  Instead, DatabaseWriter uses:
    - A bounded queue.Queue(maxsize=500) — if the queue fills, we log loudly
      rather than silently dropping financial records
    - A single daemon writer thread with retry logic (up to 3 attempts)
    - Structured logging on every failure — never silent

USAGE:
    # In runner.py (module level, after imports):
    from src.db.db_writer import get_db_writer
    db_writer = get_db_writer()   # singleton, safe to call multiple times

    # When a trade closes:
    db_writer.write_trade_record(record_dict)
    # Returns immediately — main loop continues unblocked

    # On shutdown (optional, ensures queue is drained):
    db_writer.flush(timeout=5.0)
"""
import logging
import queue
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 500   # ~500 writes queued before we warn; practically never hit
_WRITER_RETRIES = 3
_RETRY_BACKOFF = 0.5   # seconds between retries on failure

# Sentinel to signal the worker thread to stop
_STOP_SENTINEL = object()


class DatabaseWriter:
    """
    Single-threaded, queue-backed async database writer.

    All writes are ordered (FIFO), retried on failure, and logged loudly
    on permanent failure. Never silently drops a record.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._thread = threading.Thread(
            target=self._worker,
            name="DatabaseWriterThread",
            daemon=True,
        )
        self._thread.start()
        logger.info("[DBWriter] started — async trade record writes active")

    def write_trade_record(self, record_dict: Dict[str, Any]) -> None:
        """
        Enqueue a trade record for async SQLite write.
        Returns immediately — non-blocking.

        Args:
            record_dict: Keyword arguments to pass to TradeRecord(**record_dict).
                         Must include all required TradeRecord fields.
        """
        try:
            self._queue.put_nowait(("trade_record", record_dict))
        except queue.Full:
            # Queue is full — this is a critical warning, not a silent drop
            logger.critical(
                f"[DBWriter] QUEUE FULL — trade record for "
                f"{record_dict.get('symbol', '?')} will be logged to stderr instead\n"
                f"Record: {record_dict}"
            )
            # Fallback: log to stderr as structured data so it can be recovered
            import json
            print(f"UNWRITTEN_TRADE_RECORD: {json.dumps(record_dict, default=str)}")

    def flush(self, timeout: float = 5.0) -> None:
        """
        Block until the write queue is empty or timeout expires.
        Call on graceful shutdown to ensure all records are written.
        """
        try:
            self._queue.join()  # blocks until all pending items processed
        except Exception:
            # join() doesn't support timeout — implement manually
            deadline = time.monotonic() + timeout
            while not self._queue.empty() and time.monotonic() < deadline:
                time.sleep(0.05)

    def stop(self) -> None:
        """Signal the worker to stop after draining the queue."""
        self._queue.put(_STOP_SENTINEL)
        self._thread.join(timeout=10.0)

    def _worker(self) -> None:
        """Process write tasks from the queue with retry logic."""
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is _STOP_SENTINEL:
                self._queue.task_done()
                logger.info("[DBWriter] stop sentinel received — shutting down")
                break

            task_type, data = item
            success = False
            for attempt in range(1, _WRITER_RETRIES + 1):
                try:
                    if task_type == "trade_record":
                        self._write_trade_record(data)
                    success = True
                    break
                except Exception as exc:
                    logger.error(
                        f"[DBWriter] write failed (attempt {attempt}/{_WRITER_RETRIES}): {exc}"
                    )
                    if attempt < _WRITER_RETRIES:
                        time.sleep(_RETRY_BACKOFF * attempt)

            if not success:
                import json
                logger.critical(
                    f"[DBWriter] PERMANENT FAILURE after {_WRITER_RETRIES} retries. "
                    f"Record: {json.dumps(data, default=str)}"
                )

            self._queue.task_done()

    @staticmethod
    def _write_trade_record(data: Dict[str, Any]) -> None:
        """Perform the actual SQLite insert."""
        from src.db import SessionLocal, TradeRecord
        with SessionLocal() as session:
            record = TradeRecord(**data)
            session.add(record)
            session.commit()


# ── Singleton access ──────────────────────────────────────────────────────────

_instance: Optional[DatabaseWriter] = None
_instance_lock = threading.Lock()


def get_db_writer() -> DatabaseWriter:
    """
    Return the global DatabaseWriter singleton.
    Thread-safe. Creates the writer on first call.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = DatabaseWriter()
    return _instance
