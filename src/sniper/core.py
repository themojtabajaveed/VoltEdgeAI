import numpy as np
import pandas as pd
from src.sources.nse_prices import fetch_daily_ohlcv, compute_ema, compute_rsi, compute_avg_volume
from src.sniper.antigravity import evaluate_symbol, AntigravityStatus

def compute_macd(close_series: pd.Series):
    ema_12 = close_series.ewm(span=12, adjust=False).mean()
    ema_26 = close_series.ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def compute_bollinger_bands(close_series: pd.Series, period: int = 20, num_std: float = 2.0):
    sma = close_series.rolling(window=period).mean()
    std = close_series.rolling(window=period).std()
    upper = sma + (num_std * std)
    lower = sma - (num_std * std)
    bandwidth = (upper - lower) / sma
    return upper, lower, sma, bandwidth

def compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    up_move = high.diff()
    down_move = -low.diff()
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)
    
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Wilder's smoothing
    def wilder_smooth(series, n):
        return series.ewm(alpha=1/n, adjust=False).mean()
        
    atr = wilder_smooth(tr, period)
    plus_di = 100 * wilder_smooth(plus_dm, period) / atr
    minus_di = 100 * wilder_smooth(minus_dm, period) / atr
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = wilder_smooth(dx, period)
    return adx, plus_di, minus_di

