"""
groq_client.py — Groq Llama-3.3-70B Client
--------------------------------------------
Ultra-fast LLM for event classification and quick analysis.
~300ms response time. Free tier: 14,400 req/day.

Used for:
  - Rapid event urgency classification (1-10)
  - Pattern matching
  - Quick TA interpretation

Step 9: Dual-key rotation + 429 backoff
  - Reads GROQ_API_KEY (primary) and GROQ_API_KEY_2 (secondary) from env.
  - Round-robin key selection per request.
  - On 429: immediately try next key; on second 429: exponential backoff
    (1s → 2s → 4s) with max 3 backoff rounds; on exhaustion raises to caller.
  - Thread-safe via threading.Lock (used by ThreadPoolExecutor in batch path).
"""
import os
import json
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_GROQ_MODEL = "llama-3.3-70b-versatile"


class _GroqKeyRotator:
    """Thread-safe round-robin key selector with 429 backoff.

    Owns the Groq client instances for all available API keys. The public
    method `call()` wraps a single Groq completion request with the full
    rotation + backoff policy described in the module docstring.
    """

    _MAX_BACKOFF_ROUNDS = 3
    _BACKOFF_SECONDS = [1.0, 2.0, 4.0]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: list[str] = []
        self._clients: list = []
        self._current_idx: int = 0
        self._initialized = False

    def _lazy_init(self) -> None:
        """Load keys and build Groq clients on first use (lazy so import-time env
        vars are respected even if dotenv is loaded after module import)."""
        if self._initialized:
            return
        try:
            from groq import Groq
        except ImportError:
            logger.error("[GroqKeyRotator] groq package not installed. Run: pip install groq")
            self._initialized = True
            return

        for env_var in ("GROQ_API_KEY", "GROQ_API_KEY_2"):
            key = os.getenv(env_var, "").strip()
            if key:
                self._keys.append(key)
                self._clients.append(Groq(api_key=key))

        n = len(self._keys)
        if n == 0:
            logger.error("[GroqKeyRotator] No GROQ_API_KEY found in environment")
        elif n == 1:
            logger.info("[GroqKeyRotator] Single Groq API key loaded (no rotation)")
        else:
            logger.info("[GroqKeyRotator] %d Groq API keys loaded — round-robin rotation enabled", n)

        self._initialized = True

    def _next_idx(self, after: int) -> int:
        """Return the index of the next key after `after` (wraps around)."""
        return (after + 1) % len(self._keys)

    def call(self, messages: list[dict], **kwargs) -> object:
        """Issue one Groq completion with rotation + backoff.

        Args:
            messages: OpenAI-compatible messages list.
            **kwargs: Forwarded to client.chat.completions.create().

        Returns:
            Groq ChatCompletion response.

        Raises:
            Exception: If all retry/backoff rounds exhausted, or on non-429 error.
        """
        with self._lock:
            self._lazy_init()
            if not self._clients:
                raise RuntimeError("No Groq clients available — check GROQ_API_KEY env var")
            # Select the current key (round-robin: advance after each call)
            idx = self._current_idx
            self._current_idx = self._next_idx(idx)

        n_keys = len(self._clients)

        # Protocol (spec §Step-9):
        #   Attempt 1 — selected key (no sleep)
        #   429 → immediately try next key (no sleep)
        #   If next key also 429 → exponential backoff: 1s, 2s, 4s (up to
        #   _MAX_BACKOFF_ROUNDS rounds), alternating keys each round.
        #   After all rounds exhausted → raise the last 429 to the caller.

        def _try_once(key_idx: int) -> object:
            """Issue one API call; raises on any error."""
            return self._clients[key_idx].chat.completions.create(
                model=_GROQ_MODEL, messages=messages, **kwargs,
            )

        last_exc: Optional[Exception] = None
        current = idx

        # Attempt 1 — primary key
        try:
            return _try_once(current)
        except Exception as exc:
            if not self._is_429(exc):
                raise
            last_exc = exc
            next_key = self._next_idx(current)
            logger.warning(
                "[GroqKeyRotator] 429 on key index %d — switching to key index %d immediately",
                current, next_key,
            )
            current = next_key

        # Attempt 2 — alternate key (no sleep: "immediately try key N+1")
        try:
            return _try_once(current)
        except Exception as exc:
            if not self._is_429(exc):
                raise
            last_exc = exc

        # Attempts 3..N — exponential backoff rounds
        for backoff_round in range(self._MAX_BACKOFF_ROUNDS):
            delay = self._BACKOFF_SECONDS[
                min(backoff_round, len(self._BACKOFF_SECONDS) - 1)
            ]
            logger.warning(
                "[GroqKeyRotator] Both keys returned 429 — backoff %.1fs (round %d/%d)",
                delay, backoff_round + 1, self._MAX_BACKOFF_ROUNDS,
            )
            time.sleep(delay)
            current = self._next_idx(current)
            try:
                return _try_once(current)
            except Exception as exc:
                if not self._is_429(exc):
                    raise
                last_exc = exc

        logger.warning(
            "[GroqKeyRotator] All %d retry rounds exhausted — raising 429 to caller",
            self._MAX_BACKOFF_ROUNDS,
        )
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _is_429(exc: Exception) -> bool:
        """Return True if exc is an HTTP 429 (rate-limit) error."""
        # groq-python raises groq.RateLimitError; also check status_code attr
        # and string for resilience against version differences.
        exc_type = type(exc).__name__
        if exc_type in ("RateLimitError",):
            return True
        status = getattr(exc, "status_code", None)
        if status == 429:
            return True
        return "429" in str(exc) or "rate limit" in str(exc).lower()


