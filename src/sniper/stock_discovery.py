"""
stock_discovery.py
------------------
Multi-source stock discovery: fuses NSE announcements, momentum scanner
output, and news context into a single ranked watchlist.

Each stock gets scored on:
  catalyst_score  (0–5): strength of news/event
  momentum_score  (0–5): volume + % change magnitude
  liquidity_score (0–5): can we actually trade this size?

Top N = highest combined score → feeds into TechnicalScorer for final
entry evaluation.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredStock:
    symbol: str
    direction: str                  # "LONG" or "SHORT"
    catalyst_score: float = 0.0     # 0–5
    momentum_score: float = 0.0     # 0–5
    liquidity_score: float = 0.0    # 0–5
    total_score: float = 0.0        # sum of above
    sources: List[str] = field(default_factory=list)
    catalyst_headline: str = ""
    pct_change: float = 0.0
    volume: int = 0
    ltp: float = 0.0
    prev_close: float = 0.0

    def compute_total(self):
        self.total_score = self.catalyst_score + self.momentum_score + self.liquidity_score


# ── Scoring functions ──────────────────────────────────────────────────────

def _score_catalyst(headline: str) -> float:
    """
    Score the strength of a catalyst from 0–5.
    Strong catalysts: earnings beat, major contract, acquisition
    Weak catalysts: routine compliance, board meeting notice
    """
    if not headline:
        return 0.0

    hl = headline.lower()

    # Strong catalysts (4–5 pts)
    strong = [
        "financial results", "profit", "revenue", "earnings",
        "acquisition", "merger", "buyback", "bonus", "split",
        "order", "contract", "wins", "awarded",
        "approval", "license", "patent",
    ]
    for kw in strong:
        if kw in hl:
            return 4.5

    # Medium catalysts (2–3 pts)
    medium = [
        "dividend", "allotment", "scheme of arrangement",
        "expansion", "capacity", "partnership",
        "board meeting", "agm", "egm",
    ]
    for kw in medium:
        if kw in hl:
            return 2.5

    # Weak / noise (0–1 pts)
    noise = [
        "trading window", "newspaper publication", "esop", "esos",
        "employee stock", "intimation", "closure",
    ]
    for kw in noise:
        if kw in hl:
            return 0.0

    return 1.0  # Unknown headline — mild credit


def _score_momentum(pct_change: float, volume: int) -> float:
    """Score momentum from % change and volume."""
    score = 0.0

    # % change component (0–3)
    abs_pct = abs(pct_change)
    if abs_pct >= 5.0:
        score += 3.0
    elif abs_pct >= 3.0:
        score += 2.0
    elif abs_pct >= 1.5:
        score += 1.0

    # Volume component (0–2)
    if volume >= 5_000_000:
        score += 2.0
    elif volume >= 2_000_000:
        score += 1.5
    elif volume >= 500_000:
        score += 1.0

    return min(score, 5.0)


def _score_liquidity(ltp: float, volume: int) -> float:
    """Score tradability: avoid penny stocks and thin markets."""
    score = 0.0

    # Price filter
    if ltp >= 100:
        score += 2.0
    elif ltp >= 50:
        score += 1.0
    else:
        return 0.0  # penny stock — disqualify

    # Turnover proxy (volume × ltp)
    turnover = volume * ltp
    if turnover >= 50_000_000:  # ₹5 Cr+
        score += 3.0
    elif turnover >= 10_000_000:  # ₹1 Cr+
        score += 2.0
    elif turnover >= 2_000_000:  # ₹20 Lakh+
        score += 1.0

    return min(score, 5.0)


# ── Main fuser ─────────────────────────────────────────────────────────────

class StockDiscovery:
    """
    Fuses multiple discovery sources into a ranked watchlist.
    """

    def __init__(self, top_n: int = 10):
        self.top_n = top_n
        self._candidates: Dict[str, DiscoveredStock] = {}

    def ingest_scanner_results(
        self,
        gainers: List,     # List[CandidateStock] from momentum_scanner
        losers: List,      # List[CandidateStock]
    ) -> None:
        """Ingest momentum scanner output."""
        for c in gainers:
            self._merge(
                symbol=c.symbol,
                direction="LONG",
                source="scanner_gainer",
                pct_change=c.pct_change,
                volume=c.volume,
                ltp=c.last_price,
                prev_close=c.prev_close,
            )

        for c in losers:
            self._merge(
                symbol=c.symbol,
                direction="SHORT",
                source="scanner_loser",
                pct_change=c.pct_change,
                volume=c.volume,
                ltp=c.last_price,
                prev_close=c.prev_close,
            )

    def ingest_announcements(self, announcements: List) -> None:
        """
        Ingest NSE/BSE announcements (Announcement dataclass from corporate_actions).
        """
        for ann in announcements:
            headline = ann.headline or ann.body or ""
            cat_score = _score_catalyst(headline)
            if cat_score < 1.0:
                continue  # skip noise

            self._merge(
                symbol=ann.symbol,
                direction="LONG",  # announcements are typically bullish events
                source="nse_announcement",
                catalyst_headline=headline,
                catalyst_score_override=cat_score,
            )

    def ingest_manual(
        self,
        symbol: str,
        direction: str,
        source: str = "manual",
        ltp: float = 0.0,
        pct_change: float = 0.0,
        volume: int = 0,
        headline: str = "",
    ) -> None:
        """Add a manually identified stock (e.g., from social media / analyst tip)."""
        self._merge(
            symbol=symbol,
            direction=direction,
            source=source,
            pct_change=pct_change,
            volume=volume,
            ltp=ltp,
            catalyst_headline=headline,
        )

    def get_ranked_watchlist(self) -> List[DiscoveredStock]:
        """
        Return top N candidates ranked by total score.
        Returns both LONG and SHORT candidates mixed, sorted by total_score desc.
        """
        for stock in self._candidates.values():
            stock.compute_total()

        ranked = sorted(
            self._candidates.values(),
            key=lambda s: s.total_score,
            reverse=True,
        )
        return ranked[:self.top_n]

    def get_long_candidates(self) -> List[DiscoveredStock]:
        """Return only LONG candidates, ranked."""
        ranked = self.get_ranked_watchlist()
        return [s for s in ranked if s.direction == "LONG"]

    def get_short_candidates(self) -> List[DiscoveredStock]:
        """Return only SHORT candidates, ranked."""
        ranked = self.get_ranked_watchlist()
        return [s for s in ranked if s.direction == "SHORT"]

    def reset(self):
        """Clear all candidates for a new session."""
        self._candidates.clear()

    def _merge(
        self,
        symbol: str,
        direction: str,
        source: str,
        pct_change: float = 0.0,
        volume: int = 0,
        ltp: float = 0.0,
        prev_close: float = 0.0,
        catalyst_headline: str = "",
        catalyst_score_override: Optional[float] = None,
    ) -> None:
        """
        Merge a new signal into the candidate pool.
        If a symbol appears from multiple sources, scores stack.
        """
        key = f"{symbol}_{direction}"

        if key not in self._candidates:
            self._candidates[key] = DiscoveredStock(
                symbol=symbol,
                direction=direction,
            )

        stock = self._candidates[key]
        stock.sources.append(source)

        # Update price info with latest
        if ltp > 0:
            stock.ltp = ltp
        if prev_close > 0:
            stock.prev_close = prev_close
        if pct_change != 0:
            stock.pct_change = pct_change
        if volume > 0:
            stock.volume = max(stock.volume, volume)

        # Score catalyst
        if catalyst_score_override is not None:
            stock.catalyst_score = max(stock.catalyst_score, catalyst_score_override)
        elif catalyst_headline:
            stock.catalyst_score = max(stock.catalyst_score, _score_catalyst(catalyst_headline))

        if catalyst_headline and not stock.catalyst_headline:
            stock.catalyst_headline = catalyst_headline

        # Score momentum
        if pct_change != 0 or volume > 0:
            stock.momentum_score = max(
                stock.momentum_score,
                _score_momentum(stock.pct_change, stock.volume),
            )

        # Score liquidity
        if stock.ltp > 0 and stock.volume > 0:
            stock.liquidity_score = max(
                stock.liquidity_score,
                _score_liquidity(stock.ltp, stock.volume),
            )

        # Multi-source bonus: add 1 pt when stock first reaches 2 unique sources
        unique_sources = len(set(stock.sources))
        if unique_sources == 2 and len(stock.sources) == 2:
            # Only apply bonus on the exact merge that creates the 2nd unique source
            stock.catalyst_score = min(5.0, stock.catalyst_score + 1.0)
