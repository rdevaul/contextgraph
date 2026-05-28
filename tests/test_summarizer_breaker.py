"""
Tests for the Ollama summarizer circuit-breaker.

Covers:
  - Breaker stays closed under success
  - Breaker trips after N consecutive failures
  - Open breaker short-circuits to fallback (does not call httpx)
  - Breaker auto-resets after cooldown
  - Single success resets the failure counter
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import summarizer  # noqa: E402
from store import Message  # noqa: E402


def _make_msg(text: str = "hello") -> Message:
    return Message(
        id="test-msg",
        session_id="s",
        user_text=text,
        assistant_text=text,
        timestamp=time.time(),
        user_id="u",
        tags=[],
        token_count=10,
    )


@pytest.fixture(autouse=True)
def reset_breaker():
    """Always start every test with a freshly closed breaker."""
    summarizer._ollama_breaker.reset()
    yield
    summarizer._ollama_breaker.reset()


def test_breaker_starts_closed():
    state = summarizer._ollama_breaker.state()
    assert state["tripped"] is False
    assert state["consecutive_failures"] == 0
    assert summarizer._ollama_breaker.allow() is True


def test_breaker_trips_after_threshold_failures():
    threshold = summarizer.OLLAMA_BREAKER_THRESHOLD
    for _ in range(threshold - 1):
        summarizer._ollama_breaker.record_failure()
    assert summarizer._ollama_breaker.state()["tripped"] is False
    # Threshold-th failure trips it
    summarizer._ollama_breaker.record_failure()
    assert summarizer._ollama_breaker.state()["tripped"] is True
    assert summarizer._ollama_breaker.allow() is False


def test_breaker_resets_on_success():
    summarizer._ollama_breaker.record_failure()
    summarizer._ollama_breaker.record_failure()
    summarizer._ollama_breaker.record_success()
    assert summarizer._ollama_breaker.state()["consecutive_failures"] == 0


def test_open_breaker_short_circuits_to_fallback():
    """When breaker is open, _summarize_ollama must NOT call httpx."""
    # Trip the breaker
    for _ in range(summarizer.OLLAMA_BREAKER_THRESHOLD):
        summarizer._ollama_breaker.record_failure()
    assert summarizer._ollama_breaker.allow() is False

    msg = _make_msg("short")
    with patch("httpx.Client") as mock_client:
        result = summarizer._summarize_ollama(msg)
        # httpx must not have been touched
        mock_client.assert_not_called()
    # And we get the fallback truncation
    assert "short" in result


def test_summarize_ollama_records_failure_on_exception():
    """A raised exception inside the httpx block should trip the breaker counter."""
    msg = _make_msg()
    import httpx as _httpx

    class _RaisingClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            raise _httpx.ConnectTimeout("boom")

    with patch("httpx.Client", _RaisingClient):
        # Trip threshold consecutive failures
        for _ in range(summarizer.OLLAMA_BREAKER_THRESHOLD):
            summarizer._summarize_ollama(msg)
    assert summarizer._ollama_breaker.state()["tripped"] is True


def test_summarize_ollama_records_failure_on_empty_response():
    """200 OK with empty response is a soft failure (the silent-wedge symptom)."""
    msg = _make_msg()

    class _EmptyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": ""}

    class _EmptyClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return _EmptyResponse()

    with patch("httpx.Client", _EmptyClient):
        for _ in range(summarizer.OLLAMA_BREAKER_THRESHOLD):
            result = summarizer._summarize_ollama(msg)
            # Returns fallback (truncation)
            assert "hello" in result
    assert summarizer._ollama_breaker.state()["tripped"] is True


def test_breaker_cooldown_auto_reset(monkeypatch):
    """After cooldown elapses, breaker allows one probe (half-open)."""
    # Trip it
    for _ in range(summarizer.OLLAMA_BREAKER_THRESHOLD):
        summarizer._ollama_breaker.record_failure()
    assert summarizer._ollama_breaker.allow() is False

    # Fast-forward time past the cooldown
    real_monotonic = time.monotonic
    cooldown = summarizer.OLLAMA_BREAKER_COOLDOWN_SEC
    fake_now = [real_monotonic() + cooldown + 1.0]
    monkeypatch.setattr(summarizer.time, "monotonic", lambda: fake_now[0])

    # First allow() after cooldown returns True (probe)
    assert summarizer._ollama_breaker.allow() is True
    # State is now half-open: tripped_at cleared, counter just below threshold
    state = summarizer._ollama_breaker.state()
    assert state["tripped"] is False
    assert state["consecutive_failures"] == summarizer.OLLAMA_BREAKER_THRESHOLD - 1