# Module-level singleton — instantiated once, shared by all callers.
_rotator = _GroqKeyRotator()


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
    body_truncated = (body or "")[:500]

    prompt = f"""Classify this Indian stock market corporate event for trading urgency.

Symbol: {symbol}
Category: {category or "Unknown"}
Headline: {headline}
Body: {body_truncated}

Urgency Scale:
  9-10: Market-moving NOW (earnings beat >10%, major acquisition, regulatory approval, FDA rejection, earnings miss >15%, fraud allegation, sudden CEO exit)
  7-8:  Significant catalyst (earnings inline but strong guidance, bulk deal by FII, promoter pledge increase, large block sell by insider)
  5-6:  Moderate (board meeting outcome, minor corporate action)
  3-4:  Low (routine filings, AGM notice)
  1-2:  Noise (compliance filing, minor updates)

Direction guidance:
  BUY:     Positive catalyst (earnings beat, order win, regulatory approval, expansion, FII accumulation)
  SHORT:   Negative catalyst (earnings miss, FDA rejection, promoter selling, fraud allegation, downgrade, management exit, regulatory penalty)
  NEUTRAL: Ambiguous or unclear impact (board meeting scheduled, routine filing)

Event Types: EARNINGS_SURPRISE, REGULATORY_APPROVAL, REGULATORY_REJECTION, MERGER_ACQUISITION, BULK_BLOCK_DEAL,
PROMOTER_ACTIVITY, CAPEX_EXPANSION, SECTOR_POLICY, MANAGEMENT_CHANGE, DIVIDEND, FRAUD_GOVERNANCE, OTHER

Return ONLY valid JSON:
{{"urgency": <1-10>, "direction": "<BUY|SHORT|NEUTRAL>", "event_type": "<type>", "summary": "<1 sentence>", "material": <true|false>}}"""

    try:
        response = _rotator.call(
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
        logger.debug("[Groq] %s: urgency=%s, type=%s",
                     symbol, result.get("urgency", 0), result.get("event_type", "?"))
        return result

    except Exception as e:
        logger.error("[Groq] Event classification failed for %s: %s", symbol, e)
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
            logger.error("[Groq/batch] Failed to classify %s: %s", event.get("symbol"), e)
            return {
                **event,
                "urgency": 0,
                "direction": "NEUTRAL",
                "event_type": "ERROR",
                "summary": f"Batch classification error: {e}",
                "material": False,
            }

    # Cap workers at 3 to stay within Groq free tier TPM limit (12K tokens/min).
    # Each classify_event call uses ~500 tokens, so 3 concurrent = ~1500 tokens burst.
    max_workers = min(len(events), 3)
    results = [None] * len(events)
    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="GroqBatch") as pool:
        future_to_idx = {pool.submit(_classify_one, ev): i
                         for i, ev in enumerate(events)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()

    logger.debug(
        "[Groq/batch] classified %d events in parallel (max_workers=%d)",
        len(events), max_workers,
    )
    return results
