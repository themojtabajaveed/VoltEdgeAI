"""
hydra.py — 🔥 HYDRA: Event-Driven Catalyst Strategy
-----------------------------------------------------
Head 1 of the VoltEdge Dragon Architecture.

Flow:
  08:15 IST — Pre-market scan: fetch events since 15:30 yesterday,
              classify via Groq, rank top 5.
  09:15 IST — Market opens. Monitor watchlist stocks every 2 minutes.
  When TA confirms + base conviction ≥ threshold → candidate for Grok orchestrator.
  Live events with urgency ≥ 7 get fast-tracked into watchlist.

Grok Integration (v2):
  HYDRA does NOT call Grok directly. It produces a base conviction score
  (event + TA + depth, max ~102 before capping). The runner's Grok
  Portfolio Orchestrator reviews top candidates from BOTH HYDRA and VIPER
  at key intraday milestones (09:17, 09:30, 10:00, 10:45, 11:45).

HYDRA doesn't chase spikes. It waits for VWAP retest after event.
"""
import os
import json
import pathlib
import time
import logging
from datetime import datetime, date
from typing import Optional, List
from dataclasses import asdict

# Import for type guard
try:
    from src.trading.depth_analyzer import DepthAnalysis
except ImportError:
    DepthAnalysis = None

from src.strategies.base import StrategyHead, ConvictionScore, WatchlistEntry
from src.strategies.technical_body import TechnicalBody, TechnicalSnapshot
from src.data_ingestion.event_scanner import EventScanner, MarketEvent
from src.data_ingestion.pre_market_data import (
    PreMarketSignals,
    fetch_all_premarket_data,
    get_scan_universe,
)
from src.event_intelligence.hydra_bridge import get_event_intel_signals

logger = logging.getLogger(__name__)


def _volume_signal_from_rel(rel_volume: float) -> str:
    if rel_volume >= 2.0:
        return "SURGE"
    if rel_volume >= 1.5:
        return "ELEVATED"
    return "NORMAL"


def _technical_setup_from_cache(cache_setup: str) -> str:
    if cache_setup == "BREAKOUT_READY":
        return "BREAKOUT"
    if cache_setup == "PULLBACK":
        return "SUPPORT"
    return "NEUTRAL"


def _sector_momentum_from_cache(cache_momentum: str, fallback: str) -> str:
    if cache_momentum in ("HOT", "WARM"):
        return "BULLISH"
    if cache_momentum == "COLD":
        return "BEARISH"
    return fallback


def _fetch_gap_pct(symbol: str) -> float:
    """
    Fetch estimated gap % vs previous close using yfinance.
    Returns 0.0 on any failure — never raises.
    Pre-market price via fast_info["last_price"] or regularMarketPrice.
    """
    try:
        import yfinance as yf
        nse_symbol = f"{symbol}.NS"
        ticker = yf.Ticker(nse_symbol)
        fi = ticker.fast_info

        # Get current/pre-market price
        current = fi["last_price"]
        if not current:
            current = getattr(fi, "last_price", None)

        # Get previous close
        prev_close = fi["previous_close"]
        if not prev_close:
            prev_close = getattr(fi, "previous_close", None)

        if current and prev_close and prev_close > 0:
            gap = ((current - prev_close) / prev_close) * 100
            logger.debug(f"[HYDRA] {symbol} gap_pct={gap:.2f}% "
                         f"(current={current}, prev={prev_close})")
            return round(gap, 2)
    except Exception as e:
        logger.debug(f"[HYDRA] gap_pct fetch failed for {symbol}: {e}")
    return 0.0


