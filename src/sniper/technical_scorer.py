"""
technical_scorer.py
-------------------
Replaces the binary Sniper veto system with a 0–100 scoring system.

A professional trader doesn't use binary gates — they weigh multiple factors
and take the trade when enough evidence aligns. This scorer does exactly that.

Score Components:
  Daily Structure   (30 pts max)  — long-term trend health
  Intraday Setup    (40 pts max)  — is the chart actionable RIGHT NOW?
  Momentum Quality  (30 pts max)  — strength & conviction of the move

Entry thresholds (configurable):
  LONG  in bullish regime:  >= 55
  LONG  in neutral regime:  >= 65
  LONG  in bearish regime:  >= 75
  SHORT in bearish regime:  >= 55
  SHORT in neutral regime:  >= 65
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import logging
import math

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ── Data Models ────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    """Itemised breakdown so we can log WHY a score is what it is."""
    component: str    # "daily", "intraday", "momentum"
    item: str         # e.g. "above_ema200"
    points: float     # points awarded (can be 0)
    max_points: float # max possible for this item
    detail: str       # human-readable explanation


@dataclass
class TechScore:
    symbol: str
    total: float                          # 0–100
    daily_score: float                    # 0–30
    intraday_score: float                 # 0–40
    momentum_score: float                 # 0–30
    breakdown: List[ScoreBreakdown] = field(default_factory=list)
    direction: str = "LONG"               # "LONG" or "SHORT"
    ltp: float = 0.0
    vwap: float = 0.0
    atr: float = 0.0
    distance_to_resistance_pct: float = 0.0
    distance_to_support_pct: float = 0.0

    @property
    def summary(self) -> str:
        parts = [f"{self.symbol} [{self.direction}] Score={self.total:.0f}/100"]
        parts.append(f"  Daily={self.daily_score:.0f}/30  Intra={self.intraday_score:.0f}/40  Mom={self.momentum_score:.0f}/30")
        return " | ".join(parts)


# ── Helper computations ───────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if not pd.isna(val) else 50.0

def _macd(close: pd.Series):
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)

    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)

    def wilder(s, n):
        return s.ewm(alpha=1/n, adjust=False).mean()

    atr = wilder(tr, period)
    plus_di = 100 * wilder(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100 * wilder(minus_dm, period) / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = wilder(dx, period)
    return adx, plus_di, minus_di


# ── Core Scorer ────────────────────────────────────────────────────────────

class TechnicalScorer:
    """
    Scores a stock 0–100 across three axes.
    Works for both LONG and SHORT candidates.
    """

    def score_long(
        self,
        symbol: str,
        daily_df: pd.DataFrame,
        intraday_bars: List,          # list of bar objects (.open .high .low .close .volume .start/.timestamp)
        nifty_change_pct: float = 0.0,  # intraday Nifty % change for relative strength
    ) -> TechScore:
        """Score a LONG candidate."""
        bd: List[ScoreBreakdown] = []
        ts = TechScore(symbol=symbol, total=0, daily_score=0, intraday_score=0, momentum_score=0, direction="LONG")

        # ── DAILY STRUCTURE (30 pts) ──────────────────────────────────────
        d_score = self._score_daily_long(daily_df, bd)
        ts.daily_score = d_score

        # ── INTRADAY SETUP (40 pts) ───────────────────────────────────────
        i_score, vwap_val = self._score_intraday_long(intraday_bars, bd)
        ts.intraday_score = i_score
        ts.vwap = vwap_val

        # ── MOMENTUM QUALITY (30 pts) ─────────────────────────────────────
        m_score = self._score_momentum_long(daily_df, intraday_bars, nifty_change_pct, bd)
        ts.momentum_score = m_score

        ts.total = d_score + i_score + m_score
        ts.breakdown = bd

        # Attach useful metadata
        if not daily_df.empty:
            ts.ltp = float(daily_df["close"].iloc[-1])
        if intraday_bars:
            ts.ltp = intraday_bars[-1].close

        return ts

    def score_short(
        self,
        symbol: str,
        daily_df: pd.DataFrame,
        intraday_bars: List,
        nifty_change_pct: float = 0.0,
    ) -> TechScore:
        """Score a SHORT candidate (mirror of LONG logic)."""
        bd: List[ScoreBreakdown] = []
        ts = TechScore(symbol=symbol, total=0, daily_score=0, intraday_score=0, momentum_score=0, direction="SHORT")

        d_score = self._score_daily_short(daily_df, bd)
        ts.daily_score = d_score

        i_score, vwap_val = self._score_intraday_short(intraday_bars, bd)
        ts.intraday_score = i_score
        ts.vwap = vwap_val

        m_score = self._score_momentum_short(daily_df, intraday_bars, nifty_change_pct, bd)
        ts.momentum_score = m_score

        ts.total = d_score + i_score + m_score
        ts.breakdown = bd

        if intraday_bars:
            ts.ltp = intraday_bars[-1].close

        return ts

    # ──────────────────────────────────────────────────────────────────────
    # DAILY STRUCTURE (max 30 pts)
    # ──────────────────────────────────────────────────────────────────────

    def _score_daily_long(self, df: pd.DataFrame, bd: list) -> float:
        if df is None or df.empty or len(df) < 50:
            bd.append(ScoreBreakdown("daily", "insufficient_data", 0, 30, "Less than 50 bars of daily history"))
            return 0.0

        close = df["close"]
        high = df["high"]
        score = 0.0

        # 1. Above EMA 200 (+10)
        if len(df) >= 200:
            ema200 = float(_ema(close, 200).iloc[-1])
            c = float(close.iloc[-1])
            if c > ema200:
                pts = 10.0
                # Partial credit: within 2% below EMA200 gets 5 pts
            elif c > ema200 * 0.98:
                pts = 5.0
            else:
                pts = 0.0
            score += pts
            bd.append(ScoreBreakdown("daily", "above_ema200", pts, 10, f"Close={c:.2f}, EMA200={ema200:.2f}"))
        else:
            # Not enough for EMA200, use EMA50
            ema50 = float(_ema(close, 50).iloc[-1])
            c = float(close.iloc[-1])
            pts = 7.0 if c > ema50 else 0.0
            score += pts
            bd.append(ScoreBreakdown("daily", "above_ema50_fallback", pts, 10, f"Close={c:.2f}, EMA50={ema50:.2f}"))

        # 2. RSI in sweet spot: 50-70 (+5), 40-50 or 70-80 (+2)
        rsi = _rsi(close, 14)
        if 50 <= rsi <= 70:
            pts = 5.0
        elif 40 <= rsi < 50 or 70 < rsi <= 80:
            pts = 2.0
        else:
            pts = 0.0
        score += pts
        bd.append(ScoreBreakdown("daily", "rsi_sweet_spot", pts, 5, f"RSI(14)={rsi:.1f}"))

        # 3. MACD histogram expanding (+5)
        _, _, hist = _macd(close)
        h_now = float(hist.iloc[-1])
        h_prev = float(hist.iloc[-2])
        if h_now > 0 and h_now > h_prev:
            pts = 5.0
        elif h_now > 0:
            pts = 2.0
        else:
            pts = 0.0
        score += pts
        bd.append(ScoreBreakdown("daily", "macd_hist_expanding", pts, 5, f"Hist={h_now:.4f}, Prev={h_prev:.4f}"))

        # 4. ADX > 20 with +DI > -DI (+5)
        adx, pdi, mdi = _adx(df["high"], df["low"], close, 14)
        a = float(adx.iloc[-1])
        pd_val = float(pdi.iloc[-1])
        md_val = float(mdi.iloc[-1])
        if a > 25 and pd_val > md_val:
            pts = 5.0
        elif a > 20 and pd_val > md_val:
            pts = 3.0
        elif pd_val > md_val:
            pts = 1.0
        else:
            pts = 0.0
        score += pts
        bd.append(ScoreBreakdown("daily", "adx_trend", pts, 5, f"ADX={a:.1f}, +DI={pd_val:.1f}, -DI={md_val:.1f}"))

        # 5. Near 52-week high (+5)
        high_52w = float(high.tail(252).max()) if len(high) >= 252 else float(high.max())
        c = float(close.iloc[-1])
        dist = (high_52w - c) / high_52w * 100 if high_52w > 0 else 99
        if dist <= 5:
            pts = 5.0
        elif dist <= 10:
            pts = 3.0
        elif dist <= 20:
            pts = 1.0
        else:
            pts = 0.0
        score += pts
        bd.append(ScoreBreakdown("daily", "near_52w_high", pts, 5, f"52wHigh={high_52w:.2f}, Dist={dist:.1f}%"))

        return min(score, 30.0)

    def _score_daily_short(self, df: pd.DataFrame, bd: list) -> float:
        """Mirror scoring for SHORT candidates."""
        if df is None or df.empty or len(df) < 50:
            bd.append(ScoreBreakdown("daily", "insufficient_data", 0, 30, "Less than 50 bars"))
            return 0.0

        close = df["close"]
        low = df["low"]
        score = 0.0

        # 1. Below EMA 200 (+10)
        if len(df) >= 200:
            ema200 = float(_ema(close, 200).iloc[-1])
            c = float(close.iloc[-1])
            pts = 10.0 if c < ema200 else (5.0 if c < ema200 * 1.02 else 0.0)
        else:
            ema50 = float(_ema(close, 50).iloc[-1])
            c = float(close.iloc[-1])
            pts = 7.0 if c < ema50 else 0.0
        score += pts
        bd.append(ScoreBreakdown("daily", "below_ema200", pts, 10, f"Close={c:.2f}"))

        # 2. RSI < 50 (+5)
        rsi = _rsi(close, 14)
        if 30 <= rsi <= 50:
            pts = 5.0
        elif 20 <= rsi < 30:
            pts = 2.0
        else:
            pts = 0.0
        score += pts
        bd.append(ScoreBreakdown("daily", "rsi_weak", pts, 5, f"RSI={rsi:.1f}"))

        # 3. MACD histogram negative and expanding (+5)
        _, _, hist = _macd(close)
        h = float(hist.iloc[-1])
        hp = float(hist.iloc[-2])
        pts = 5.0 if (h < 0 and h < hp) else (2.0 if h < 0 else 0.0)
        score += pts
        bd.append(ScoreBreakdown("daily", "macd_bearish", pts, 5, f"Hist={h:.4f}"))

        # 4. ADX > 20 with -DI > +DI (+5)
        adx, pdi, mdi = _adx(df["high"], df["low"], close, 14)
        a = float(adx.iloc[-1])
        pd_val = float(pdi.iloc[-1])
        md_val = float(mdi.iloc[-1])
        pts = 5.0 if (a > 25 and md_val > pd_val) else (3.0 if a > 20 and md_val > pd_val else 0.0)
        score += pts
        bd.append(ScoreBreakdown("daily", "adx_short_trend", pts, 5, f"ADX={a:.1f}"))

        # 5. Near 52-week low (+5)
        low_52w = float(low.tail(252).min()) if len(low) >= 252 else float(low.min())
        c = float(close.iloc[-1])
        dist = (c - low_52w) / low_52w * 100 if low_52w > 0 else 99
        pts = 5.0 if dist <= 5 else (3.0 if dist <= 10 else 0.0)
        score += pts
        bd.append(ScoreBreakdown("daily", "near_52w_low", pts, 5, f"52wLow={low_52w:.2f}, Dist={dist:.1f}%"))

        return min(score, 30.0)

    # ──────────────────────────────────────────────────────────────────────
    # INTRADAY SETUP (max 40 pts)
    # ──────────────────────────────────────────────────────────────────────

    def _compute_vwap(self, bars: List) -> float:
        """Compute VWAP from intraday bars."""
        if not bars:
            return 0.0
        cum_pv = 0.0
        cum_vol = 0.0
        for b in bars:
            tp = (b.high + b.low + b.close) / 3.0
            cum_pv += tp * b.volume
            cum_vol += b.volume
        return cum_pv / cum_vol if cum_vol > 0 else 0.0

    def _score_intraday_long(self, bars: List, bd: list) -> tuple:
        if not bars or len(bars) < 5:
            bd.append(ScoreBreakdown("intraday", "insufficient_bars", 0, 40, f"Only {len(bars) if bars else 0} intraday bars"))
            return 0.0, 0.0

        score = 0.0
        vwap = self._compute_vwap(bars)
        ltp = bars[-1].close

        # 1. Above VWAP (+10)
        if vwap > 0:
            if ltp > vwap:
                pts = 10.0
            elif ltp > vwap * 0.998:  # within 0.2% is okay
                pts = 5.0
            else:
                pts = 0.0
            score += pts
            bd.append(ScoreBreakdown("intraday", "above_vwap", pts, 10, f"LTP={ltp:.2f}, VWAP={vwap:.2f}"))

        # 2. Opening Range Breakout (+10)
        # OR = first 15 min (first ~15 bars of 1m data)
        or_bars = bars[:15] if len(bars) >= 15 else bars[:len(bars)//2]
        if or_bars:
            or_high = max(b.high for b in or_bars)
            or_low = min(b.low for b in or_bars)
            if ltp > or_high:
                pts = 10.0
            elif ltp > (or_high + or_low) / 2:
                pts = 5.0
            else:
                pts = 0.0
            score += pts
            bd.append(ScoreBreakdown("intraday", "orb_breakout", pts, 10, f"ORHigh={or_high:.2f}, LTP={ltp:.2f}"))

        # 3. Volume spike vs time-of-day average (+10)
        recent_vols = [b.volume for b in bars[-5:]]
        earlier_vols = [b.volume for b in bars[:-5]] if len(bars) > 10 else [b.volume for b in bars]
        avg_vol = np.mean(earlier_vols) if earlier_vols else 0
        cur_vol = np.mean(recent_vols)
        if avg_vol > 0:
            vol_ratio = cur_vol / avg_vol
            if vol_ratio >= 2.0:
                pts = 10.0
            elif vol_ratio >= 1.5:
                pts = 7.0
            elif vol_ratio >= 1.2:
                pts = 4.0
            else:
                pts = 0.0
            score += pts
            bd.append(ScoreBreakdown("intraday", "volume_spike", pts, 10, f"VolRatio={vol_ratio:.2f}x"))

        # 4. Holding above intraday support (+10)
        # Support = lowest low of last 20 bars. If LTP is well above it, we're strong.
        lookback = bars[-20:] if len(bars) >= 20 else bars
        intra_low = min(b.low for b in lookback)
        intra_high = max(b.high for b in lookback)
        rng = intra_high - intra_low
        if rng > 0:
            pos_in_range = (ltp - intra_low) / rng  # 0=at low, 1=at high
            if pos_in_range >= 0.75:
                pts = 10.0
            elif pos_in_range >= 0.5:
                pts = 6.0
            elif pos_in_range >= 0.3:
                pts = 3.0
            else:
                pts = 0.0
            score += pts
            bd.append(ScoreBreakdown("intraday", "position_in_range", pts, 10, f"PosInRange={pos_in_range:.2f}"))

        return min(score, 40.0), vwap

    def _score_intraday_short(self, bars: List, bd: list) -> tuple:
        """Mirror for SHORT candidates."""
        if not bars or len(bars) < 5:
            bd.append(ScoreBreakdown("intraday", "insufficient_bars", 0, 40, f"Only {len(bars) if bars else 0} bars"))
            return 0.0, 0.0

        score = 0.0
        vwap = self._compute_vwap(bars)
        ltp = bars[-1].close

        # 1. Below VWAP (+10)
        if vwap > 0:
            pts = 10.0 if ltp < vwap else (5.0 if ltp < vwap * 1.002 else 0.0)
            score += pts
            bd.append(ScoreBreakdown("intraday", "below_vwap", pts, 10, f"LTP={ltp:.2f}, VWAP={vwap:.2f}"))

        # 2. Opening Range Breakdown (+10)
        or_bars = bars[:15] if len(bars) >= 15 else bars[:len(bars)//2]
        if or_bars:
            or_low = min(b.low for b in or_bars)
            pts = 10.0 if ltp < or_low else 0.0
            score += pts
            bd.append(ScoreBreakdown("intraday", "orb_breakdown", pts, 10, f"ORLow={or_low:.2f}, LTP={ltp:.2f}"))

        # 3. Volume spike (+10)
        recent_vols = [b.volume for b in bars[-5:]]
        earlier_vols = [b.volume for b in bars[:-5]] if len(bars) > 10 else [b.volume for b in bars]
        avg_vol = np.mean(earlier_vols) if earlier_vols else 0
        cur_vol = np.mean(recent_vols)
        if avg_vol > 0:
            vol_ratio = cur_vol / avg_vol
            pts = 10.0 if vol_ratio >= 2.0 else (7.0 if vol_ratio >= 1.5 else 0.0)
            score += pts
            bd.append(ScoreBreakdown("intraday", "volume_spike", pts, 10, f"VolRatio={vol_ratio:.2f}x"))

        # 4. At bottom of intraday range (+10)
        lookback = bars[-20:] if len(bars) >= 20 else bars
        intra_low = min(b.low for b in lookback)
        intra_high = max(b.high for b in lookback)
        rng = intra_high - intra_low
        if rng > 0:
            pos = (ltp - intra_low) / rng
            pts = 10.0 if pos <= 0.25 else (6.0 if pos <= 0.4 else 0.0)
            score += pts
            bd.append(ScoreBreakdown("intraday", "position_in_range", pts, 10, f"PosInRange={pos:.2f}"))

        return min(score, 40.0), vwap

    # ──────────────────────────────────────────────────────────────────────
    # MOMENTUM QUALITY (max 30 pts)
    # ──────────────────────────────────────────────────────────────────────

    def _score_momentum_long(self, daily_df, bars, nifty_pct, bd) -> float:
        score = 0.0

        # 1. Relative strength vs Nifty today (+10)
        if bars and len(bars) >= 2:
            stock_open = bars[0].open
            stock_ltp = bars[-1].close
            stock_pct = ((stock_ltp - stock_open) / stock_open * 100) if stock_open > 0 else 0
            rel_str = stock_pct - nifty_pct
            if rel_str >= 1.5:
                pts = 10.0
            elif rel_str >= 0.5:
                pts = 7.0
            elif rel_str >= 0.0:
                pts = 3.0
            else:
                pts = 0.0
            score += pts
            bd.append(ScoreBreakdown("momentum", "relative_strength", pts, 10,
                                     f"Stock={stock_pct:.2f}%, Nifty={nifty_pct:.2f}%, RelStr={rel_str:.2f}%"))

        # 2. Consecutive green candles in last 4 bars (+5)
        if bars and len(bars) >= 4:
            greens = sum(1 for b in bars[-4:] if b.close > b.open)
            pts = 5.0 if greens >= 3 else (3.0 if greens >= 2 else 0.0)
            score += pts
            bd.append(ScoreBreakdown("momentum", "green_candles", pts, 5, f"{greens}/4 green candles"))

        # 3. Volume increasing (each of last 3 bars > previous) (+5)
        if bars and len(bars) >= 4:
            vols = [b.volume for b in bars[-4:]]
            increasing = all(vols[i] > vols[i-1] for i in range(1, len(vols)))
            pts = 5.0 if increasing else 0.0
            score += pts
            bd.append(ScoreBreakdown("momentum", "volume_increasing", pts, 5,
                                     f"Vols={[int(v) for v in vols]}"))

        # 4. ATR expansion — volatility increasing (+5)
        if bars and len(bars) >= 15:
            recent_ranges = [b.high - b.low for b in bars[-5:]]
            older_ranges = [b.high - b.low for b in bars[-15:-5]]
            if older_ranges:
                avg_recent = np.mean(recent_ranges)
                avg_older = np.mean(older_ranges)
                if avg_older > 0 and avg_recent / avg_older >= 1.3:
                    pts = 5.0
                elif avg_older > 0 and avg_recent / avg_older >= 1.1:
                    pts = 2.0
                else:
                    pts = 0.0
                score += pts
                bd.append(ScoreBreakdown("momentum", "atr_expansion", pts, 5,
                                         f"RecentATR={avg_recent:.2f}, OlderATR={avg_older:.2f}"))

        # 5. Distance to resistance — more room to run (+5)
        if daily_df is not None and not daily_df.empty and len(daily_df) >= 20:
            high_20d = float(daily_df["high"].tail(20).max())
            c = float(daily_df["close"].iloc[-1])
            dist_pct = ((high_20d - c) / c * 100) if c > 0 else 0
            if dist_pct >= 3.0:
                pts = 5.0  # lots of room
            elif dist_pct >= 1.5:
                pts = 3.0
            elif dist_pct >= 0:
                pts = 1.0  # at or near resistance, limited room
            else:
                pts = 5.0  # already broken through recent highs — strong
            score += pts
            bd.append(ScoreBreakdown("momentum", "room_to_resistance", pts, 5, f"Dist={dist_pct:.1f}%"))

        return min(score, 30.0)

    def _score_momentum_short(self, daily_df, bars, nifty_pct, bd) -> float:
        """Mirror momentum scoring for SHORT."""
        score = 0.0

        # 1. Relative weakness vs Nifty (+10)
        if bars and len(bars) >= 2:
            stock_open = bars[0].open
            stock_ltp = bars[-1].close
            stock_pct = ((stock_ltp - stock_open) / stock_open * 100) if stock_open > 0 else 0
            rel_str = stock_pct - nifty_pct
            if rel_str <= -1.5:
                pts = 10.0
            elif rel_str <= -0.5:
                pts = 7.0
            elif rel_str <= 0.0:
                pts = 3.0
            else:
                pts = 0.0
            score += pts
            bd.append(ScoreBreakdown("momentum", "relative_weakness", pts, 10,
                                     f"Stock={stock_pct:.2f}%, RelStr={rel_str:.2f}%"))

        # 2. Consecutive red candles (+5)
        if bars and len(bars) >= 4:
            reds = sum(1 for b in bars[-4:] if b.close < b.open)
            pts = 5.0 if reds >= 3 else (3.0 if reds >= 2 else 0.0)
            score += pts
            bd.append(ScoreBreakdown("momentum", "red_candles", pts, 5, f"{reds}/4 red candles"))

        # 3. Volume increasing on down bars (+5)
        if bars and len(bars) >= 4:
            vols = [b.volume for b in bars[-4:]]
            increasing = all(vols[i] > vols[i-1] for i in range(1, len(vols)))
            pts = 5.0 if increasing else 0.0
            score += pts
            bd.append(ScoreBreakdown("momentum", "vol_increasing_down", pts, 5, f"Vols={[int(v) for v in vols]}"))

        # 4. ATR expansion (+5)
        if bars and len(bars) >= 15:
            recent = np.mean([b.high - b.low for b in bars[-5:]])
            older = np.mean([b.high - b.low for b in bars[-15:-5]])
            pts = 5.0 if (older > 0 and recent / older >= 1.3) else 0.0
            score += pts
            bd.append(ScoreBreakdown("momentum", "atr_expansion", pts, 5, f"Ratio={recent/older:.2f}" if older > 0 else "N/A"))

        # 5. Distance to support (+5)
        if daily_df is not None and not daily_df.empty and len(daily_df) >= 20:
            low_20d = float(daily_df["low"].tail(20).min())
            c = float(daily_df["close"].iloc[-1])
            dist_pct = ((c - low_20d) / c * 100) if c > 0 else 0
            pts = 5.0 if dist_pct >= 3.0 else (3.0 if dist_pct >= 1.5 else 1.0)
            score += pts
            bd.append(ScoreBreakdown("momentum", "room_to_support", pts, 5, f"Dist={dist_pct:.1f}%"))

        return min(score, 30.0)


# ── Entry threshold helper ─────────────────────────────────────────────────

def meets_entry_threshold(
    score: TechScore,
    market_trend: str = "sideways",
) -> bool:
    """
    Check if a score meets entry threshold given market regime.
    
    LONG thresholds:   bullish=55, sideways=65, bearish=75
    SHORT thresholds:  bearish=55, sideways=65, bullish=never
    """
    if score.direction == "LONG":
        thresholds = {"bullish": 55, "sideways": 65, "bearish": 75}
        return score.total >= thresholds.get(market_trend, 65)
    elif score.direction == "SHORT":
        if market_trend == "bullish":
            return False  # don't short in a bull market
        thresholds = {"bearish": 55, "sideways": 65}
        return score.total >= thresholds.get(market_trend, 65)
    return False
