"""
catalyst_analyzer.py
--------------------
LLM-powered "Why is this stock moving?" agent.

Before entering any trade, a professional trader asks:
  1. What's driving this move?
  2. Is it fundamental (lasting) or technical (noise)?
  3. How much further can it realistically go?
  4. What could go wrong?

This module uses Gemini to answer these questions in structured form.
"""
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import logging
import json
import os

logger = logging.getLogger(__name__)

try:
    import google.generativeai as genai
except ImportError:
    genai = None


@dataclass
class CatalystReport:
    symbol: str
    catalyst_type: str          # "EARNINGS" | "CONTRACT" | "SECTOR_ROTATION" | "TECHNICAL" | "NEWS" | "UNKNOWN"
    catalyst_summary: str       # 1-line summary of why the stock is moving
    is_fundamental: bool        # True = lasting catalyst, False = noise/technical
    estimated_remaining_move: float   # % estimate of further potential (e.g. 1.5 = 1.5%)
    risk_factors: str           # what could go wrong
    confidence: float           # 0–1, how sure is the LLM about this analysis
    direction_bias: str         # "BULLISH" | "BEARISH" | "NEUTRAL"
    raw_response: str = ""


# ── Prompt template ────────────────────────────────────────────────────────

CATALYST_PROMPT = """You are a senior Indian stock market analyst with 20 years of experience.

A stock is showing unusual activity today. Analyze it and return a JSON response.

Stock: {symbol}
Current Price: ₹{ltp}
Today's Change: {pct_change}%
Volume: {volume:,} shares
Direction: {direction} (it's in the top {direction_label} today)

Recent headline (if any): {headline}

Instructions:
1. Based on the symbol, price action, and headline, determine WHY this stock is moving.
2. Classify the catalyst type.
3. Assess if this is a fundamental move (will sustain) or technical noise (will fade).
4. Estimate how much further the stock could realistically move today (as a %).
5. Identify the key risk that could reverse this move.

Return ONLY valid JSON in this exact format:
{{
  "catalyst_type": "EARNINGS|CONTRACT|SECTOR_ROTATION|TECHNICAL|NEWS|UNKNOWN",
  "catalyst_summary": "One clear sentence explaining the move",
  "is_fundamental": true/false,
  "estimated_remaining_move_pct": 1.5,
  "risk_factors": "One sentence on what could go wrong",
  "confidence": 0.7,
  "direction_bias": "BULLISH|BEARISH|NEUTRAL"
}}"""


class CatalystAnalyzer:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = None

        if genai and self.api_key:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel("gemini-2.0-flash")
        else:
            logger.warning("CatalystAnalyzer: Gemini unavailable, operating in stub mode.")

    def analyze(
        self,
        symbol: str,
        ltp: float = 0.0,
        pct_change: float = 0.0,
        volume: int = 0,
        direction: str = "LONG",
        headline: str = "",
    ) -> CatalystReport:
        """
        Analyze why a stock is moving and return a structured report.
        Falls back to a rule-based stub if Gemini is unavailable.
        """
        if not self.model:
            return self._stub_analysis(symbol, pct_change, direction, headline)

        direction_label = "gainers" if direction == "LONG" else "losers"
        prompt = CATALYST_PROMPT.format(
            symbol=symbol,
            ltp=ltp,
            pct_change=pct_change,
            volume=volume,
            direction=direction,
            direction_label=direction_label,
            headline=headline or "No headline available",
        )

        try:
            response = self.model.generate_content(prompt)
            raw = response.text.strip()

            # Extract JSON from response (handle markdown code blocks)
            json_str = raw
            if "```json" in raw:
                json_str = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                json_str = raw.split("```")[1].split("```")[0].strip()

            data = json.loads(json_str)

            return CatalystReport(
                symbol=symbol,
                catalyst_type=data.get("catalyst_type", "UNKNOWN"),
                catalyst_summary=data.get("catalyst_summary", "Unable to determine"),
                is_fundamental=data.get("is_fundamental", False),
                estimated_remaining_move=float(data.get("estimated_remaining_move_pct", 0.0)),
                risk_factors=data.get("risk_factors", "Unknown risks"),
                confidence=float(data.get("confidence", 0.5)),
                direction_bias=data.get("direction_bias", "NEUTRAL"),
                raw_response=raw,
            )

        except Exception as e:
            logger.error(f"CatalystAnalyzer failed for {symbol}: {e}")
            return self._stub_analysis(symbol, pct_change, direction, headline)

    def _stub_analysis(
        self,
        symbol: str,
        pct_change: float,
        direction: str,
        headline: str,
    ) -> CatalystReport:
        """Rule-based fallback when Gemini is unavailable."""
        abs_pct = abs(pct_change)

        # Guess catalyst type from headline keywords
        cat_type = "UNKNOWN"
        hl = headline.lower()
        if any(kw in hl for kw in ["result", "profit", "revenue", "earnings"]):
            cat_type = "EARNINGS"
        elif any(kw in hl for kw in ["order", "contract", "wins", "awarded"]):
            cat_type = "CONTRACT"
        elif any(kw in hl for kw in ["acquisition", "merger", "buyback"]):
            cat_type = "NEWS"

        is_fundamental = cat_type in ("EARNINGS", "CONTRACT", "NEWS")
        remaining = max(0.5, abs_pct * 0.3)  # conservative: 30% of today's move remaining

        return CatalystReport(
            symbol=symbol,
            catalyst_type=cat_type,
            catalyst_summary=headline[:100] if headline else f"{symbol} moving {pct_change:+.1f}% — reason unknown",
            is_fundamental=is_fundamental,
            estimated_remaining_move=round(remaining, 2),
            risk_factors="No LLM analysis available — proceed with caution",
            confidence=0.3,
            direction_bias="BULLISH" if direction == "LONG" else "BEARISH",
        )
