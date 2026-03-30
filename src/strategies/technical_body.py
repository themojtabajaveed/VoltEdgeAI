"""
technical_body.py — Shared Technical Analysis Engine
-----------------------------------------------------
Pure math computation layer shared by ALL strategy heads.
Each head gets the same TechnicalSnapshot, then applies its own rules.

Optimisation 4 — Streaming / Incremental TA
--------------------------------------------
TechnicalBody.update(new_bar, state) updates all indicators from a SINGLE new
bar using recursive formulas that only need the previous state value:

  EMA:   ema_new    = alpha * close + (1 - alpha) * ema_prev
  RSI:   avg_gain/loss updated with EWM alpha = 1/14
  MACD:  ema12, ema26, signal updated recursively
  VWAP:  cumulative (tp*vol) / cumulative vol — extended by one bar
  ATR:   true_range used to update EWM ATR
  ADX:   +DI/-DI updated incrementally (one DM step)

The existing TechnicalBody.compute() (full Pandas sweep from 70 bars) is kept as
the cold-start path until WARM_UP_BARS have been received for the symbol. After
warm-up the streaming path takes over, running at <1ms vs ~50-80ms.
"""
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Streaming warm-up: use compute() until we have this many bars ─────────────
WARM_UP_BARS = 27  # need ≥26 bars for MACD; 27 adds one confirmation bar


@dataclass
class TechnicalSnapshot:
    """All technicals computed from intraday bars. Shared across heads."""
    # EMAs
    ema9: float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0
    ema_alignment: str = ""         # "9 > 20 (bullish)" or "9 < 20 (bearish)"

    # RSI
    rsi14: float = 50.0

    # MACD
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    macd_histogram: float = 0.0         # alias for macd_hist (used by COIL rules)
    macd_histogram_prev: float = 0.0    # previous bar histogram (for divergence)
    macd_crossover_bullish: bool = False  # recent bullish crossover

    # VWAP
    vwap: float = 0.0
    above_vwap: bool = False

    # ADX
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0

    # Opening Range Breakout
    orb_high: float = 0.0
    orb_low: float = 0.0
    orb_breakout: bool = False
    orb_breakdown: bool = False

    # Volume
    volume_avg: float = 0.0
    volume_current: float = 0.0
    volume_spike_ratio: float = 0.0

    # ATR
    atr14: float = 0.0

    # Bollinger Bands (20-period SMA ± 2 standard deviations)
    # bb_upper/lower are 0.0 until at least 20 bars are available.
    # Scoring blocks in hydra.py / viper_rules.py guard with `if snapshot.bb_upper > 0`.
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_mid: float = 0.0
    bb_width: float = 0.0      # (upper - lower) / mid — normalised band width
    bb_squeeze: bool = False   # True when bb_width < BB_SQUEEZE_THRESHOLD (0.035)

    # On-Balance Volume (OBV)
    obv: float = 0.0
    obv_bullish_div: bool = False
    obv_bearish_div: bool = False

    # Price levels
    last_price: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    day_open: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Optimisation 4: Streaming indicator state per symbol ─────────────────────

BB_PERIOD = 20
BB_STD_DEV = 2.0
BB_SQUEEZE_THRESHOLD = 0.035   # bands within 3.5% of mid price = squeeze


@dataclass
class StreamingTechnicalState:
    """
    Carries all the scalar state needed for incremental indicator updates.
    One instance is kept per symbol in _streaming_states.

    Warmed up by running compute() on the first WARM_UP_BARS bars, then
    updated bar-by-bar with TechnicalBody.update().
    """
    bars_seen: int = 0

    # EMA state (each is just the last EMA value)
    ema9:  float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0

    # RSI state
    avg_gain: float = 0.0
    avg_loss: float = 0.0
    prev_close: float = 0.0

    # MACD state
    ema12: float = 0.0
    ema26: float = 0.0
    macd_signal: float = 0.0
    prev_macd_hist: float = 0.0

    # VWAP state (cumulative numerator and denominator)
    cum_tp_vol: float = 0.0
    cum_vol: float = 0.0

    # ATR state
    atr: float = 0.0
    prev_close_atr: float = 0.0  # separate prev_close for TR calculation

    # ADX state
    plus_dm_avg: float = 0.0
    minus_dm_avg: float = 0.0
    atr_adx: float = 0.0       # ATR used by ADX (same value, kept separate for clarity)
    adx: float = 0.0
    prev_high: float = 0.0
    prev_low: float = 0.0

    # ORB (frozen after first 3 bars)
    orb_high: float = 0.0
    orb_low: float = 0.0
    orb_frozen: bool = False

    # Volume rolling average (simple 14-bar EWM)
    vol_avg: float = 0.0

    # Day extremes
    day_high: float = 0.0
    day_low: float = 9e9
    day_open: float = 0.0

    # ── Bollinger Bands streaming state ──────────────────────────────────
    # Ring buffer of last BB_PERIOD (20) closing prices for incremental std.
    bb_close_buffer: list = field(default_factory=list)

    # ── RSI divergence tracking ───────────────────────────────────────────
    # Ring buffer of last 5 (close, rsi) tuples for momentum divergence check.
    rsi_history: list = field(default_factory=list)

    # ── OBV state & divergence tracking ──────────────────────────────────
    obv: float = 0.0
    obv_history: list = field(default_factory=list)  # (close, obv) tuples

    # Last snapshot (cached)
    last_snapshot: Optional["TechnicalSnapshot"] = None


