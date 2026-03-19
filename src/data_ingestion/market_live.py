from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, List
import threading
import logging
import os

try:
    from kiteconnect import KiteConnect, KiteTicker
except ImportError:
    KiteConnect, KiteTicker = None, None

from src.config.zerodha import load_zerodha_config, ZerodhaConfig

@dataclass
class Tick:
    symbol: str          # e.g. "RELIANCE"
    instrument_token: int
    ltp: float
    volume: int
    timestamp: datetime
    oi: Optional[int] = None      # for F&O
    bid: Optional[float] = None
    ask: Optional[float] = None

@dataclass
class Snapshot:
    symbol: str
    instrument_token: int
    ltp: float
    ohlc: Dict[str, float]        # {"open":..., "high":..., "low":..., "close":...}
    volume: int
    oi: Optional[int] = None
    timestamp: datetime = None

class KiteLiveClient:
    def __init__(self, config: ZerodhaConfig | None = None, symbol_to_token: Dict[str, int] | None = None):
        self.logger = logging.getLogger(__name__)

        if config is None:
            config = load_zerodha_config()
        self.config = config

        if not KiteConnect:
            self.logger.warning("kiteconnect missing; operating in dummy mode.")
            self._kite = None
            self._ticker = None
        else:
            self._kite = KiteConnect(api_key=self.config.api_key)
            # Access token will be set later once I generate it via login flow
            self._ticker: KiteTicker | None = None
            if self.config.access_token:
                self._kite.set_access_token(self.config.access_token)
                self._ticker = KiteTicker(self.config.api_key, self.config.access_token)
                self._setup_callbacks()

        self._last_ticks: Dict[str, Tick] = {}
        self._lock = threading.Lock()

        # We need a way to map instrument_token to symbol
        self._token_to_symbol: Dict[int, str] = {}
        self._symbol_to_token: Dict[str, int] = {}
        
        if symbol_to_token:
            self.set_instrument_mapping(symbol_to_token)

    def _setup_callbacks(self):
        if not self._ticker: return
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_noreconnect = self._on_noreconnect
        self._ticker.on_reconnect = self._on_reconnect

    def _on_ticks(self, ws, ticks):
        with self._lock:
            for t in ticks:
                token = t['instrument_token']
                symbol = self._token_to_symbol.get(token, str(token))
                
                # Parse timestamp safely
                ts = t.get('timestamp')
                if not ts:
                    ts = datetime.now()
                elif isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts)
                    except ValueError:
                        ts = datetime.now()
                
                tick_obj = Tick(
                    symbol=symbol,
                    instrument_token=token,
                    ltp=t.get('last_price', 0.0),
                    volume=t.get('volume_traded', 0),
                    timestamp=ts,
                    oi=t.get('oi', None),
                    bid=t['depth']['buy'][0]['price'] if 'depth' in t and t['depth']['buy'] else None,
                    ask=t['depth']['sell'][0]['price'] if 'depth' in t and t['depth']['sell'] else None
                )
                self._last_ticks[symbol] = tick_obj

    def _on_connect(self, ws, response):
        self.logger.info("Kite WebSocket connected.")
        # We implicitly re-subscribe all mapped tokens seamlessly
        if self._symbol_to_token and self._ticker:
            tokens = list(self._symbol_to_token.values())
            self._ticker.subscribe(tokens)
            self._ticker.set_mode(self._ticker.MODE_FULL, tokens)

    def _on_close(self, ws, code, reason):
        self.logger.warning(f"Kite WebSocket closed: {code} - {reason}")

    def _on_error(self, ws, code, reason):
        self.logger.error(f"Kite WebSocket error: {code} - {reason}")
        
    def _on_reconnect(self, ws, attempts_count):
        self.logger.info(f"Kite WebSocket reconnecting... attempt {attempts_count}")
        
    def _on_noreconnect(self, ws):
        self.logger.error("Kite WebSocket max reconnects reached.")

    def set_instrument_mapping(self, token_map: Dict[str, int]):
        """Inject known mapping of symbol -> instrument_token dynamically"""
        with self._lock:
            self._symbol_to_token.update(token_map)
            for sym, tok in token_map.items():
                self._token_to_symbol[tok] = sym

    def start_websocket(self) -> None:
        """Start the WebSocket in a background thread or async loop."""
        if not self._ticker:
            self.logger.error("Cannot start WebSocket. Kite credentials missing or no access token.")
            return
            
        self.logger.info("Starting Kite WebSocket stream in background...")
        self._ticker.connect(threaded=True)

    def stop_websocket(self) -> None:
        """Stop the WebSocket cleanly."""
        if self._ticker:
           self._ticker.close()
           self.logger.info("Kite WebSocket stopped.")

    def subscribe_symbols(self, symbols: List[str], mode: str = "full") -> None:
        """Subscribe to given NSE cash symbols in a given mode: 'ltp' | 'quote' | 'full'."""
        if not self._ticker: return
        
        tokens = []
        for sym in symbols:
            tok = self._symbol_to_token.get(sym)
            if tok:
                tokens.append(tok)
            else:
                self.logger.warning(f"Skipping subscription for {sym}: no instrument_token mapped.")
                
        if tokens:
            try:
                self._ticker.subscribe(tokens)
                k_mode = self._ticker.MODE_FULL
                if mode == "ltp":
                    k_mode = self._ticker.MODE_LTP
                elif mode == "quote":
                    k_mode = self._ticker.MODE_QUOTE
                    
                self._ticker.set_mode(k_mode, tokens)
                self.logger.info(f"Subscribed to {len(tokens)} tokens in '{mode}' mode.")
            except AttributeError:
                self.logger.warning(f"KiteTicker WebSocket is not fully connected yet. Will naturally auto-subscribe {len(tokens)} symbols upon successful _on_connect callback.")
            except Exception as e:
                self.logger.error(f"Failed to manually subscribe symbols: {e}")

    def unsubscribe_symbols(self, symbols: List[str]) -> None:
        if not self._ticker: return
        tokens = [self._symbol_to_token[s] for s in symbols if s in self._symbol_to_token]
        if tokens:
            self._ticker.unsubscribe(tokens)

    def get_last_tick(self, symbol: str) -> Optional[Tick]:
        """Return most recent Tick for a symbol, or None if no data yet."""
        with self._lock:
            return self._last_ticks.get(symbol)

    def get_snapshot(self, symbols: List[str]) -> Dict[str, Snapshot]:
        """Use Kite quote API to get a one-time snapshot for up to 500 symbols."""
        if not self._kite:
            return {}
            
        # Kite API expects symbols formatted securely with exchange prefixes
        prefixed_symbols = [f"NSE:{s}" if ":" not in s else s for s in symbols]
        
        try:
            quotes = self._kite.quote(prefixed_symbols)
        except Exception as e:
            self.logger.error(f"Failed to fetch quotes: {e}")
            return {}
            
        result = {}
        for kite_sym, data in quotes.items():
            plain_sym = kite_sym.split(":")[-1] if ":" in kite_sym else kite_sym
            
            ts = data.get('timestamp')
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except ValueError:
                    ts = datetime.now()
            elif not ts:
                ts = datetime.now()
                
            snap = Snapshot(
                symbol=plain_sym,
                instrument_token=data.get('instrument_token', 0),
                ltp=data.get('last_price', 0.0),
                ohlc=data.get('ohlc', {'open': 0, 'high': 0, 'low': 0, 'close': 0}),
                volume=data.get('volume', 0),
                oi=data.get('oi', None),
                timestamp=ts
            )
            result[plain_sym] = snap
            
        return result