class HydraRules:
    """
    HYDRA-specific TA interpretation.
    Events override some standard TA rules.
    """

    # ── Component max caps (raw points before weighting) ────────────────────
    _MAX_VOLUME = 8.0
    _MAX_VWAP   = 5.0
    _MAX_EMA    = 4.0
    _MAX_ORB    = 5.0

    @staticmethod
    def _get_regime_weights(snapshot: TechnicalSnapshot, direction: str) -> dict:
        """
        Return per-component weight multipliers based on current intraday regime.
        Weights shift emphasis within each component's max — they never raise the cap.

        Regimes:
          TRENDING   — strong directional trend confirmed by ADX + DI alignment
          BREAKOUT   — high volume surge with nascent trend (ADX still low)
          RANGING    — ADX weak, choppy price action
          EXHAUSTION — RSI in extreme zone suggesting price overreach
          NORMAL     — default; all multipliers 1.0
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
            "TRENDING":   {"volume": 1.0, "vwap": 1.0, "ema": 1.3, "orb": 1.0},
            "BREAKOUT":   {"volume": 1.5, "vwap": 0.8, "ema": 1.0, "orb": 1.4},
            "RANGING":    {"volume": 0.8, "vwap": 1.3, "ema": 0.7, "orb": 0.6},
            "EXHAUSTION": {"volume": 0.7, "vwap": 1.2, "ema": 0.8, "orb": 0.5},
            "NORMAL":     {"volume": 1.0, "vwap": 1.0, "ema": 1.0, "orb": 1.0},
        }
        w = dict(table[regime])
        w["regime"] = regime
        return w

    @staticmethod
    def confirms_event(snapshot: TechnicalSnapshot, direction: str) -> tuple[float, str]:
        """
        Check if technicals confirm the event catalyst.
        Applies context-aware regime weighting to each sub-score.

        Returns:
            (score: 0-22, reasoning: str)
        """
        score = 0.0
        reasons = []
        w = HydraRules._get_regime_weights(snapshot, direction)
        reasons.append(f"Regime={w['regime']}")

        if direction == "BUY":
            # ── Volume (max 8) ──────────────────────────────────────────
            raw_vol = 0.0
            if snapshot.volume_spike_ratio >= 2.0:
                raw_vol = 8.0
                reasons.append(f"Volume spike {snapshot.volume_spike_ratio:.1f}x (reacting)")
            elif snapshot.volume_spike_ratio >= 1.5:
                raw_vol = 4.0
                reasons.append(f"Moderate volume {snapshot.volume_spike_ratio:.1f}x")
            score += min(raw_vol * w["volume"], HydraRules._MAX_VOLUME)

            # ── VWAP proximity (max 5) ──────────────────────────────────
            raw_vwap = 0.0
            if snapshot.above_vwap and snapshot.last_price > 0 and snapshot.vwap > 0:
                vwap_dist = (snapshot.last_price - snapshot.vwap) / snapshot.vwap * 100
                if vwap_dist <= 0.5:
                    raw_vwap = 5.0
                    reasons.append(f"Near VWAP ({vwap_dist:.1f}%) — pullback entry")
                elif vwap_dist <= 1.0:
                    raw_vwap = 3.0
                    reasons.append(f"Moderately above VWAP ({vwap_dist:.1f}%)")
                else:
                    reasons.append(f"Extended from VWAP ({vwap_dist:.1f}%) — spike top risk")
            elif not snapshot.above_vwap:
                reasons.append("Below VWAP — weak for bullish event")
            score += min(raw_vwap * w["vwap"], HydraRules._MAX_VWAP)

            # ── EMA alignment (max 4) ────────────────────────────────────
            raw_ema = 0.0
            if snapshot.ema9 > snapshot.ema20:
                raw_ema = 4.0
                reasons.append("EMA 9>20 (bullish alignment)")
            score += min(raw_ema * w["ema"], HydraRules._MAX_EMA)

            # ── ORB breakout (max 5) ───────────────────────────────────
            raw_orb = 0.0
            if snapshot.orb_breakout:
                raw_orb = 5.0
                reasons.append("ORB breakout (momentum confirmed)")
            score += min(raw_orb * w["orb"], HydraRules._MAX_ORB)

            # ── Bollinger Band bonus (max 3) ────────────────────────────
            # Guard: bb_upper is 0.0 until 20 bars are available
            if snapshot.bb_upper > 0:
                if snapshot.last_price > snapshot.bb_upper:
                    if snapshot.volume_spike_ratio >= 1.5:
                        score += 3
                        reasons.append(
                            f"Above BB upper ({snapshot.last_price:.2f}>{snapshot.bb_upper:.2f})+vol"
                        )
                    else:
                        score += 1
                        reasons.append("Above BB upper (low volume)")
                elif snapshot.bb_squeeze:
                    score += 1
                    reasons.append(f"BB squeeze (w={snapshot.bb_width:.3f}) — event may ignite")

            # Note: RSI is INTENTIONALLY IGNORED for events — overbought is expected after a catalyst

        elif direction == "SHORT":
            # ── Volume (max 8) ──────────────────────────────────────────
            raw_vol = 0.0
            if snapshot.volume_spike_ratio >= 2.0:
                raw_vol = 8.0
                reasons.append(f"Volume spike {snapshot.volume_spike_ratio:.1f}x (panic selling)")
            elif snapshot.volume_spike_ratio >= 1.5:
                raw_vol = 4.0
                reasons.append(f"Heavy selling volume {snapshot.volume_spike_ratio:.1f}x")
            score += min(raw_vol * w["volume"], HydraRules._MAX_VOLUME)

            # ── VWAP (max 5) ────────────────────────────────────────────
            raw_vwap = 0.0
            if not snapshot.above_vwap:
                raw_vwap = 5.0
                reasons.append("Below VWAP (sellers in control)")
            score += min(raw_vwap * w["vwap"], HydraRules._MAX_VWAP)

            # ── EMA (max 4) ──────────────────────────────────────────────
            raw_ema = 0.0
            if snapshot.ema9 < snapshot.ema20:
                raw_ema = 4.0
                reasons.append("EMA 9<20 (bearish alignment)")
            score += min(raw_ema * w["ema"], HydraRules._MAX_EMA)

            # ── ORB (max 5) ─────────────────────────────────────────────
            raw_orb = 0.0
            if snapshot.orb_breakdown:
                raw_orb = 5.0
                reasons.append("ORB breakdown (sellers confirmed)")
            score += min(raw_orb * w["orb"], HydraRules._MAX_ORB)

            # ── Bollinger Band bonus (max 3) ────────────────────────────
            if snapshot.bb_lower > 0:
                if snapshot.last_price < snapshot.bb_lower:
                    if snapshot.volume_spike_ratio >= 1.5:
                        score += 3
                        reasons.append(
                            f"Below BB lower ({snapshot.last_price:.2f}<{snapshot.bb_lower:.2f})+vol"
                        )
                    else:
                        score += 1
                        reasons.append("Below BB lower (low volume)")
                elif snapshot.bb_squeeze:
                    score += 1
                    reasons.append(f"BB squeeze (w={snapshot.bb_width:.3f}) — breakdown risk")

        # ── Regime Hard Penalty ──────────────────────────────────────
        if w["regime"] == "RANGING":
            score -= 5.0
            reasons.append("Hard penalty (-5): ADX < 20 in RANGING regime")

        reasoning = "; ".join(reasons) if reasons else "No TA confirmation"
        return min(score, 22.0), reasoning


class HydraStrategy(StrategyHead):
    """
    🔥 HYDRA — Event-Driven Catalyst Hunter.

    Scans corporate events, classifies urgency via Groq,
    confirms with TA, then produces base conviction scores.
    Grok integration is handled centrally by the runner's orchestrator.
    """

    def __init__(self):
        super().__init__(name="HYDRA", max_watchlist=5)
        self.event_scanner = EventScanner()
        self.technical_body = TechnicalBody()
        self.rules = HydraRules()
        self._pattern_db_path = "data/hydra_pattern_db.json"

    # ── Core Interface ────────────────────────────────────────

    def scan(self) -> List[WatchlistEntry]:
        """
        Scan for event-driven opportunities.
        
        Called at 09:00 IST for full scan since last close.
        Called every 2 minutes during market hours for new events.
        """
        # ── Priority path: event-intelligence pipeline signals ──────────
        # These come from deep PDF/XBRL parsing + Gemini classification.
        # They get priority slots; EventScanner fills remaining capacity.
        intel_entries = []
        try:
            intel_entries = get_event_intel_signals(data_dir="data")
            if intel_entries:
                logger.info(
                    "[HYDRA] event-intel bridge: %d signals (top: %s %.1f)",
                    len(intel_entries), intel_entries[0].symbol, intel_entries[0].urgency,
                )
        except Exception as _ei_err:
            logger.warning("[HYDRA] event-intel bridge failed: %s", _ei_err)

        # ── Fallback path: EventScanner headline classification ────────
        if self._last_scan_time is None:
            events = self.event_scanner.get_hot_events(min_urgency=6.0)
        else:
            new_events = self.event_scanner.scan_new_events()
            if new_events:
                classified = self.event_scanner.classify_events(new_events)
                events = [e for e in classified if e.urgency >= 7.0]
            else:
                events = []

        self._last_scan_time = datetime.now()

        # Exclude symbols already covered by intel bridge
        intel_syms = {e.symbol for e in intel_entries}
        events = [e for e in events if e.symbol not in intel_syms]

        # Read macro regime once (regime fallback for sector_momentum)
        _regime_momentum = "NEUTRAL"
        try:
            with open("data/daily_regime.json") as _rf:
                _regime = json.load(_rf)
            _trend = _regime.get("trend", "sideways")
            if _trend == "bullish":
                _regime_momentum = "BULLISH"
            elif _trend == "bearish":
                _regime_momentum = "BEARISH"
        except Exception:
            pass  # defaults to NEUTRAL

        # Load pre-market cache once — shared source for gap/volume/technical/sector
        premarket_cache: dict = {}
        try:
            event_syms = [e.symbol for e in events[:self.max_watchlist]]
            if event_syms:
                universe = list({*get_scan_universe(), *event_syms})
                premarket_cache = fetch_all_premarket_data(universe)
        except Exception as _e:
            logger.warning(f"[HYDRA] premarket cache load failed: {_e}")

        # Convert MarketEvents to WatchlistEntries
        entries = []
        for event in events[:self.max_watchlist]:
            urgency = event.urgency
            if urgency >= 8:
                _catalyst_strength = "HIGH"
            elif urgency >= 5:
                _catalyst_strength = "MEDIUM"
            else:
                _catalyst_strength = "LOW"

            cached: Optional[PreMarketSignals] = premarket_cache.get(event.symbol)
            if cached:
                gap_pct = cached.gap_pct
                volume_signal = _volume_signal_from_rel(cached.rel_volume)
                technical_setup = _technical_setup_from_cache(cached.technical_setup)
                sector_momentum = _sector_momentum_from_cache(
                    cached.sector_momentum, _regime_momentum
                )
            else:
                gap_pct = _fetch_gap_pct(event.symbol)
                volume_signal = "NORMAL"
                technical_setup = "NEUTRAL"
                sector_momentum = _regime_momentum
                logger.debug(
                    f"[HYDRA] {event.symbol} not in premarket cache — using yfinance fallback"
                )

            entries.append(WatchlistEntry(
                symbol=event.symbol,
                direction=event.direction,
                event_summary=event.summary or event.headline,
                urgency=urgency,
                catalyst_strength=_catalyst_strength,
                sector_momentum=sector_momentum,
                gap_pct=gap_pct,
                volume_signal=volume_signal,
                technical_setup=technical_setup,
                # 4-layer R4 inputs — prev_close doubles as price_at_filing for
                # pre-market filings (close is the last known level); deal_size
                # flows through from the regex parser in event_scanner.
                filing_subject=event.headline,
                price_at_filing_time=(cached.prev_close if cached and cached.prev_close > 0 else None),
                deal_size_inr=event.deal_size_inr,
                market_cap_inr=event.market_cap_inr,
                filed_at=(event.filed_at_dt.isoformat() if event.filed_at_dt else None),
                filing_category=(event.event_type or getattr(event, "category", "") or "").upper(),
                filing_urgency=float(getattr(event, "urgency", 0.0) or 0.0),
            ))

        # Merge: intel-bridge entries first (priority), then EventScanner entries
        entries = intel_entries + entries
        entries = entries[:self.max_watchlist]

        if entries:
            self.update_watchlist(entries)
            logger.info(f"[HYDRA] Watchlist: {[(e.symbol, e.urgency) for e in entries]}")

        _out = pathlib.Path("data/hydra_watchlist.json")
        _out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(_out, "w") as f:
                json.dump({
                    "generated_at": datetime.utcnow().isoformat(),
                    "candidates": [asdict(c) for c in entries],
                }, f, indent=2, default=str)
            logger.info(f"[HYDRA] Watchlist saved: {len(entries)} candidates → {_out}")
        except Exception as e:
            logger.warning(f"[HYDRA] Failed to save watchlist: {e}")

        return entries

    def evaluate(self, entry: WatchlistEntry, snapshot: TechnicalSnapshot,
                 depth_analysis=None) -> ConvictionScore:
        """
        Evaluate a watchlist entry against technical and depth data.
        Computes the final conviction score.
        """
        conviction = ConvictionScore(
            strategy="HYDRA",
            symbol=entry.symbol,
            direction=entry.direction,
        )

        # 1. Event strength (from Groq classification, max 70)
        conviction.event_strength = entry.urgency * 7.0  # urgency 10 → 70 points

        # 2. Technical confirmation (from HydraRules, max 22)
        ta_score, ta_reasoning = self.rules.confirms_event(snapshot, entry.direction)
        conviction.technical_confirm = ta_score

        # 3. Order book intelligence (max 10)
        # Duck-typing: accept any object with 'signal' attribute (DepthAnalysis or compatible)
        if depth_analysis is not None and hasattr(depth_analysis, 'signal'):
            depth_score = 0.0
            # BUG-1 FIX: Illiquid = hard kill (set score to 0 and skip)
            if getattr(depth_analysis, 'signal', '') == "illiquid":
                conviction.depth_signal = 0.0
                conviction.reasoning = f"Event={conviction.event_strength:.0f}, TA={ta_score:.0f} ({ta_reasoning}), Depth=ILLIQUID_KILL"
                conviction.total = 0.0
                entry.conviction = conviction
                logger.warning(f"[HYDRA] {entry.symbol}: ILLIQUID — hard kill")
                return conviction

            if entry.direction == "BUY":
                if getattr(depth_analysis, 'signal', '') == "strong_bid":
                    depth_score += 5
                if getattr(depth_analysis, 'buy_wall_detected', False):
                    depth_score += 3
                if getattr(depth_analysis, 'is_liquid', False):
                    depth_score += 2
            elif entry.direction == "SHORT":
                if getattr(depth_analysis, 'signal', '') == "strong_ask":
                    depth_score += 5
                if getattr(depth_analysis, 'sell_wall_detected', False):
                    depth_score += 3
                if getattr(depth_analysis, 'is_liquid', False):
                    depth_score += 2
            # BUG-1 FIX: Clamp depth score to [0, 10] — never negative
            conviction.depth_signal = max(0.0, min(depth_score, 10.0))

        # 4. Base total (no Grok inline — orchestrator handles LLM calls centrally)
        subtotal = conviction.event_strength + conviction.technical_confirm + conviction.depth_signal
        conviction.reasoning = f"Event={conviction.event_strength:.0f}, TA={ta_score:.0f} ({ta_reasoning}), Depth={conviction.depth_signal:.0f}"
        conviction.total = max(0.0, min(subtotal, 100.0))
        entry.conviction = conviction
        entry.last_checked = datetime.now()

        logger.info(f"[HYDRA] {entry.symbol} base_conviction={conviction.total:.1f} ({conviction.reasoning[:100]})")
        return conviction

    # ── Candidate Export (for Grok orchestrator) ────────────────

    def get_top_candidates(self, max_n: int = 5) -> List[dict]:
        """
        Export top watchlist entries as dicts for the Grok Portfolio Orchestrator.
        Called by runner.py at orchestrator decision points.
        """
        candidates = []
        for entry in sorted(self.watchlist, key=lambda e: e.urgency, reverse=True)[:max_n]:
            conv = entry.conviction
            candidates.append({
                "symbol": entry.symbol,
                "direction": entry.direction,
                "urgency": entry.urgency,
                "event_summary": entry.event_summary,
                "ta_score": conv.technical_confirm if conv else 0.0,
                "base_conviction": conv.total if conv else 0.0,
            })
        return candidates

    # ── Learning Loop ─────────────────────────────────────────

    def save_trade_result(self, symbol: str, direction: str, entry_price: float,
                          exit_price: float, pnl: float, event_summary: str,
                          conviction_score: float):
        """Persist trade result to HYDRA's own pattern database."""
        db_path = self._pattern_db_path
        try:
            if os.path.exists(db_path):
                with open(db_path) as f:
                    db = json.load(f)
            else:
                db = {"trades": [], "stats": {}}

            db["trades"].append({
                "date": str(date.today()),
                "symbol": symbol,
                "direction": direction,
                "event_summary": event_summary,
                "conviction_score": round(conviction_score, 1),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": round(pnl, 2),
                "win": pnl > 0,
            })

            # Keep last 200 trades
            db["trades"] = db["trades"][-200:]

            # Update stats
            trades = db["trades"]
            wins = sum(1 for t in trades if t.get("win", False))
            db["stats"] = {
                "total_trades": len(trades),
                "wins": wins,
                "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
                "avg_conviction": round(sum(t.get("conviction_score", 0) for t in trades) / len(trades), 1) if trades else 0,
                "last_updated": str(date.today()),
            }

            os.makedirs("data", exist_ok=True)
            with open(db_path, "w") as f:
                json.dump(db, f, indent=2)

            logger.info(f"[HYDRA] Trade result saved: {symbol} PnL=₹{pnl:.2f}")
        except Exception as e:
            logger.error(f"[HYDRA] Failed to save trade result: {e}")

    def reset_daily(self):
        """Reset HYDRA state for new trading day."""
        super().reset_daily()
        self.event_scanner._seen_headlines.clear()
        logger.info("[HYDRA] Daily reset complete")