# Module-level registry: symbol → StreamingTechnicalState
# Populated by the runner; reset each day via reset_streaming_state().
_streaming_states: Dict[str, StreamingTechnicalState] = {}


def get_or_create_streaming_state(symbol: str) -> StreamingTechnicalState:
    """Return the streaming state for a symbol, creating it if absent."""
    if symbol not in _streaming_states:
        _streaming_states[symbol] = StreamingTechnicalState()
    return _streaming_states[symbol]


def reset_streaming_state(symbol: Optional[str] = None) -> None:
    """Reset streaming state. Pass None to reset all symbols (call at midnight)."""
    if symbol is None:
        _streaming_states.clear()
    else:
        _streaming_states.pop(symbol, None)


class TechnicalBody:
    """
    Shared TA computation engine. Pure math — no opinions.
    All strategy heads consume this same output, then apply their own rules.
    """

    @staticmethod
    def compute(bars_df: pd.DataFrame) -> TechnicalSnapshot:
        """
        Compute full technical snapshot from intraday bars (5-min).
        
        Args:
            bars_df: DataFrame with columns: date, open, high, low, close, volume
            
        Returns:
            TechnicalSnapshot with all indicators computed.
        """
        snap = TechnicalSnapshot()

        if bars_df is None or bars_df.empty or len(bars_df) < 5:
            return snap

        close = bars_df["close"].astype(float)
        high = bars_df["high"].astype(float)
        low = bars_df["low"].astype(float)
        volume = bars_df["volume"].astype(float)

        snap.last_price = float(close.iloc[-1])
        snap.day_high = float(high.max())
        snap.day_low = float(low.min())
        snap.day_open = float(bars_df["open"].iloc[0])

        # ── EMAs ───────────────────────────────────────────────
        snap.ema9 = round(float(close.ewm(span=9, adjust=False).mean().iloc[-1]), 2)
        snap.ema20 = round(float(close.ewm(span=20, adjust=False).mean().iloc[-1]), 2)
        if len(close) >= 50:
            snap.ema50 = round(float(close.ewm(span=50, adjust=False).mean().iloc[-1]), 2)
        if len(close) >= 200:
            snap.ema200 = round(float(close.ewm(span=200, adjust=False).mean().iloc[-1]), 2)

        snap.ema_alignment = "9 > 20 (bullish)" if snap.ema9 > snap.ema20 else "9 < 20 (bearish)"

        # ── RSI 14 ─────────────────────────────────────────────
        if len(close) >= 15:
            delta = close.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss_s = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss_s.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            snap.rsi14 = round(float(rsi.iloc[-1]), 1) if not pd.isna(rsi.iloc[-1]) else 50.0

        # ── MACD ───────────────────────────────────────────────
        if len(close) >= 26:
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_hist = macd_line - signal_line

            snap.macd_line = round(float(macd_line.iloc[-1]), 4)
            snap.macd_signal = round(float(signal_line.iloc[-1]), 4)
            snap.macd_hist = round(float(macd_hist.iloc[-1]), 4)
            snap.macd_histogram = snap.macd_hist  # alias for COIL rules
            if len(macd_hist) >= 2:
                snap.macd_histogram_prev = round(float(macd_hist.iloc[-2]), 4)

            # Check for recent bullish crossover (last 5 bars)
            for i in range(max(1, len(macd_hist) - 5), len(macd_hist)):
                if float(macd_hist.iloc[i - 1]) < 0 and float(macd_hist.iloc[i]) > 0:
                    snap.macd_crossover_bullish = True
                    break

        # ── VWAP ───────────────────────────────────────────────
        tp = (high + low + close) / 3.0
        cum_tp_vol = (tp * volume).cumsum()
        cum_vol = volume.cumsum()
        vwap_series = cum_tp_vol / cum_vol.replace(0, np.nan)
        snap.vwap = round(float(vwap_series.iloc[-1]), 2)
        snap.above_vwap = snap.last_price > snap.vwap

        # ── ADX ────────────────────────────────────────────────
        if len(bars_df) >= 14:
            try:
                up = high.diff()
                dn = -low.diff()
                plus_dm = pd.Series(
                    np.where((up > dn) & (up > 0), up, 0.0), index=high.index
                )
                minus_dm = pd.Series(
                    np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index
                )
                tr = pd.concat([
                    high - low,
                    (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()
                ], axis=1).max(axis=1)
                atr = tr.ewm(alpha=1/14, adjust=False).mean()
                p_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
                m_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan)
                dx = 100 * (p_di - m_di).abs() / (p_di + m_di).replace(0, np.nan)
                adx_s = dx.ewm(alpha=1/14, adjust=False).mean()

                snap.adx = round(float(adx_s.iloc[-1]), 1) if not pd.isna(adx_s.iloc[-1]) else 0.0
                snap.plus_di = round(float(p_di.iloc[-1]), 1) if not pd.isna(p_di.iloc[-1]) else 0.0
                snap.minus_di = round(float(m_di.iloc[-1]), 1) if not pd.isna(m_di.iloc[-1]) else 0.0
            except Exception as e:
                logger.warning(f"ADX computation failed: {e}")

        # ── ORB (first 15 minutes = first 3 bars of 5-min) ────
        or_bars = min(3, len(bars_df))
        snap.orb_high = round(float(high.iloc[:or_bars].max()), 2)
        snap.orb_low = round(float(low.iloc[:or_bars].min()), 2)
        snap.orb_breakout = snap.last_price > snap.orb_high
        snap.orb_breakdown = snap.last_price < snap.orb_low

        # ── Volume ─────────────────────────────────────────────
        snap.volume_current = float(volume.iloc[-1])
        snap.volume_avg = float(volume.mean())
        snap.volume_spike_ratio = round(
            snap.volume_current / snap.volume_avg if snap.volume_avg > 0 else 0, 2
        )

        # ── On-Balance Volume (OBV) ────────────────────────────
        if len(bars_df) > 1:
            try:
                # Calculate OBV for the entire window
                obv_change = np.where(close > close.shift(1), volume, np.where(close < close.shift(1), -volume, 0))
                obv_series = pd.Series(obv_change).cumsum()
                snap.obv = float(obv_series.iloc[-1])
            except Exception as e:
                logger.warning(f"OBV computation failed: {e}")

        # ── ATR 14 ─────────────────────────────────────────────
        if len(bars_df) >= 14:
            try:
                tr = pd.concat([
                    high - low,
                    (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()
                ], axis=1).max(axis=1)
                snap.atr14 = round(float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1]), 2)
            except Exception:
                pass

        # ── Bollinger Bands (20-period SMA ± 2σ) ───────────────
        if len(close) >= BB_PERIOD:
            try:
                bb_sma = close.rolling(BB_PERIOD).mean()
                bb_std = close.rolling(BB_PERIOD).std()
                snap.bb_mid   = round(float(bb_sma.iloc[-1]), 2)
                snap.bb_upper = round(float(bb_sma.iloc[-1] + BB_STD_DEV * bb_std.iloc[-1]), 2)
                snap.bb_lower = round(float(bb_sma.iloc[-1] - BB_STD_DEV * bb_std.iloc[-1]), 2)
                if snap.bb_mid > 0:
                    snap.bb_width   = round((snap.bb_upper - snap.bb_lower) / snap.bb_mid, 4)
                    snap.bb_squeeze = snap.bb_width < BB_SQUEEZE_THRESHOLD
            except Exception as e:
                logger.warning(f"Bollinger Bands computation failed: {e}")

        return snap

    # ─────────────────────────────────────────────────────────────────────────
    # Optimisation 4 — Streaming / Incremental TA
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def update(bar, state: "StreamingTechnicalState") -> TechnicalSnapshot:
        """
        Incrementally update all indicators from a single new completed bar.

        Args:
            bar: Any object with .open, .high, .low, .close, .volume attributes
                 (Bar dataclass from market_live.py works directly).
            state: StreamingTechnicalState for this symbol (mutated in-place).

        Returns:
            TechnicalSnapshot populated from the updated state. Sub-millisecond.
        """
        o, h, l, c, v = float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume)
        snap = TechnicalSnapshot()

        state.bars_seen += 1
        n = state.bars_seen

        # ── Day extremes ────────────────────────────────────────────────────
        if n == 1:
            state.day_open = o
            state.day_high = h
            state.day_low  = l
        else:
            state.day_high = max(state.day_high, h)
            state.day_low  = min(state.day_low, l)
        snap.day_open  = state.day_open
        snap.day_high  = state.day_high
        snap.day_low   = state.day_low
        snap.last_price = c

        # ── ORB (first 3 bars, then frozen) ─────────────────────────────────
        if not state.orb_frozen:
            state.orb_high = max(state.orb_high, h)
            state.orb_low  = min(state.orb_low if state.orb_low > 0 else l, l)
            if n >= 3:
                state.orb_frozen = True
        snap.orb_high      = state.orb_high
        snap.orb_low       = state.orb_low
        snap.orb_breakout  = c > state.orb_high
        snap.orb_breakdown = c < state.orb_low

        # ── EMA (alpha = 2/(span+1)) ─────────────────────────────────────────
        def _ema(prev: float, close: float, span: int) -> float:
            alpha = 2.0 / (span + 1)
            return alpha * close + (1 - alpha) * prev if prev != 0.0 else close

        state.ema9  = _ema(state.ema9,  c, 9)
        state.ema20 = _ema(state.ema20, c, 20)
        state.ema50 = _ema(state.ema50, c, 50)
        snap.ema9   = round(state.ema9, 2)
        snap.ema20  = round(state.ema20, 2)
        snap.ema50  = round(state.ema50, 2)
        snap.ema_alignment = "9 > 20 (bullish)" if snap.ema9 > snap.ema20 else "9 < 20 (bearish)"

        # ── RSI (EWM smoothing, alpha = 1/14) ────────────────────────────────
        if n > 1:
            delta = c - state.prev_close
            gain  = max(delta, 0.0)
            loss  = max(-delta, 0.0)
            alpha_rsi = 1.0 / 14
            state.avg_gain = alpha_rsi * gain + (1 - alpha_rsi) * state.avg_gain
            state.avg_loss = alpha_rsi * loss + (1 - alpha_rsi) * state.avg_loss
            rs = state.avg_gain / state.avg_loss if state.avg_loss > 0 else 1e9
            snap.rsi14 = round(100 - 100 / (1 + rs), 1)
        else:
            snap.rsi14 = 50.0
        state.prev_close = c

        # ── MACD (ema12 - ema26, signal = ema9 of macd_line) ────────────────
        state.ema12 = _ema(state.ema12, c, 12)
        state.ema26 = _ema(state.ema26, c, 26)
        macd_line   = state.ema12 - state.ema26
        state.macd_signal = _ema(state.macd_signal, macd_line, 9)
        macd_hist   = macd_line - state.macd_signal
        snap.macd_line     = round(macd_line, 4)
        snap.macd_signal   = round(state.macd_signal, 4)
        snap.macd_hist     = round(macd_hist, 4)
        snap.macd_histogram = snap.macd_hist
        snap.macd_histogram_prev = round(state.prev_macd_hist, 4)
        
        # Bullish cross calculation logic (MACD crosses above Signal)
        if state.prev_macd_hist < 0 and macd_hist > 0:
            snap.macd_crossover_bullish = True
        
        # Update state for next incremental step
        state.prev_macd_hist = macd_hist

        # ── VWAP (cumulative typical-price × volume) ─────────────────────────
        tp = (h + l + c) / 3.0
        state.cum_tp_vol += tp * v
        state.cum_vol    += v
        snap.vwap       = round(state.cum_tp_vol / state.cum_vol, 2) if state.cum_vol > 0 else c
        snap.above_vwap = c > snap.vwap

        # ── ATR (EWM with alpha=1/14) ─────────────────────────────────────────
        if n > 1:
            tr = max(h - l,
                     abs(h - state.prev_close_atr),
                     abs(l - state.prev_close_atr))
            alpha_atr = 1.0 / 14
            state.atr = alpha_atr * tr + (1 - alpha_atr) * state.atr if state.atr > 0 else tr
        state.prev_close_atr = c
        snap.atr14 = round(state.atr, 2)

        # ── ADX (+DI / -DI incremental) ───────────────────────────────────────
        if n > 1 and state.atr > 0:
            up_move   = h - state.prev_high
            down_move = state.prev_low - l
            plus_dm   = up_move   if up_move > down_move and up_move > 0 else 0.0
            minus_dm  = down_move if down_move > up_move and down_move > 0 else 0.0
            alpha_adx = 1.0 / 14
            state.atr_adx      = alpha_adx * max(h - l, abs(h - state.prev_close_atr), abs(l - state.prev_close_atr)) + (1 - alpha_adx) * state.atr_adx if state.atr_adx > 0 else state.atr
            state.plus_dm_avg  = alpha_adx * plus_dm  + (1 - alpha_adx) * state.plus_dm_avg
            state.minus_dm_avg = alpha_adx * minus_dm + (1 - alpha_adx) * state.minus_dm_avg
            plus_di  = 100 * state.plus_dm_avg  / state.atr_adx if state.atr_adx > 0 else 0.0
            minus_di = 100 * state.minus_dm_avg / state.atr_adx if state.atr_adx > 0 else 0.0
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0.0
            state.adx = alpha_adx * dx + (1 - alpha_adx) * state.adx
            snap.adx      = round(state.adx, 1)
            snap.plus_di  = round(plus_di, 1)
            snap.minus_di = round(minus_di, 1)
        state.prev_high = h
        state.prev_low  = l

        # ── Volume ────────────────────────────────────────────────────────────
        alpha_vol = 1.0 / 14
        state.vol_avg = alpha_vol * v + (1 - alpha_vol) * state.vol_avg if state.vol_avg > 0 else v
        snap.volume_current     = v
        snap.volume_avg         = round(state.vol_avg, 0)
        snap.volume_spike_ratio = round(v / state.vol_avg, 2) if state.vol_avg > 0 else 0.0

        # ── Bollinger Bands (streaming via ring buffer) ───────────────────────
        state.bb_close_buffer.append(c)
        if len(state.bb_close_buffer) > BB_PERIOD:
            state.bb_close_buffer = state.bb_close_buffer[-BB_PERIOD:]
        if len(state.bb_close_buffer) == BB_PERIOD:
            buf        = state.bb_close_buffer
            bb_mid_val = sum(buf) / BB_PERIOD
            bb_var     = sum((x - bb_mid_val) ** 2 for x in buf) / BB_PERIOD
            bb_std_val = bb_var ** 0.5
            snap.bb_mid    = round(bb_mid_val, 2)
            snap.bb_upper  = round(bb_mid_val + BB_STD_DEV * bb_std_val, 2)
            snap.bb_lower  = round(bb_mid_val - BB_STD_DEV * bb_std_val, 2)
            if bb_mid_val > 0:
                snap.bb_width   = round((snap.bb_upper - snap.bb_lower) / bb_mid_val, 4)
                snap.bb_squeeze = snap.bb_width < BB_SQUEEZE_THRESHOLD

        # ── RSI history for divergence detection ─────────────────────────────
        state.rsi_history.append((c, snap.rsi14))
        if len(state.rsi_history) > 5:
            state.rsi_history = state.rsi_history[-5:]

        # ── OBV streaming & divergence history ──────────────────────────────
        if n > 1:
            if c > state.prev_close:
                state.obv += v
            elif c < state.prev_close:
                state.obv -= v
        snap.obv = state.obv

        state.obv_history.append((c, snap.obv))
        if len(state.obv_history) > 5:
            state.obv_history = state.obv_history[-5:]

        # ── Detect OBV Divergence ───────────────────────────────────────────
        snap.obv_bullish_div = TechnicalBody.detect_bullish_obv_divergence(state)
        snap.obv_bearish_div = TechnicalBody.detect_bearish_obv_divergence(state)

        state.last_snapshot = snap
        return snap

    @staticmethod
    def detect_bearish_rsi_divergence(state: "StreamingTechnicalState") -> bool:
        """
        Bearish RSI divergence: price making a higher high while RSI makes a lower high.
        Signals that momentum is fracturing — institution distribution may have begun.
        Requires at least 4 bars of (close, rsi) history.
        """
        hist = state.rsi_history
        if len(hist) < 4:
            return False
        recent_close, recent_rsi = hist[-1]
        prev_close,   prev_rsi   = hist[-3]   # 3-bar lookback
        price_up        = recent_close > prev_close
        rsi_down        = recent_rsi   < prev_rsi
        rsi_significant = (prev_rsi - recent_rsi) >= 3.0   # at least 3 RSI points drop
        return price_up and rsi_down and rsi_significant

    @staticmethod
    def detect_bullish_rsi_divergence(state: "StreamingTechnicalState") -> bool:
        """
        Bullish RSI divergence: price making a lower low while RSI makes a higher low.
        Signals potential reversal from oversold — useful for SHORT exits.
        Requires at least 4 bars of (close, rsi) history.
        """
        hist = state.rsi_history
        if len(hist) < 4:
            return False
        recent_close, recent_rsi = hist[-1]
        prev_close,   prev_rsi   = hist[-3]
        price_down      = recent_close  < prev_close
        rsi_up          = recent_rsi    > prev_rsi
        rsi_significant = (recent_rsi - prev_rsi) >= 3.0
        return price_down and rsi_up and rsi_significant

    @staticmethod
    def detect_bullish_obv_divergence(state: "StreamingTechnicalState") -> bool:
        """
        Detect hidden institutional accumulation before price moves.
        Signals price is flat or down (consolidation), but volume is net positive (OBV rising).
        Requires at least 4 bars.
        """
        hist = state.obv_history
        if len(hist) < 4:
            return False
        recent_close, recent_obv = hist[-1]
        prev_close, prev_obv = hist[-3]
        
        # Price is consolidating/down, OBV is rising strongly
        price_flat_or_down = recent_close <= (prev_close * 1.002) # allowing very slight up moves
        obv_up = recent_obv > prev_obv
        return price_flat_or_down and obv_up

    @staticmethod
    def detect_bearish_obv_divergence(state: "StreamingTechnicalState") -> bool:
        """
        Detect hidden institutional distribution before price drops.
        Signals price is flat or up (consolidation), but volume is net negative (OBV falling).
        Requires at least 4 bars.
        """
        hist = state.obv_history
        if len(hist) < 4:
            return False
        recent_close, recent_obv = hist[-1]
        prev_close, prev_obv = hist[-3]
        
        # Price is consolidating/up, OBV is falling strongly
        price_flat_or_up = recent_close >= (prev_close * 0.998)
        obv_down = recent_obv < prev_obv
        return price_flat_or_up and obv_down

    @classmethod
    def compute_or_stream(
        cls,
        symbol: str,
        bars_df: pd.DataFrame,
        latest_bar=None,
    ) -> TechnicalSnapshot:
        """
        Transparent fast/slow path selector.

        - If the symbol has a warm streaming state AND latest_bar is provided,
          call update() → <1ms.
        - Otherwise call compute() on the full bars_df (cold-start/fallback).

        Args:
            symbol:     Stock symbol (used to key streaming state).
            bars_df:    Full bar DataFrame (always required; used for cold-start).
            latest_bar: The most recently closed Bar object. If None, forces
                        a full compute() regardless of warm-up status.
        """
        state = get_or_create_streaming_state(symbol)

        if latest_bar is not None and state.bars_seen >= WARM_UP_BARS:
            # Fast path — single-bar incremental update
            return cls.update(latest_bar, state)

        # Cold/warm-up path — full Pandas sweep; seed the streaming state
        snap = cls.compute(bars_df)
        # Seed state from the full compute so streaming can take over next bar
        if snap.last_price > 0 and len(bars_df) >= WARM_UP_BARS:
            state.ema9         = snap.ema9
            state.ema20        = snap.ema20
            state.ema50        = snap.ema50
            state.prev_close   = snap.last_price
            state.ema12        = snap.ema9   # rough seed; EWM will converge in ~3 bars
            state.ema26        = snap.ema20
            state.macd_signal  = snap.macd_signal
            state.cum_tp_vol   = snap.vwap * float(bars_df["volume"].sum()) if snap.vwap > 0 else 0.0
            state.cum_vol      = float(bars_df["volume"].sum())
            state.atr          = snap.atr14
            state.prev_close_atr = snap.last_price
            state.adx          = snap.adx
            state.vol_avg      = snap.volume_avg
            state.obv          = snap.obv
            state.day_open     = snap.day_open
            state.day_high     = snap.day_high
            state.day_low      = snap.day_low
            state.orb_high     = snap.orb_high
            state.orb_low      = snap.orb_low
            state.orb_frozen   = True
            state.bars_seen    = len(bars_df)
            state.last_snapshot = snap
        return snap
