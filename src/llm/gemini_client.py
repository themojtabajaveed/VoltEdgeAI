"""gemini_client.py — Google Gemini Client (production-grade)
---------------------------------------------------------------
LLM for deep document understanding and structured extraction from
Indian corporate filings (XBRL/PDF text → strict JSON).

Mirrors the architecture of `groq_client.py`:
  - Lazy module-level singleton, thread-safe via `threading.Lock`
  - 429/5xx retry with single-attempt exponential backoff
  - JSON parsing with markdown fallback
  - Daily budget tracking with hard-stop fallback to deterministic logic

Used by `src/event_intelligence/classifier.py` for earnings, guidance,
dividend, and board-meeting extraction. Default model: `gemini-2.5-pro`.

Reads GEMINI_API_KEY from env. If absent, the client logs a warning and
all calls return None — callers must handle that path (and they do, via
heuristic fallback in classifier.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_DEFAULT_MODEL = "gemini-2.5-pro"
_BACKOFF_SECONDS = [2.0, 5.0]   # 2 retries; total worst-case ~7s


class _GeminiBudget:
    """Per-day call counter with hard-stop at `daily_budget`."""

    def __init__(self, daily_budget: int) -> None:
        self._lock = threading.Lock()
        self._date: Optional[str] = None
        self._count: int = 0
        self._budget = max(1, int(daily_budget))

    def _today(self) -> str:
        return datetime.now(IST).strftime("%Y-%m-%d")

    def try_consume(self) -> bool:
        """Consume one call quota slot. Returns False if today's budget exhausted."""
        with self._lock:
            today = self._today()
            if self._date != today:
                self._date = today
                self._count = 0
            if self._count >= self._budget:
                return False
            self._count += 1
            if self._count == int(self._budget * 0.8):
                logger.warning(
                    "[GeminiClient] 80%% of daily budget consumed (%d/%d)",
                    self._count, self._budget,
                )
            return True

    def used(self) -> int:
        with self._lock:
            return self._count


class _GeminiClient:
    """Thread-safe lazy-init wrapper around `google.genai.Client`.

    Single API key (no rotation — Gemini's per-key quotas are large enough
    that rotation is unnecessary at v1's expected volume of ≤ 1000 calls/day).
    """

    def __init__(self, daily_budget: int = 1000) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._client: Any = None
        self._types: Any = None
        self._model = _DEFAULT_MODEL
        self.budget = _GeminiBudget(daily_budget)

    def _lazy_init(self) -> None:
        if self._initialized:
            return
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            logger.error(
                "[GeminiClient] GEMINI_API_KEY not set — classifier will fall back to heuristic"
            )
            self._initialized = True
            return
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            logger.error(
                "[GeminiClient] google-genai package not installed. Run: pip install google-genai"
            )
            self._initialized = True
            return
        self._client = genai.Client(api_key=api_key)
        self._types = types
        self._initialized = True
        logger.info("[GeminiClient] initialized (model=%s)", self._model)

    def set_model(self, model: str) -> None:
        with self._lock:
            self._model = model

    def call_json(
        self,
        prompt: str,
        timeout_s: float = 30.0,
    ) -> Optional[Dict[str, Any]]:
        """Issue one Gemini call configured for JSON output.

        Returns parsed dict on success, None on:
          - missing API key
          - daily budget exhausted
          - all retries exhausted
          - response that cannot be parsed as JSON

        The caller (classifier) handles None by falling back to heuristic.
        """
        with self._lock:
            self._lazy_init()
            client = self._client
            types = self._types
            model = self._model

        if client is None or types is None:
            return None

        if not self.budget.try_consume():
            logger.warning("[GeminiClient] daily budget exhausted — returning None")
            return None

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,   # deterministic extraction
        )

        last_exc: Optional[Exception] = None
        for attempt in range(len(_BACKOFF_SECONDS) + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
                text = (response.text or "").strip()
                if not text:
                    logger.warning("[GeminiClient] empty response text")
                    return None
                return _parse_json_lenient(text)
            except Exception as exc:   # noqa: BLE001 — we explicitly classify below
                last_exc = exc
                if not _is_retryable(exc):
                    logger.error("[GeminiClient] non-retryable error: %s", exc)
                    return None
                if attempt < len(_BACKOFF_SECONDS):
                    delay = _BACKOFF_SECONDS[attempt]
                    logger.warning(
                        "[GeminiClient] retryable error (%s) — sleeping %.1fs (attempt %d/%d)",
                        exc, delay, attempt + 1, len(_BACKOFF_SECONDS) + 1,
                    )
                    time.sleep(delay)

        logger.error("[GeminiClient] all retries exhausted: %s", last_exc)
        return None


def _is_retryable(exc: Exception) -> bool:
    """True for transient failures (429, 5xx, network)."""
    s = str(exc).lower()
    if "429" in s or "rate limit" in s or "resource exhausted" in s:
        return True
    if any(code in s for code in ("500", "502", "503", "504")):
        return True
    if any(net in s for net in ("timeout", "timed out", "connection")):
        return True
    return False


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _parse_json_lenient(text: str) -> Optional[Dict[str, Any]]:
    """Strip markdown fences if any, then `json.loads`. Returns None on failure."""
    cleaned = _JSON_FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON object inside the text (model sometimes adds prose).
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if 0 <= first < last:
            try:
                return json.loads(cleaned[first : last + 1])
            except json.JSONDecodeError as e:
                logger.warning("[GeminiClient] JSON parse failed even after slice: %s", e)
                return None
        logger.warning("[GeminiClient] response was not valid JSON")
        return None


# Module-level singleton — single client shared across callers.
_client_singleton: Optional[_GeminiClient] = None
_singleton_lock = threading.Lock()


def get_client(daily_budget: int = 1000, model: Optional[str] = None) -> _GeminiClient:
    """Return the process-wide Gemini client. Creates on first call."""
    global _client_singleton
    with _singleton_lock:
        if _client_singleton is None:
            _client_singleton = _GeminiClient(daily_budget=daily_budget)
        if model:
            _client_singleton.set_model(model)
        return _client_singleton


def call_json(prompt: str, timeout_s: float = 30.0) -> Optional[Dict[str, Any]]:
    """Convenience wrapper: classifier code calls this directly."""
    return get_client().call_json(prompt, timeout_s=timeout_s)
