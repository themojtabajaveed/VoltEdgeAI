"""
groq_client.py — Groq Llama-3.3-70B Client
--------------------------------------------
Ultra-fast LLM for event classification and quick analysis.
~300ms response time. Free tier: 14,400 req/day.

Used for:
  - Rapid event urgency classification (1-10)
  - Pattern matching
  - Quick TA interpretation
"""
import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_GROQ_MODEL = "llama-3.3-70b-versatile"


def _get_client():
    """Lazy-init the Groq client."""
    try:
        from groq import Groq
    except ImportError:
        logger.error("groq package not installed. Run: pip install groq")
        return None

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY not set in environment")
        return None

    return Groq(api_key=api_key)


def classify_event(
    symbol: str,
    headline: str,
    category: str = "",
    body: str = "",
) -> dict:
    """
    Classify a corporate event for trading urgency.
    Ultra-fast (~300ms) via Groq.

    Args:
        symbol: Stock symbol
        headline: Event headline
        category: Event category (e.g. "Board Meeting", "Results")
        body: Full event body text (truncated to 500 chars)

    Returns:
        {
            "urgency": 1-10,
            "direction": "BUY" | "SHORT" | "NEUTRAL",
            "event_type": "EARNINGS_SURPRISE" | "REGULATORY" | "DEAL" | ...,
            "summary": "One sentence explanation",
            "material": true/false  (will this move the stock >1%?)
        }
    """
    client = _get_client()
    if client is None:
        return {"urgency": 0, "direction": "NEUTRAL", "event_type": "UNKNOWN",
                "summary": "Groq unavailable", "material": False}

    body_truncated = (body or "")[:500]

    prompt = f"""Classify this Indian stock market corporate event for trading urgency.

Symbol: {symbol}
Category: {category or "Unknown"}
Headline: {headline}
Body: {body_truncated}

Urgency Scale:
  9-10: Market-moving NOW (earnings beat >10%, major acquisition, regulatory approval)
  7-8:  Significant catalyst (earnings inline but strong guidance, bulk deal by FII)
  5-6:  Moderate (board meeting outcome, minor corporate action)
  3-4:  Low (routine filings, AGM notice)
  1-2:  Noise (compliance filing, minor updates)

Event Types: EARNINGS_SURPRISE, REGULATORY_APPROVAL, MERGER_ACQUISITION, BULK_BLOCK_DEAL, 
PROMOTER_ACTIVITY, CAPEX_EXPANSION, SECTOR_POLICY, MANAGEMENT_CHANGE, DIVIDEND, OTHER

Return ONLY valid JSON:
{{"urgency": <1-10>, "direction": "<BUY|SHORT|NEUTRAL>", "event_type": "<type>", "summary": "<1 sentence>", "material": <true|false>}}"""

    try:
        response = client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        raw = response.choices[0].message.content.strip()

        json_str = raw
        if "```json" in raw:
            json_str = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            json_str = raw.split("```")[1].split("```")[0].strip()

        result = json.loads(json_str)
        logger.debug(f"[Groq] {symbol}: urgency={result.get('urgency', 0)}, type={result.get('event_type', '?')}")
        return result

    except Exception as e:
        logger.error(f"[Groq] Event classification failed for {symbol}: {e}")
        return {"urgency": 0, "direction": "NEUTRAL", "event_type": "ERROR",
                "summary": f"Classification error: {e}", "material": False}


def classify_events_batch(events: list[dict]) -> list[dict]:
    """
    Optimisation 5 — Classify multiple events in PARALLEL via ThreadPoolExecutor.

    All Groq API calls are issued simultaneously. Total latency ≈ the slowest
    single call (~300ms) instead of N × 300ms.

    Args:
        events: List of {symbol, headline, category, body}

    Returns:
        Same list with classification fields merged in.
    """
    if not events:
        return events

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _classify_one(event: dict) -> dict:
        try:
            classification = classify_event(
                symbol=event.get("symbol", ""),
                headline=event.get("headline", ""),
                category=event.get("category", ""),
                body=event.get("body", ""),
            )
            return {**event, **classification}
        except Exception as e:
            logger.error(f"[Groq/batch] Failed to classify {event.get('symbol')}: {e}")
            return {
                **event,
                "urgency": 0,
                "direction": "NEUTRAL",
                "event_type": "ERROR",
                "summary": f"Batch classification error: {e}",
                "material": False,
            }

    # Cap workers at min(len(events), 10) to avoid flooding Groq free tier.
    max_workers = min(len(events), 10)
    results = [None] * len(events)
    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="GroqBatch") as pool:
        future_to_idx = {pool.submit(_classify_one, ev): i
                         for i, ev in enumerate(events)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()

    logger.debug(
        f"[Groq/batch] classified {len(events)} events in parallel "
        f"(max_workers={max_workers})"
    )
    return results
