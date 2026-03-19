from typing import List, Dict
import threading
from datetime import datetime, timedelta
from data_ingestion.market_live import Bar

class IntradayStore:
    def __init__(self):
        self._bars: Dict[str, List[Bar]] = {}
        self._lock = threading.Lock()

    def add_bar(self, bar: Bar):
        with self._lock:
            if bar.symbol not in self._bars:
                self._bars[bar.symbol] = []
            
            # To ensure chronological order and replace duplicates if same start time
            if self._bars[bar.symbol] and self._bars[bar.symbol][-1].start == bar.start:
                self._bars[bar.symbol][-1] = bar
            else:
                self._bars[bar.symbol].append(bar)

    def get_bars(self, symbol: str, lookback_minutes: int = 60) -> List[Bar]:
        with self._lock:
            bars = self._bars.get(symbol, [])
            if not bars:
                return []
            
            # Simple fallback to naive datetime if tz bounds conflict
            try:
                cutoff = datetime.now(bars[-1].start.tzinfo) - timedelta(minutes=lookback_minutes)
            except AttributeError:
                cutoff = datetime.now() - timedelta(minutes=lookback_minutes)
                
            return [b for b in bars if b.start >= cutoff]

# Global singleton store instance
_store = IntradayStore()

def add_intraday_bar_to_store(bar: Bar):
    """Push a completed bar into the live intraday store."""
    _store.add_bar(bar)

def get_intraday_bars_for_symbol(symbol: str, lookback_minutes: int = 60) -> list[Bar]:
    """
    Return last N minutes of bars for this symbol from an in-memory store
    that KiteLiveClient + BarBuilder are updating.
    """
    return _store.get_bars(symbol, lookback_minutes)