@dataclass
class Bar:
    symbol: str
    interval: str       # "1m" or "5m"
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

class BarBuilder:
    def __init__(self, interval: str = "1m"):
        self.interval = interval
        self._current: Dict[str, Bar] = {}
        self._lock = threading.Lock()
        
        self._interval_sec = 60 # Default 1m
        if interval.endswith("m"):
            self._interval_sec = int(interval[:-1]) * 60
        elif interval.endswith("s"):
            self._interval_sec = int(interval[:-1])

    def _get_bucket_start(self, dt: datetime) -> datetime:
        """Floor the datetime to the nearest interval bucket timezone-safely."""
        ts = dt.timestamp()
        bucket_ts = (ts // self._interval_sec) * self._interval_sec
        return datetime.fromtimestamp(bucket_ts, tz=dt.tzinfo)

    def on_tick(self, tick: Tick) -> List[Bar]:
        """
        Update current bar for the symbol tracking cumulative limits across buckets explicitly.
        """
        completed_bars = []
        bucket_start = self._get_bucket_start(tick.timestamp)
        bucket_end = datetime.fromtimestamp(bucket_start.timestamp() + self._interval_sec, tz=bucket_start.tzinfo)
        
        with self._lock:
            current = self._current.get(tick.symbol)
            
            if current is None:
                # Prime the first accumulation
                self._current[tick.symbol] = Bar(
                    symbol=tick.symbol,
                    interval=self.interval,
                    start=bucket_start,
                    end=bucket_end,
                    open=tick.ltp,
                    high=tick.ltp,
                    low=tick.ltp,
                    close=tick.ltp,
                    volume=0
                )
                current = self._current[tick.symbol]
                current._start_volume = tick.volume
                from src.data_ingestion.intraday_context import add_intraday_bar_to_store
                add_intraday_bar_to_store(current)
            
            elif bucket_start > current.start:
                # Time bucket rolls over the threshold!
                completed_bars.append(current)
                
                # Initiate fresh bar structure mapping directly to current ticks
                self._current[tick.symbol] = Bar(
                    symbol=tick.symbol,
                    interval=self.interval,
                    start=bucket_start,
                    end=bucket_end,
                    open=tick.ltp,
                    high=tick.ltp,
                    low=tick.ltp,
                    close=tick.ltp,
                    volume=0
                )
                self._current[tick.symbol]._start_volume = tick.volume
                from src.data_ingestion.intraday_context import add_intraday_bar_to_store
                add_intraday_bar_to_store(self._current[tick.symbol])
                
            else:
                # Update bounds actively
                current.high = max(current.high, tick.ltp)
                current.low = min(current.low, tick.ltp)
                current.close = tick.ltp
                
                if hasattr(current, '_start_volume') and tick.volume >= current._start_volume:
                    current.volume = tick.volume - current._start_volume
                
                from src.data_ingestion.intraday_context import add_intraday_bar_to_store
                add_intraday_bar_to_store(current)
                
        return completed_bars

    def get_current_bar(self, symbol: str) -> Optional[Bar]:
        """Return the in-progress bar for a symbol, if any."""
        with self._lock:
            return self._current.get(symbol)

def make_default_live_client(symbol_to_token: Dict[str, int] | None = None) -> KiteLiveClient:
    """Convenience factory that loads env config and returns a ready client."""
    return KiteLiveClient(symbol_to_token=symbol_to_token)
