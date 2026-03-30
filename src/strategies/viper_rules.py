"""
viper_rules.py — STRIKE & COIL Technical Confirmation Rules
-------------------------------------------------------------
VIPER-specific TA rules that use the shared TechnicalBody snapshot.

STRIKE rules (momentum continuation):
  - Needs trend alignment (EMA, VWAP, ADX)
  - Needs volume confirmation
  - Needs pullback to entry zone (not chasing)

COIL rules (mean reversion, DRY-RUN only):
  - Needs exhaustion signals (RSI extreme, volume decline)
  - Needs pattern confirmation (MACD divergence, double top/bottom)
  - Needs time filter (only after 11 AM)
"""
import logging
from typing import Tuple
from src.strategies.technical_body import TechnicalSnapshot

logger = logging.getLogger(__name__)
class ViperRules:
    """
    Computes TA confirmation scores for VIPER strategies.

    STRIKE max score: 25
    COIL max score: 25
    """

    # ── Strike component max caps (raw points before weighting) ────────────
    _MAX_VOLUME = 8.0
    _MAX_VWAP   = 5.0
    _MAX_EMA    = 5.0
    _MAX_ADX    = 4.0
    _MAX_ORB    = 3.0

    @staticmethod
    def _get_regime_weights(snapshot: "TechnicalSnapshot", direction: str) -> dict:
        """
        Classify intraday regime and return per-component weight multipliers.
        Identical logic to HydraRules._get_regime_weights — kept local to avoid
        circular import.
        """
        adx = snapshot.adx
        rsi = snapshot.rsi14
        vol = snapshot.volume_spike_ratio
        di_aligned = (
            snapshot.plus_di > snapshot.minus_di if direction == "BUY"
            else snapshot.minus_di > snapshot.plus_di
        )
        if adx >= 25 and di_aligned:
            regime = "TRENDING"
        elif vol >= 2.0 and adx >= 18:
            regime = "BREAKOUT"
        elif adx < 20:
            regime = "RANGING"
        elif (direction == "BUY" and rsi > 78) or (direction == "SHORT" and rsi < 22):
            regime = "EXHAUSTION"
        else:
            regime = "NORMAL"
        table = {
            "TRENDING":   {"volume": 1.0, "vwap": 1.0, "ema": 1.3, "adx": 1.5, "orb": 1.0},
            "BREAKOUT":   {"volume": 1.5, "vwap": 0.8, "ema": 1.0, "adx": 1.2, "orb": 1.4},
            "RANGING":    {"volume": 0.8, "vwap": 1.3, "ema": 0.7, "adx": 0.5, "orb": 0.6},
            "EXHAUSTION": {"volume": 0.7, "vwap": 1.2, "ema": 0.8, "adx": 0.6, "orb": 0.5},
            "NORMAL":     {"volume": 1.0, "vwap": 1.0, "ema": 1.0, "adx": 1.0, "orb": 1.0},
        }
        w = dict(table[regime])
        w["regime"] = regime
        return w

    # ── STRIKE: Momentum Continuation ─────────────────────────

    def strike_confirms(
        self,
        snapshot: "TechnicalSnapshot",
        direction: str,
        gap_pct: float = 0.0,
    ) -> Tuple[float, str]:
        """
        Check if technicals confirm a STRIKE (momentum continuation) trade.
        Applies regime-aware weighting + Bollinger Band + RSI acceleration scoring.

        Returns:
            (score 0-25, comma-separated reasons)
        """
        score = 0.0
        reasons = []
        w = self._get_regime_weights(snapshot, direction)
        reasons.append(f"Regime={w['regime']}")

        if direction == "BUY":
            # ── 1. Volume spike (max 8) ──────────────────────────────
            raw_vol = 0.0
            if snapshot.volume_spike_ratio >= 2.5:
                raw_vol = 8.0
                reasons.append(f"Strong volume spike {snapshot.volume_spike_ratio:.1f}x")
            elif snapshot.volume_spike_ratio >= 1.8:
                raw_vol = 5.0
                reasons.append(f"Volume spike {snapshot.volume_spike_ratio:.1f}x")
            elif snapshot.volume_spike_ratio >= 1.3:
                raw_vol = 2.0
                reasons.append(f"Modest volume {snapshot.volume_spike_ratio:.1f}x")
            score += min(raw_vol * w["volume"], self._MAX_VOLUME)

            # ── 2. VWAP support (max 5) ─────────────────────────────
            raw_vwap = 0.0
            if snapshot.above_vwap and snapshot.last_price > 0 and snapshot.vwap > 0:
                dist = (snapshot.last_price - snapshot.vwap) / snapshot.vwap * 100
                if dist <= 0.5:
                    raw_vwap = 5.0
                    reasons.append(f"At VWAP pullback ({dist:.1f}% above)")
                elif dist <= 1.5:
                    raw_vwap = 3.0
                    reasons.append(f"Near VWAP ({dist:.1f}% above)")
                else:
                    raw_vwap = 1.0
                    reasons.append(f"Above VWAP, extended ({dist:.1f}%)")
            elif not snapshot.above_vwap:
                reasons.append("Below VWAP (weak for BUY)")
            score += min(raw_vwap * w["vwap"], self._MAX_VWAP)

            # ── 3. EMA alignment (max 5) ─────────────────────────────
            raw_ema = 0.0
            if snapshot.ema9 > snapshot.ema20:
                raw_ema = 5.0
                reasons.append("EMA 9>20 (bullish alignment)")
            elif snapshot.ema9 > 0 and snapshot.ema20 > 0:
                reasons.append("EMA 9<20 (no trend alignment)")
            score += min(raw_ema * w["ema"], self._MAX_EMA)

            # ── 4. ADX trend strength (max 4) ────────────────────────
            raw_adx = 0.0
            if snapshot.adx >= 30:
                raw_adx = 4.0
                reasons.append(f"Strong trend ADX={snapshot.adx:.0f}")
            elif snapshot.adx >= 25:
                raw_adx = 2.0
                reasons.append(f"Trending ADX={snapshot.adx:.0f}")
            elif snapshot.adx < 20:
                reasons.append(f"Weak trend ADX={snapshot.adx:.0f} — regime penalty applied")
            score += min(raw_adx * w["adx"], self._MAX_ADX)

            # ── 5. ORB breakout (max 3) ──────────────────────────────
            raw_orb = 0.0
            if snapshot.orb_breakout:
                raw_orb = 3.0
                reasons.append("ORB breakout confirmed")
            score += min(raw_orb * w["orb"], self._MAX_ORB)

            # ── 6. RSI momentum regime (max 4) ────────────────────────
            # Research: RSI 70-85 = embedded institutional buying (NOT overbought).
            # RSI crossing 70 with velocity is the acceleration signal for STRIKE.
            rsi = snapshot.rsi14
            if 70 <= rsi <= 85:
                score += 4
                reasons.append(f"RSI embedded momentum ({rsi:.0f}) — institutional buying")
            elif 60 <= rsi < 70:
                score += 2
                reasons.append(f"RSI building momentum ({rsi:.0f})")
            elif 50 <= rsi < 60:
                score += 1
                reasons.append(f"RSI neutral-positive ({rsi:.0f})")
            elif rsi > 85:
                score += 1
                reasons.append(f"RSI parabolic ({rsi:.0f}) — tighten stop")
            # RSI < 50: no bonus (momentum not confirmed for a BUY strike)

            # ── 7. Bollinger Band (max 4) ─────────────────────────────
            # Guard: bb_upper is 0.0 until 20 bars are available
            if snapshot.bb_upper > 0:
                if snapshot.last_price > snapshot.bb_upper and snapshot.volume_spike_ratio >= 2.0:
                    score += 4
                    reasons.append(
                        f"BB breakout+vol ({snapshot.last_price:.2f}>{snapshot.bb_upper:.2f})"
                    )
                elif snapshot.last_price > snapshot.bb_upper:
                    score += 2
                    reasons.append("BB upper break (low volume — partial)")
                elif snapshot.bb_squeeze:
                    score += 2
                    reasons.append(f"BB squeeze (w={snapshot.bb_width:.3f}) — coiled spring")

            # ── 8. OBV Divergence (Bonus max 3) ──────────────────────
            if getattr(snapshot, "obv_bullish_div", False):
                score += 3.0
                reasons.append("OBV accumulation divergence (hidden buying)")

        elif direction == "SHORT":
            # ── 1. Volume (max 8) ─────────────────────────────────
            raw_vol = 0.0
            if snapshot.volume_spike_ratio >= 2.5:
                raw_vol = 8.0
                reasons.append(f"Panic selling {snapshot.volume_spike_ratio:.1f}x volume")
            elif snapshot.volume_spike_ratio >= 1.8:
                raw_vol = 5.0
                reasons.append(f"Heavy selling volume {snapshot.volume_spike_ratio:.1f}x")
            elif snapshot.volume_spike_ratio >= 1.3:
                raw_vol = 2.0
                reasons.append(f"Selling pressure {snapshot.volume_spike_ratio:.1f}x")
            score += min(raw_vol * w["volume"], self._MAX_VOLUME)

            # ── 2. VWAP rejection (max 5) ────────────────────────────
            raw_vwap = 0.0
            if not snapshot.above_vwap and snapshot.last_price > 0 and snapshot.vwap > 0:
                dist_below = (snapshot.vwap - snapshot.last_price) / snapshot.vwap * 100
                if dist_below <= 0.5:
                    raw_vwap = 5.0
                    reasons.append(f"At VWAP rejection ({dist_below:.1f}% below)")
                elif dist_below <= 1.5:
                    raw_vwap = 3.0
                    reasons.append(f"Near VWAP ({dist_below:.1f}% below)")
                else:
                    raw_vwap = 1.0
                    reasons.append(f"Below VWAP, extended ({dist_below:.1f}%)")
            score += min(raw_vwap * w["vwap"], self._MAX_VWAP)

            # ── 3. EMA alignment (max 5) ────────────────────────────
            raw_ema = 0.0
            if snapshot.ema9 < snapshot.ema20:
                raw_ema = 5.0
                reasons.append("EMA 9<20 (bearish alignment)")
            score += min(raw_ema * w["ema"], self._MAX_EMA)

            # ── 4. ADX (max 4) ─────────────────────────────────────
            raw_adx = 0.0
            if snapshot.adx >= 30:
                raw_adx = 4.0
                reasons.append(f"Strong downtrend ADX={snapshot.adx:.0f}")
            elif snapshot.adx >= 25:
                raw_adx = 2.0
                reasons.append(f"Trending down ADX={snapshot.adx:.0f}")
            elif snapshot.adx < 20:
                reasons.append(f"Weak trend ADX={snapshot.adx:.0f} — regime penalty applied")
            score += min(raw_adx * w["adx"], self._MAX_ADX)

            # ── 5. ORB breakdown (max 3) ────────────────────────────
            raw_orb = 0.0
            if getattr(snapshot, "orb_breakdown", False):
                raw_orb = 3.0
                reasons.append("ORB breakdown confirmed")
            score += min(raw_orb * w["orb"], self._MAX_ORB)

            # ── 6. RSI embedded bearish momentum (max 4) ──────────────
            rsi = snapshot.rsi14
            if 15 <= rsi <= 30:
                score += 4
                reasons.append(f"RSI embedded bearish ({rsi:.0f}) — institutional selling")
            elif 30 < rsi <= 40:
                score += 2
                reasons.append(f"RSI building bearish ({rsi:.0f})")
            elif 40 < rsi <= 50:
                score += 1
                reasons.append(f"RSI neutral-bearish ({rsi:.0f})")
            elif rsi < 15:
                score += 1
                reasons.append(f"RSI climactic bearish ({rsi:.0f}) — tighten stop")

            # ── 7. Bollinger Band (max 4) ────────────────────────────
            if snapshot.bb_lower > 0:
                if snapshot.last_price < snapshot.bb_lower and snapshot.volume_spike_ratio >= 2.0:
                    score += 4
                    reasons.append(
                        f"BB breakdown+vol ({snapshot.last_price:.2f}<{snapshot.bb_lower:.2f})"
                    )
                elif snapshot.last_price < snapshot.bb_lower:
                    score += 2
                    reasons.append("BB lower break (low volume)")
                elif snapshot.bb_squeeze:
                    score += 2
                    reasons.append(f"BB squeeze (w={snapshot.bb_width:.3f}) — downside coiling")

            # ── 8. OBV Divergence (Bonus max 3) ──────────────────────
            if getattr(snapshot, "obv_bearish_div", False):
                score += 3.0
                reasons.append("OBV distribution divergence (hidden selling)")

        # ── Regime Hard Penalty ──────────────────────────────────────
        if w["regime"] == "RANGING":
            score -= 5.0
            reasons.append("Hard penalty (-5): ADX < 20 in RANGING regime")

        score = min(score, 25.0)
        reason_str = "; ".join(reasons) if reasons else "No TA confirmation"
        return score, reason_str

    # ── COIL: Mean Reversion (DRY-RUN) ────────────────────

    def coil_confirms(
        self,
        snapshot: TechnicalSnapshot,
        direction: str,
        pct_change: float = 0.0,
    ) -> Tuple[float, str]:
        """
        Check if technicals confirm a COIL (mean reversion) trade.
        Reversal trades = counter-trend. Needs HIGHER bar.

        Entry direction is the REVERSAL direction:
          - Stock overbought → direction="SHORT" (fade the up move)
          - Stock oversold → direction="BUY" (fade the down move)

        Returns:
            (score 0-25, comma-separated reasons)
        """
        score = 0.0
        reasons = []

        # 1. RSI extreme (max 8)
        if direction == "SHORT":  # Fading overbought
            if snapshot.rsi14 >= 85:
                score += 8
                reasons.append(f"RSI extremely overbought {snapshot.rsi14:.0f}")
            elif snapshot.rsi14 >= 75:
                score += 5
                reasons.append(f"RSI overbought {snapshot.rsi14:.0f}")
            elif snapshot.rsi14 >= 70:
                score += 2
                reasons.append(f"RSI elevated {snapshot.rsi14:.0f}")
        elif direction == "BUY":  # Fading oversold
            if snapshot.rsi14 <= 15:
                score += 8
                reasons.append(f"RSI extremely oversold {snapshot.rsi14:.0f}")
            elif snapshot.rsi14 <= 25:
                score += 5
                reasons.append(f"RSI oversold {snapshot.rsi14:.0f}")
            elif snapshot.rsi14 <= 30:
                score += 2
                reasons.append(f"RSI depressed {snapshot.rsi14:.0f}")

        # 2. Extension from VWAP (max 5)
        if snapshot.last_price > 0 and snapshot.vwap > 0:
            vwap_dist_pct = abs(snapshot.last_price - snapshot.vwap) / snapshot.vwap * 100
            if vwap_dist_pct >= 3.0:
                score += 5
                reasons.append(f"Far from VWAP ({vwap_dist_pct:.1f}%) → reversion likely")
            elif vwap_dist_pct >= 2.0:
                score += 3
                reasons.append(f"Extended from VWAP ({vwap_dist_pct:.1f}%)")
            elif vwap_dist_pct >= 1.5:
                score += 1
                reasons.append(f"Moderately extended ({vwap_dist_pct:.1f}%)")

        # 3. Volume declining (max 5) — momentum fading
        # Volume spike ratio < 1.0 means current volume is BELOW average = exhaustion
        # ⚠️ MIDDAY GATE: Between 11:30–13:30 IST, volume naturally drops 40-60%
        # on NSE. Require a MUCH lower threshold during lunch to avoid false signals.
        try:
            import zoneinfo
            _IST = zoneinfo.ZoneInfo("Asia/Kolkata")
            from datetime import datetime as _dt
            _hour_decimal = _dt.now(_IST).hour + _dt.now(_IST).minute / 60.0
        except Exception:
            _hour_decimal = 12.0  # Default to midday if timezone fails

        is_lunch_window = 11.5 <= _hour_decimal <= 13.5

        if is_lunch_window:
            # During lunch: only award exhaust points for truly dead volume
            if snapshot.volume_spike_ratio < 0.3:
                score += 3  # Reduced from 5 — still some credit for extreme dryness
                reasons.append(f"Volume dried up even for lunch ({snapshot.volume_spike_ratio:.1f}x)")
            elif snapshot.volume_spike_ratio < 0.5:
                score += 1
                reasons.append(f"Low volume during lunch ({snapshot.volume_spike_ratio:.1f}x) — inconclusive")
        else:
            # Normal hours — original thresholds
            if snapshot.volume_spike_ratio < 0.7:
                score += 5
                reasons.append(f"Volume dried up ({snapshot.volume_spike_ratio:.1f}x avg)")
            elif snapshot.volume_spike_ratio < 1.0:
                score += 3
                reasons.append(f"Volume declining ({snapshot.volume_spike_ratio:.1f}x avg)")
            elif snapshot.volume_spike_ratio < 1.3:
                score += 1
                reasons.append(f"Volume easing ({snapshot.volume_spike_ratio:.1f}x)")

        # 4. MACD histogram divergence (max 4)
        macd_hist = getattr(snapshot, 'macd_histogram', None)
        macd_hist_prev = getattr(snapshot, 'macd_histogram_prev', None)
        if macd_hist is not None and macd_hist_prev is not None:
            if direction == "SHORT" and macd_hist < macd_hist_prev:
                score += 4
                reasons.append("MACD histogram declining (momentum fading)")
            elif direction == "BUY" and macd_hist > macd_hist_prev:
                score += 4
                reasons.append("MACD histogram rising (selling exhausted)")

        # 5. Extension magnitude (max 3) — bigger move = more reversion potential
        abs_change = abs(pct_change)
        if abs_change >= 8:
            score += 3
            reasons.append(f"Massive {pct_change:+.1f}% move — high reversion potential")
        elif abs_change >= 5:
            score += 2
            reasons.append(f"Extended {pct_change:+.1f}% move")

        score = min(score, 25.0)
        reason_str = "; ".join(reasons) if reasons else "No COIL confirmation"
        return score, reason_str
