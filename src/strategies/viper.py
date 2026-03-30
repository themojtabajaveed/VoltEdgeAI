"""
viper.py — VIPER Strategy (Head 2: Top Mover Momentum)
-------------------------------------------------------
Scans top gainers/losers at key market intervals, classifies
move types, and executes STRIKE (continuation) or logs COIL
(reversal, dry-run only) trades.

Pipeline:
  1. Scan top movers (momentum_scanner.py → Kite batch quote)
  2. Classify each mover (move_classifier.py → rules + Groq)
  3. Get Grok conviction with X real-time sentiment
  4. TA confirmation (viper_rules.py → TechnicalBody)
  5. Score → if ≥70: STRIKE=live trade, COIL=dry-run log
"""
import os
import json
import logging
from datetime import datetime, date
from typing import Optional, List, Dict

from src.strategies.base import StrategyHead, ConvictionScore, WatchlistEntry
from src.strategies.technical_body import TechnicalBody, TechnicalSnapshot
from src.strategies.move_classifier import (
    MoveClassifier, ClassifiedMover, MoveType, TradeMode
)
from src.strategies.viper_rules import ViperRules

logger = logging.getLogger(__name__)


class ViperStrategy(StrategyHead):
    """
    VIPER — Tracks top gainers/losers and trades momentum
    continuation (STRIKE, live) and mean reversion (COIL, dry-run).
    """

    def __init__(self):
        super().__init__(name="VIPER", max_watchlist=10)
        self.ta_body = TechnicalBody()
        self.classifier = MoveClassifier()
        self.rules = ViperRules()

        # State
        self.watchlist: List[WatchlistEntry] = []
        self._classified_movers: List[ClassifiedMover] = []
        self._scan_count_today = 0
        self._last_scan_time: Optional[datetime] = None

        # COIL dry-run log
        self._coil_signals: List[dict] = []

    # ── Core Interface ────────────────────────────────────

    def scan(self, access_token: str = None) -> List[WatchlistEntry]:
        """
        Scan top movers from Kite batch quote, classify them,
        and build the VIPER watchlist.

        Args:
            access_token: Kite access token (from runner)

        Returns:
            List of WatchlistEntry for tradeable movers.
        """
        self._scan_count_today += 1
        logger.info(f"[VIPER] Scan #{self._scan_count_today} starting...")

        # 1. Fetch top movers
        movers_data = self._fetch_movers(access_token)
        if not movers_data:
            logger.warning("[VIPER] No movers found — skipping scan")
            return []

        # 2. Get sector map for context
        sector_map = self._get_sector_map()

        # 3. Classify each mover
        self._classified_movers = self.classifier.classify_movers(
            movers_data, sector_map
        )

        # 4. Enrich with Groq (fast, free)
        tradeable = [
            cm for cm in self._classified_movers
            if cm.trade_mode != TradeMode.SKIP
        ]
        if tradeable:
            self.classifier.enrich_with_groq(tradeable)

        # 5. Build watchlist entries
        self.watchlist = []
        for cm in self._classified_movers:
            if cm.trade_mode == TradeMode.SKIP:
                continue

            entry = WatchlistEntry(
                symbol=cm.symbol,
                direction=cm.direction,
                event_summary=(
                    f"{cm.move_type.value}: {cm.pct_change:+.1f}% "
                    f"vol={cm.volume_ratio:.1f}x | {cm.classification_reason}"
                ),
                urgency=self._urgency_from_move(cm),
            )
            # Stash extra data for later use
            entry.metadata = {
                "move_type": cm.move_type.value,
                "trade_mode": cm.trade_mode.value,
                "gap_pct": cm.gap_pct,
                "volume_ratio": cm.volume_ratio,
                "pct_change": cm.pct_change,
                "sector": cm.sector,
                "is_sector_leader": cm.is_sector_leader,
                "groq_summary": cm.groq_summary,
            }
            self.watchlist.append(entry)

        self._last_scan_time = datetime.now()
        logger.info(
            f"[VIPER] Scan #{self._scan_count_today} complete: "
            f"{len(self.watchlist)} tradeable movers "
            f"({sum(1 for w in self.watchlist if w.metadata.get('trade_mode') == 'STRIKE')} STRIKE, "
            f"{sum(1 for w in self.watchlist if w.metadata.get('trade_mode') == 'COIL')} COIL)"
        )
        return self.watchlist

    def evaluate(
        self,
        entry: WatchlistEntry,
        snapshot: TechnicalSnapshot,
        depth_analysis=None,
    ) -> ConvictionScore:
        """
        Score a VIPER watchlist entry for trade conviction.

        Args:
            entry: WatchlistEntry from scan()
            snapshot: Live TechnicalSnapshot from shared TechnicalBody
            depth_analysis: Optional depth book data

        Returns:
            ConvictionScore with total 0-100. Trade if ≥70 (STRIKE) or ≥75 (COIL).
        """
        conviction = ConvictionScore(
            strategy="VIPER",
            symbol=entry.symbol,
            direction=entry.direction,
        )

        metadata = getattr(entry, 'metadata', {}) or {}
        trade_mode = metadata.get("trade_mode", "STRIKE")
        move_type = metadata.get("move_type", "UNKNOWN")
        pct_change = metadata.get("pct_change", 0.0)
        gap_pct = metadata.get("gap_pct", 0.0)
        volume_ratio = metadata.get("volume_ratio", 0.0)

        # ── 1. Move Quality (max 30) ──
        move_score = self._score_move_quality(
            pct_change, gap_pct, volume_ratio, move_type, trade_mode
        )
        conviction.event_strength = move_score

        # ── 2. TA Confirmation (max 25) ──
        if trade_mode == "STRIKE":
            ta_score, ta_reason = self.rules.strike_confirms(
                snapshot, entry.direction, gap_pct
            )
        else:  # COIL
            # For COIL, direction is the REVERSAL direction
            ta_score, ta_reason = self.rules.coil_confirms(
                snapshot, entry.direction, pct_change
            )
        conviction.technical_confirm = ta_score

        # ── 3. Depth signal (max 10) ──
        if depth_analysis is not None and hasattr(depth_analysis, 'signal'):
            if getattr(depth_analysis, 'signal', '') == "illiquid":
                conviction.depth_signal = 0.0
                conviction.total = 0.0
                conviction.reasoning = f"ILLIQUID — hard kill"
                entry.conviction = conviction
                return conviction

            depth_score = 0.0
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
            conviction.depth_signal = max(0.0, min(depth_score, 10.0))

        # ── 4. Context bonus (max 10) ──
        context_score = 0.0
        context_reasons = []

        # Sector leader bonus
        if metadata.get("is_sector_leader", False) and move_type == "SECTOR_WAVE":
            context_score += 5
            context_reasons.append("Sector leader")

        # Not just noise — volume confirms
        if volume_ratio >= 2.5:
            context_score += 3
            context_reasons.append(f"Strong volume {volume_ratio:.1f}x")

        # Time-of-day bonus for COIL (better after 11 AM)
        if trade_mode == "COIL":
            try:
                import zoneinfo
                IST = zoneinfo.ZoneInfo("Asia/Kolkata")
                current_hour = datetime.now(IST).hour
                if current_hour >= 11:
                    context_score += 2
                    context_reasons.append("Post-11AM (optimal COIL window)")
            except Exception:
                pass

        conviction.context_bonus = min(context_score, 10.0)

        # ── 5. Subtotal ──
        subtotal = conviction.event_strength + conviction.technical_confirm + conviction.depth_signal + conviction.context_bonus
        conviction.reasoning = (
            f"Move={move_score:.0f} ({move_type}), "
            f"TA={ta_score:.0f} ({ta_reason}), "
            f"Context={context_score:.0f}"
        )

        # ── 6. Base total (no Grok inline — orchestrator handles LLM calls centrally) ──
        conviction.total = max(0.0, min(subtotal, 100.0))
        entry.conviction = conviction
        entry.last_checked = datetime.now()

        # ── 7. COIL dry-run logging ──
        if trade_mode == "COIL" and conviction.total >= 70:
            self._log_coil_signal(entry, conviction, snapshot, metadata)

        logger.info(
            f"[VIPER] {entry.symbol} [{trade_mode}] "
            f"base_score={conviction.total:.1f} "
            f"{'→ CANDIDATE' if conviction.total >= 45 else '→ SKIP'}"
        )
        return conviction

    # ── Candidate Export (for Grok orchestrator) ──────────────

    def get_top_candidates(self, max_n: int = 5) -> List[dict]:
        """
        Export top watchlist entries as dicts for the Grok Portfolio Orchestrator.
        Called by runner.py at orchestrator decision points.
        """
        candidates = []
        for entry in sorted(self.watchlist, key=lambda e: e.urgency, reverse=True)[:max_n]:
            meta = getattr(entry, 'metadata', {}) or {}
            conv = entry.conviction
            candidates.append({
                "symbol": entry.symbol,
                "direction": entry.direction,
                "move_type": meta.get('move_type', 'UNKNOWN'),
                "pct_change": meta.get('pct_change', 0),
                "volume_ratio": meta.get('volume_ratio', 0),
                "trade_mode": meta.get('trade_mode', 'STRIKE'),
                "ta_score": conv.technical_confirm if conv else 0.0,
                "base_conviction": conv.total if conv else 0.0,
            })
        return candidates

    # ── COIL Dry-Run Reporting ─────────────────────────────

    def _log_coil_signal(
        self,
        entry: WatchlistEntry,
        conviction: ConvictionScore,
        snapshot: TechnicalSnapshot,
        metadata: dict,
    ) -> None:
        """Log a COIL signal for dry-run analysis."""
        try:
            import zoneinfo
            IST = zoneinfo.ZoneInfo("Asia/Kolkata")
            now = datetime.now(IST)
        except Exception:
            now = datetime.now()

        signal = {
            "symbol": entry.symbol,
            "signal_time": now.strftime("%H:%M:%S"),
            "direction": entry.direction,
            "entry_price": snapshot.last_price,
            "conviction_score": round(conviction.total, 1),
            "move_type": metadata.get("move_type", ""),
            "pct_change": metadata.get("pct_change", 0),
            "rsi_at_signal": round(snapshot.rsi14, 1),
            "vwap": round(snapshot.vwap, 2),
            "volume_ratio": metadata.get("volume_ratio", 0),
            "conviction_reasoning": conviction.reasoning[:200],
            "hypothetical_sl": self._calc_coil_sl(snapshot, entry.direction),
            "hypothetical_target": self._calc_coil_target(snapshot, entry.direction),
        }
        self._coil_signals.append(signal)
        logger.info(
            f"[VIPER/COIL DRY-RUN] {entry.symbol} {entry.direction} "
            f"@ ₹{snapshot.last_price:.2f} conviction={conviction.total:.1f} "
            f"SL=₹{signal['hypothetical_sl']:.2f} "
            f"TGT=₹{signal['hypothetical_target']:.2f}"
        )

    def _calc_coil_sl(self, snapshot: TechnicalSnapshot, direction: str) -> float:
        """Calculate COIL stop loss — beyond day's extreme + 1 ATR."""
        atr = snapshot.atr14 if snapshot.atr14 > 0 else snapshot.last_price * 0.01
        if direction == "SHORT":
            # SL above day high + ATR
            return snapshot.orb_high + atr if snapshot.orb_high > 0 else snapshot.last_price * 1.02
        else:
            # SL below day low - ATR
            return (snapshot.orb_low - atr) if snapshot.orb_low > 0 else snapshot.last_price * 0.98

    def _calc_coil_target(self, snapshot: TechnicalSnapshot, direction: str) -> float:
        """Calculate COIL target — 50% retracement toward VWAP."""
        if snapshot.vwap <= 0:
            return snapshot.last_price * (0.985 if direction == "SHORT" else 1.015)
        distance = abs(snapshot.last_price - snapshot.vwap)
        if direction == "SHORT":
            return snapshot.last_price - (distance * 0.5)
        else:
            return snapshot.last_price + (distance * 0.5)

    def save_coil_report(self) -> str:
        """
        Save today's COIL dry-run signals to disk.
        Called at EOD by the runner.

        Returns:
            Path to saved report file.
        """
        if not self._coil_signals:
            return ""

        try:
            import zoneinfo
            IST = zoneinfo.ZoneInfo("Asia/Kolkata")
            today = datetime.now(IST).strftime("%Y-%m-%d")
        except Exception:
            today = str(date.today())

        report_dir = os.path.join("logs", "viper_coil")
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, f"{today}_coil_report.json")

        report = {
            "date": today,
            "coil_signals": self._coil_signals,
            "summary": {
                "total_signals": len(self._coil_signals),
                "avg_conviction": round(
                    sum(s["conviction_score"] for s in self._coil_signals)
                    / len(self._coil_signals), 1
                ) if self._coil_signals else 0,
                "symbols": [s["symbol"] for s in self._coil_signals],
            },
        }

        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"[VIPER] COIL report saved: {report_path}")
        return report_path

    # ── Helpers ────────────────────────────────────────────

    def _score_move_quality(
        self,
        pct_change: float,
        gap_pct: float,
        volume_ratio: float,
        move_type: str,
        trade_mode: str,
    ) -> float:
        """
        Score the quality of a price/volume move (max 30 points).

        Components:
          - Price magnitude (max 12): how big the move is
          - Gap quality (max 8): gap size with volume backing
          - Volume conviction (max 10): volume ratio tiers

        COIL (reversal) trades get a 20% penalty since counter-trend
        requires stronger evidence.
        """
        score = 0.0
        abs_change = abs(pct_change)

        # ── 1. Price magnitude (max 12) ──
        if abs_change >= 5.0:
            score += 12
        elif abs_change >= 3.0:
            score += 8
        elif abs_change >= 2.0:
            score += 4
        elif abs_change >= 1.0:
            score += 2

        # ── 2. Gap quality (max 8) ──
        abs_gap = abs(gap_pct)
        if abs_gap >= 3.0 and volume_ratio >= 2.0:
            score += 8  # Strong gap with strong volume
        elif abs_gap >= 2.0 and volume_ratio >= 1.5:
            score += 5  # Decent gap with decent volume
        elif abs_gap >= 1.5:
            score += 3  # Moderate gap
        elif abs_gap >= 0.5:
            score += 1  # Small gap

        # ── 3. Volume conviction (max 10) ──
        # NOTE: volume_ratio is currently a price-derived proxy
        # (abs(pct_change)/2), not actual relative volume.
        # Thresholds are calibrated for this proxy.
        if volume_ratio >= 2.5:
            score += 10
        elif volume_ratio >= 2.0:
            score += 6
        elif volume_ratio >= 1.5:
            score += 3
        elif volume_ratio >= 1.0:
            score += 1

        # ── COIL penalty: counter-trend needs higher bar ──
        if trade_mode == "COIL":
            score *= 0.80

        return min(score, 30.0)

    def _fetch_movers(self, access_token: str = None) -> List[dict]:
        """Fetch top movers from momentum_scanner."""
        try:
            from src.sniper.momentum_scanner import fetch_top_movers
            result = fetch_top_movers(access_token=access_token)

            movers = []
            for c in result.get("gainers", []) + result.get("losers", []):
                m = {
                    "symbol": c.symbol if hasattr(c, 'symbol') else c.get("symbol", ""),
                    "last_price": c.last_price if hasattr(c, 'last_price') else c.get("last_price", 0),
                    "prev_close": c.prev_close if hasattr(c, 'prev_close') else c.get("prev_close", 0),
                    "pct_change": c.pct_change if hasattr(c, 'pct_change') else c.get("pct_change", 0),
                    "volume": c.volume if hasattr(c, 'volume') else c.get("volume", 0),
                    "direction": c.direction if hasattr(c, 'direction') else c.get("direction", ""),
                }
                # Compute gap % and volume ratio from available data
                prev = float(m["prev_close"])
                if prev > 0:
                    open_p = float(m.get("open_price", m["last_price"]))
                    m["gap_pct"] = round((open_p - prev) / prev * 100, 2)
                else:
                    m["gap_pct"] = 0
                # ⚠️ PROXY WARNING: volume_ratio is derived from price change,
                # not actual relative volume. momentum_scanner does not provide
                # average volume data. All downstream volume-based rules
                # (COIL exhaust, GAP_AND_TRAP, etc.) use this proxy.
                m["volume_ratio"] = max(1.0, abs(float(m["pct_change"])) / 2.0)
                m["open_price"] = m.get("open_price", m["last_price"])

                movers.append(m)

            if movers and self._scan_count_today <= 1:
                logger.warning(
                    "[VIPER] volume_ratio is a price-derived proxy (abs(pct_change)/2), "
                    "not actual relative volume. Volume-based rules operate on estimated data."
                )

            return movers

        except Exception as e:
            logger.error(f"[VIPER] Mover fetch failed: {e}")
            return []

    def _get_sector_map(self) -> dict:
        """Get symbol→sector mapping from sector_guard."""
        try:
            from src.trading.sector_guard import SECTOR_MAP
            # Invert: SECTOR_MAP is {sector: [symbols]}
            result = {}
            for sector, symbols in SECTOR_MAP.items():
                for sym in symbols:
                    result[sym] = sector
            return result
        except Exception:
            return {}

    def _urgency_from_move(self, cm: ClassifiedMover) -> float:
        """Convert move magnitude to urgency score (0-10)."""
        abs_change = abs(cm.pct_change)
        base = min(abs_change * 1.5, 8.0)  # Scale: 5% → 7.5

        # Bonus for volume
        if cm.volume_ratio >= 2.5:
            base += 1.5
        elif cm.volume_ratio >= 1.5:
            base += 0.5

        return min(base, 10.0)

    def reset_daily(self) -> None:
        """Reset VIPER state at midnight. Called by runner."""
        # Save COIL report before reset
        if self._coil_signals:
            self.save_coil_report()

        self.watchlist = []
        self._classified_movers = []
        self._coil_signals = []
        self._scan_count_today = 0
        self._last_scan_time = None
        logger.info("[VIPER] Daily reset complete")

    def check_confluence(self, hydra_symbols: List[str]) -> List[str]:
        """
        Check for cross-head confluence: stocks in BOTH HYDRA and VIPER watchlists.

        Args:
            hydra_symbols: List of symbols currently in HYDRA watchlist.

        Returns:
            List of symbols that appear in both.
        """
        viper_symbols = {e.symbol for e in self.watchlist}
        overlap = viper_symbols.intersection(set(hydra_symbols))
        if overlap:
            logger.info(
                f"[VIPER] 🐉 DRAGON CONFLUENCE detected: {overlap} "
                f"(in both HYDRA and VIPER watchlists!)"
            )
        return list(overlap)