def evaluate_signal(symbol: str) -> dict:
    """
    Fetch daily OHLCV, compute indicators, and apply Sniper v1 rules.
    """
    df = fetch_daily_ohlcv(symbol, days=252) # Fetch enough for 52-week high
    
    if df is None or df.empty or len(df) < 200:
        return {
            "symbol": symbol,
            "status": "VETO",
            "reason": "Insufficient history",
            "close": None,
            "ema_200": None,
            "rsi_14": None,
            "vol_today": None,
            "vol_20": None,
            "macd": None,
            "macd_signal": None,
            "macd_hist": None,
            "adx_14": None,
            "plus_di": None,
            "minus_di": None,
            "bb_breakout": None,
            "near_52w_high": None
        }

    high = df["high"]
    low = df["low"]
    close = df["close"]
    volume = df["volume"]
    
    ema_200 = compute_ema(close, 200)
    rsi_14 = compute_rsi(close, 14)
    vol_20 = compute_avg_volume(volume, 20)
    
    macd_line, macd_signal, macd_hist = compute_macd(close)
    upper_bb, lower_bb, sma_bb, bandwidth = compute_bollinger_bands(close, 20, 2.0)
    adx, plus_di, minus_di = compute_adx(high, low, close, 14)
    
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    
    c_close = float(latest["close"])
    c_high = float(latest["high"])
    c_low = float(latest["low"])
    c_vol = float(latest["volume"])
    
    c_ema_200 = float(ema_200) if ema_200 is not None else 0.0
    c_rsi_14 = float(rsi_14) if rsi_14 is not None else 0.0
    c_vol_20 = float(vol_20) if vol_20 is not None else 0.0
    
    c_macd = float(macd_line.iloc[-1])
    c_macd_signal = float(macd_signal.iloc[-1])
    c_macd_hist = float(macd_hist.iloc[-1])
    p_macd_hist = float(macd_hist.iloc[-2])
    
    c_adx = float(adx.iloc[-1])
    p_adx = float(adx.iloc[-2])
    c_plus_di = float(plus_di.iloc[-1])
    c_minus_di = float(minus_di.iloc[-1])
    
    c_bandwidth = float(bandwidth.iloc[-1])
    bw_100_avg = bandwidth.tail(100).mean()
    bw_100_30th = bandwidth.tail(100).quantile(0.30)
    bw_5_ago = float(bandwidth.iloc[-6]) # 5 days ago (index -6 compared to -1)
    
    c_upper_bb = float(upper_bb.iloc[-1])
    
    high_52w = float(high.tail(252).max())

    # --- Hard Veto Conditions ---
    veto_reasons = []
    
    if c_close < c_ema_200:
        veto_reasons.append("Below 200 EMA")
    if c_vol < 2 * c_vol_20:
        veto_reasons.append(f"Volume < 2x 20-day avg ({c_vol:,.0f} vs {2*c_vol_20:,.0f})")
    if c_rsi_14 < 50:
        veto_reasons.append("RSI below 50 (no momentum)")
        
    if veto_reasons:
        return {
            "symbol": symbol,
            "status": "VETO",
            "reason": "; ".join(veto_reasons),
            "close": c_close,
            "ema_200": c_ema_200,
            "rsi_14": c_rsi_14,
            "vol_today": c_vol,
            "vol_20": c_vol_20,
            "macd": c_macd,
            "macd_signal": c_macd_signal,
            "macd_hist": c_macd_hist,
            "adx_14": c_adx,
            "plus_di": c_plus_di,
            "minus_di": c_minus_di,
            "bb_breakout": False,
            "near_52w_high": False
        }

    # --- Entry-Quality Checks ---
    # Breakout structure
    near_52w_high = (c_close >= 0.95 * high_52w)
    top_25_pct_range = c_close >= c_low + 0.75 * (c_high - c_low)
    
    # Bollinger Squeeze Resolved
    bb_squeeze = (bw_5_ago <= bw_100_30th)
    bb_breakout = bb_squeeze and (c_bandwidth > bw_5_ago) and (c_close > c_upper_bb)
    
    # MACD Bullish
    macd_bullish = (c_macd > c_macd_signal) and (c_macd_hist > 0) and (c_macd_hist > p_macd_hist)
    
    # ADX Trend
    adx_trend = (c_adx >= 25) and (c_adx > p_adx) and (c_plus_di > c_minus_di)
    
    # Final check
    if not (c_rsi_14 >= 60 and near_52w_high and top_25_pct_range and bb_breakout and macd_bullish and adx_trend):
        return {
            "symbol": symbol,
            "status": "VETO",
            "reason": "No breakout setup (conditions not fully met)",
            "close": c_close,
            "ema_200": c_ema_200,
            "rsi_14": c_rsi_14,
            "vol_today": c_vol,
            "vol_20": c_vol_20,
            "macd": c_macd,
            "macd_signal": c_macd_signal,
            "macd_hist": c_macd_hist,
            "adx_14": c_adx,
            "plus_di": c_plus_di,
            "minus_di": c_minus_di,
            "bb_breakout": bb_breakout,
            "near_52w_high": near_52w_high
        }

    ag_decision = evaluate_symbol(symbol)
    
    ag_dict = {
        "status": ag_decision.status.value if ag_decision.status else None,
        "z_score": ag_decision.z_score,
        "vwap": ag_decision.vwap,
        "ltp": ag_decision.ltp,
        "sigma_vwap": ag_decision.sigma_vwap,
        "raw_reason": ag_decision.reason
    }
    
    final_status = "KEEP"
    final_reason = "Passes Sniper v1 breakout rules"
    
    if ag_decision.status == AntigravityStatus.WAITING_FOR_GRAVITY:
        final_status = "WAIT"
        z_val = ag_decision.z_score or 0.0
        final_reason += f" | Antigravity: Z-score={z_val:.2f} in WAITING_FOR_GRAVITY zone (price stretched above VWAP)"
    elif ag_decision.status == AntigravityStatus.BEAR_CONTROL:
        final_status = "VETO"
        final_reason += f" | Antigravity: Price is below VWAP, flagged BEAR_CONTROL"

    return {
        "symbol": symbol,
        "status": final_status,
        "reason": final_reason,
        "close": c_close,
        "ema_200": c_ema_200,
        "rsi_14": c_rsi_14,
        "vol_today": c_vol,
        "vol_20": c_vol_20,
        "macd": c_macd,
        "macd_signal": c_macd_signal,
        "macd_hist": c_macd_hist,
        "adx_14": c_adx,
        "plus_di": c_plus_di,
        "minus_di": c_minus_di,
        "bb_breakout": bb_breakout,
        "near_52w_high": near_52w_high,
        "antigravity": ag_dict
    }
