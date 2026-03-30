"""
exit_monitor.py
---------------
Dedicated 1-second ExitEngine monitor thread.

WHY A SEPARATE THREAD:
  The main runner.py loop runs every 60 seconds. If the loop is busy
  (LLM calls, scraping, TA computation) for 45 seconds of that minute,
  the ExitEngine would fire only once every ~2 minutes — long enough for
  a stop-loss price to be breached and recovered without triggering an exit.

  This thread decouples position protection from strategy scanning,
  evaluating exits every second independent of main loop activity.

THREAD SAFETY:
  - ExitEngine.tick() reads positions via positions.get_open_positions()
    which acquires PositionBook's RLock (P0 fix).
  - Exit signals are placed on a thread-safe queue.SimpleQueue.
  - The main runner.py drains exit_signal_queue each cycle and executes
    orders — orders are NOT placed from this thread to avoid concurrent
    broker API calls.

SEPARATION OF CONCERNS:
  Detection  → ExitMonitorThread (this file)
  Execution  → main runner.py draining exit_signal_queue
"""
import logging
import queue
import threading
import time
from datetime import datetime
from typing import List

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except ImportError:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

from src.trading.exit_engine import ExitEngine, ExitSignal

logger = logging.getLogger(__name__)


class ExitMonitorThread:
    """
    Wraps ExitEngine in a dedicated 1-second daemon thread.

    Usage in runner.py:
        exit_monitor = ExitMonitorThread(exit_engine)
        exit_monitor.start()

        # In the main loop (every 60s cycle):
        exit_signals = exit_monitor.drain_signals()
        for es in exit_signals:
            ... execute orders ...

        # On shutdown:
        exit_monitor.stop()
    """

    def __init__(
        self,
        exit_engine: ExitEngine,
        interval_seconds: float = 1.0,
    ) -> None:
        self._engine = exit_engine
        self._interval = interval_seconds
        self._signal_queue: queue.SimpleQueue[ExitSignal] = queue.SimpleQueue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the monitor thread. Call once after runner initialization."""
        if self._thread and self._thread.is_alive():
            logger.warning("[ExitMonitor] already running — ignoring start()")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="ExitMonitorThread",
            daemon=True,  # dies with main process — no cleanup needed
        )
        self._thread.start()
        logger.info(f"[ExitMonitor] started — checking exits every {self._interval}s")

    def stop(self) -> None:
        """Signal the thread to stop. Blocks until it exits (max 3s)."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            logger.info("[ExitMonitor] stopped")

    def drain_signals(self) -> List[ExitSignal]:
        """
        Non-blocking drain of all queued exit signals.
        Call this from runner.py each cycle to execute the detected exits.
        """
        signals: List[ExitSignal] = []
        try:
            while True:
                signals.append(self._signal_queue.get_nowait())
        except queue.Empty:
            pass
        return signals

    def _run(self) -> None:
        """Main loop — runs until stop() is called."""
        while not self._stop_event.is_set():
            loop_start = time.monotonic()
            try:
                now = datetime.now(IST)
                signals = self._engine.tick(now=now)
                for sig in signals:
                    self._signal_queue.put_nowait(sig)
                    logger.info(
                        f"[ExitMonitor] ⚡ queued exit: {sig.symbol} {sig.side} "
                        f"reason={sig.reason} ltp={sig.ltp:.2f}"
                    )
            except Exception as exc:
                logger.error(f"[ExitMonitor] tick error: {exc}")

            # Precise interval sleep — subtract time spent in tick()
            elapsed = time.monotonic() - loop_start
            sleep_for = max(0.0, self._interval - elapsed)
            time.sleep(sleep_for)
