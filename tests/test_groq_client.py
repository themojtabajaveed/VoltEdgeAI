"""
test_groq_client.py — Unit tests for Step 9: Groq dual-key rotation + 429 backoff.

Tests are isolated from real Groq API by injecting mock client instances
directly into fresh _GroqKeyRotator instances. No network calls are made.
"""
import time
import pytest
from unittest.mock import MagicMock, patch

from src.llm.groq_client import _GroqKeyRotator


# ── helpers ──────────────────────────────────────────────────────────────────

class _FakeRateLimitError(Exception):
    """Simulates groq.RateLimitError (identified by class name)."""
    def __init__(self, msg: str = "Rate limit exceeded"):
        super().__init__(msg)
        self.status_code = 429


class _FakeOtherError(Exception):
    """Non-429 Groq error (network, auth, etc.)."""


def _make_client(response=None, side_effect=None) -> MagicMock:
    """Return a mock Groq client whose .chat.completions.create behaves as specified."""
    client = MagicMock()
    if side_effect is not None:
        client.chat.completions.create.side_effect = side_effect
    else:
        client.chat.completions.create.return_value = response
    return client


def _fresh_rotator(*clients) -> _GroqKeyRotator:
    """Build a _GroqKeyRotator with pre-injected mock clients (bypasses env var reads)."""
    r = _GroqKeyRotator()
    r._initialized = True          # skip _lazy_init
    r._keys = [f"fake_key_{i}" for i in range(len(clients))]
    r._clients = list(clients)
    r._current_idx = 0
    return r


# ── Step 9 tests ──────────────────────────────────────────────────────────────

class TestGroqKeyRotator:

    def test_single_key_returns_response(self):
        """Single key, no 429 — returns the completion response directly."""
        expected = MagicMock()
        rotator = _fresh_rotator(_make_client(response=expected))
        result = rotator.call(messages=[{"role": "user", "content": "hi"}])
        assert result is expected

    def test_single_key_non_429_raises_immediately(self):
        """Non-429 errors are re-raised without retrying."""
        rotator = _fresh_rotator(_make_client(side_effect=_FakeOtherError("auth fail")))
        with pytest.raises(_FakeOtherError):
            rotator.call(messages=[{"role": "user", "content": "hi"}])
        # Must have been called only once — no retry
        assert rotator._clients[0].chat.completions.create.call_count == 1

    def test_dual_key_switches_on_429(self):
        """429 on key 0 → next request uses key 1 and succeeds."""
        good_response = MagicMock()
        client_0 = _make_client(side_effect=_FakeRateLimitError())
        client_1 = _make_client(response=good_response)
        rotator = _fresh_rotator(client_0, client_1)

        result = rotator.call(messages=[{"role": "user", "content": "hi"}])

        assert result is good_response
        assert client_0.chat.completions.create.call_count == 1, "key 0 tried once"
        assert client_1.chat.completions.create.call_count == 1, "key 1 tried once"

    def test_dual_key_backoff_fires_on_double_429(self):
        """Both keys return 429 in the first round — backoff sleep is called."""
        client_0 = _make_client(side_effect=_FakeRateLimitError())
        client_1 = _make_client(side_effect=_FakeRateLimitError())
        rotator = _fresh_rotator(client_0, client_1)

        with patch("src.llm.groq_client.time.sleep") as mock_sleep:
            with pytest.raises(_FakeRateLimitError):
                rotator.call(messages=[{"role": "user", "content": "hi"}])

        # At least one sleep must have been called (backoff between rounds)
        assert mock_sleep.call_count >= 1
        # All sleep delays must be from the configured backoff sequence (>= 1s)
        for call in mock_sleep.call_args_list:
            assert call.args[0] >= 1.0

    def test_exhausted_retries_raise_429(self):
        """After _MAX_BACKOFF_ROUNDS all exhausted, the 429 is raised to the caller."""
        client_0 = _make_client(side_effect=_FakeRateLimitError("key0 limit"))
        client_1 = _make_client(side_effect=_FakeRateLimitError("key1 limit"))
        rotator = _fresh_rotator(client_0, client_1)

        with patch("src.llm.groq_client.time.sleep"):
            with pytest.raises(_FakeRateLimitError):
                rotator.call(messages=[{"role": "user", "content": "hi"}])

    def test_is_429_detects_rate_limit_error_class(self):
        """_is_429 identifies groq.RateLimitError by class name."""
        exc = _FakeRateLimitError()
        assert _GroqKeyRotator._is_429(exc) is True

    def test_is_429_detects_status_code_attr(self):
        """_is_429 identifies 429 via status_code attribute."""
        exc = Exception("some error")
        exc.status_code = 429                           # type: ignore[attr-defined]
        assert _GroqKeyRotator._is_429(exc) is True

    def test_is_429_detects_429_in_message(self):
        """_is_429 identifies 429 via string in the exception message."""
        exc = Exception("HTTP 429: Too Many Requests")
        assert _GroqKeyRotator._is_429(exc) is True

    def test_is_429_false_for_non_429(self):
        """_is_429 returns False for an unrelated exception."""
        exc = Exception("Connection refused")
        assert _GroqKeyRotator._is_429(exc) is False

    def test_round_robin_advances_key_index(self):
        """Each successful call advances the key index for the next call."""
        resp_a = MagicMock()
        resp_b = MagicMock()
        client_0 = _make_client(response=resp_a)
        client_1 = _make_client(response=resp_b)
        rotator = _fresh_rotator(client_0, client_1)

        r1 = rotator.call(messages=[{"role": "user", "content": "first"}])
        r2 = rotator.call(messages=[{"role": "user", "content": "second"}])

        # Key 0 handles call 1, key 1 handles call 2 (round-robin)
        assert r1 is resp_a
        assert r2 is resp_b

    def test_second_key_success_after_first_429_no_extra_sleep(self):
        """On first 429 → second key succeeds: exactly one immediate switch, no sleep."""
        good_response = MagicMock()
        client_0 = _make_client(side_effect=_FakeRateLimitError())
        client_1 = _make_client(response=good_response)
        rotator = _fresh_rotator(client_0, client_1)

        with patch("src.llm.groq_client.time.sleep") as mock_sleep:
            result = rotator.call(messages=[{"role": "user", "content": "hi"}])

        assert result is good_response
        # No sleep needed when the alternate key succeeds immediately on first 429
        assert mock_sleep.call_count == 0
