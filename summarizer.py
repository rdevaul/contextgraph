"""
summarizer.py — On-demand summarization of large messages for context assembly.

Configurable backend:
  - anthropic (default): claude-haiku-4-5 via API
  - ollama: local model (e.g. qwen2.5:7b) via http://localhost:11434/api/generate

Environment variables:
  SUMMARIZER_BACKEND=anthropic|ollama  (default: anthropic)
  SUMMARIZER_MODEL=<model name>        (default: claude-haiku-4-5 or qwen2.5:7b)
  ANTHROPIC_API_KEY=<key>              (required for anthropic backend)
  OLLAMA_URL=http://localhost:11434    (default for ollama)

Ollama socket hygiene (2026-05-28 incident fix):
  The previous `requests`-based implementation wedged silently for 17h on
  stale HTTP connection state — every call hit a 60s read timeout, HTTP
  layer still returned 200 (via fallback truncation), summary field landed
  empty. Replaced with httpx using short-lived clients per call, explicit
  Connection: close, and a circuit-breaker that bypasses Ollama for 5 min
  after 5 consecutive failures.
"""

import os
import time
import logging
import threading
from typing import Optional
from store import Message

logger = logging.getLogger(__name__)

# Configuration
SUMMARIZER_BACKEND = os.getenv("SUMMARIZER_BACKEND", "anthropic")
SUMMARIZER_MODEL = os.getenv("SUMMARIZER_MODEL", None)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", None)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# Set default models based on backend
if SUMMARIZER_MODEL is None:
    if SUMMARIZER_BACKEND == "anthropic":
        SUMMARIZER_MODEL = "claude-haiku-4-5"
    else:
        SUMMARIZER_MODEL = "qwen2.5:7b"

SUMMARIZATION_PROMPT = """Summarize this conversation exchange in ≤300 words. Preserve: key decisions made, file names/paths mentioned, errors and their resolutions, commands run, outcomes. Be concrete and specific.

USER:
{user_text}

ASSISTANT:
{assistant_text}"""


def _fallback_truncation(msg: Message) -> str:
    """Simple truncation fallback if summarization fails."""
    user_preview = msg.user_text[:200] if msg.user_text else ""
    assistant_preview = msg.assistant_text[:500] if msg.assistant_text else ""
    return f"{user_preview} | {assistant_preview}"


def _summarize_anthropic(msg: Message) -> str:
    """Summarize using Anthropic Claude API.

    Connection hygiene (2026-07-21 incident fix):
      The original implementation built an anthropic.Anthropic() client per
      call but relied on the SDK's default pooled httpx transport with
      keepalive ON and NO explicit timeout. In a 20-day-lived daemon that
      pool accumulated a dead keepalive socket -> every call raised
      "Connection error" -> summaries silently landed empty for ~2 weeks
      (Jul 7 onward). This mirrors the Ollama wedge fixed in May, which was
      only ever patched on the Ollama path, never the (now-default) anthropic
      path.

      Fixes applied here, matching _summarize_ollama:
        - Fresh client per call with an explicit non-keepalive httpx transport
          (Connection: close, no pooled sockets to go stale).
        - Explicit connect/read timeouts so a wedged socket fails fast.
        - SDK-level retries disabled (max_retries=0); the circuit breaker
          owns retry/backoff policy.
        - Shared circuit breaker: after N consecutive failures, bypass the
          API for the cooldown window and serve fallback truncation directly.
    """
    try:
        import anthropic
        import httpx
    except ImportError:
        logger.error("anthropic/httpx not installed; install with: pip install 'anthropic>=0.40' httpx")
        return _fallback_truncation(msg)

    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set; cannot use anthropic backend")
        return _fallback_truncation(msg)

    # Circuit breaker: skip the API entirely during a sustained outage so we
    # don't absorb N * connect-timeout of latency per message.
    if not _summarizer_breaker.allow():
        logger.debug(
            "Summarizer circuit breaker open; using fallback truncation for msg.id=%s",
            getattr(msg, "id", "?"),
        )
        return _fallback_truncation(msg)

    prompt_text = SUMMARIZATION_PROMPT.format(
        user_text=msg.user_text,
        assistant_text=msg.assistant_text,
    )

    # Short-lived, non-pooled HTTP transport. keepalive_expiry=0 + explicit
    # Connection: close means no socket survives the call to go stale.
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
    limits = httpx.Limits(
        max_connections=1,
        max_keepalive_connections=0,
        keepalive_expiry=0.0,
    )
    http_client = httpx.Client(
        timeout=timeout,
        limits=limits,
        headers={"Connection": "close"},
    )
    try:
        # IMPORTANT (2026-07-21): the anthropic SDK does NOT inherit the read
        # timeout from a custom http_client for its request-level timeout —
        # that param defaults to NOT_GIVEN, so a hung TLS read can block
        # forever (observed: backfill wedged in _ssl__SSLSocket_read with no
        # timeout firing despite the httpx client's read=30). We MUST set
        # timeout= on the SDK itself, and again per-request (belt & braces),
        # so a stalled socket actually raises.
        client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            http_client=http_client,
            max_retries=0,
            timeout=timeout,
        )
        response = client.messages.create(
            model=SUMMARIZER_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt_text}],
            timeout=timeout,
        )
        summary = response.content[0].text if response.content else ""
        if summary and summary.strip():
            _summarizer_breaker.record_success()
            return summary.strip()
        logger.warning(
            "Anthropic returned empty content for msg.id=%s; treating as failure",
            getattr(msg, "id", "?"),
        )
        _summarizer_breaker.record_failure()
        return _fallback_truncation(msg)
    except Exception as e:
        logger.error(f"Anthropic summarization failed: {e}")
        _summarizer_breaker.record_failure()
        return _fallback_truncation(msg)
    finally:
        try:
            http_client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Circuit breaker for Ollama summarization
# ---------------------------------------------------------------------------
# After 5 consecutive failures, bypass Ollama for 5 min and serve truncation
# directly. This caps the worst-case latency contribution of a wedged Ollama
# (was 60s/call * N calls = unbounded; now 0s/call for the cooldown window).

OLLAMA_BREAKER_THRESHOLD = int(os.getenv("OLLAMA_BREAKER_THRESHOLD", "5"))
OLLAMA_BREAKER_COOLDOWN_SEC = float(os.getenv("OLLAMA_BREAKER_COOLDOWN_SEC", "300"))


class _OllamaCircuitBreaker:
    """Thread-safe circuit-breaker. Trips after N consecutive failures.

    States:
      - closed:  normal operation, calls pass through.
      - open:    skip the call, return fallback. Auto-resets after cooldown.
    A single successful call resets the failure counter.
    """

    def __init__(self, threshold: int, cooldown_sec: float) -> None:
        self.threshold = threshold
        self.cooldown_sec = cooldown_sec
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._tripped_at: Optional[float] = None

    def allow(self) -> bool:
        """Return True if the call should proceed, False if breaker is open."""
        with self._lock:
            if self._tripped_at is None:
                return True
            elapsed = time.monotonic() - self._tripped_at
            if elapsed >= self.cooldown_sec:
                # Half-open: allow one probe to see if Ollama is back.
                # If it fails, record_failure() trips us again immediately.
                # If it succeeds, record_success() resets fully.
                logger.info(
                    "Ollama circuit breaker: cooldown expired (%.0fs), allowing probe",
                    elapsed,
                )
                self._tripped_at = None
                self._consecutive_failures = self.threshold - 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._consecutive_failures > 0 or self._tripped_at is not None:
                logger.info("Ollama circuit breaker: success, resetting counter")
            self._consecutive_failures = 0
            self._tripped_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.threshold and self._tripped_at is None:
                self._tripped_at = time.monotonic()
                logger.error(
                    "Ollama circuit breaker TRIPPED after %d consecutive failures; "
                    "bypassing for %.0fs",
                    self._consecutive_failures,
                    self.cooldown_sec,
                )

    def state(self) -> dict:
        """Introspection hook for tests + diagnostics."""
        with self._lock:
            return {
                "consecutive_failures": self._consecutive_failures,
                "tripped": self._tripped_at is not None,
                "cooldown_remaining": (
                    max(0.0, self.cooldown_sec - (time.monotonic() - self._tripped_at))
                    if self._tripped_at is not None
                    else 0.0
                ),
            }

    def reset(self) -> None:
        """Test-only: forcibly reset to closed state."""
        with self._lock:
            self._consecutive_failures = 0
            self._tripped_at = None


# Shared circuit breaker for BOTH summarizer backends (2026-07-21). The class
# was originally Ollama-specific but is backend-agnostic; a single breaker now
# protects whichever backend is active (anthropic default, or ollama).
_summarizer_breaker = _OllamaCircuitBreaker(
    threshold=OLLAMA_BREAKER_THRESHOLD,
    cooldown_sec=OLLAMA_BREAKER_COOLDOWN_SEC,
)

# Backward-compatible alias: existing Ollama path + any tests reference
# `_ollama_breaker`. Keep it pointing at the same shared instance.
_ollama_breaker = _summarizer_breaker


def _summarize_ollama(msg: Message) -> str:
    """Summarize using Ollama local API via httpx.

    Connection hygiene:
      - Short-lived httpx.Client per call (no keepalive pool reuse)
      - Explicit (connect=5s, read=30s) timeouts
      - Explicit Connection: close header so the server tears the socket
      - Circuit-breaker short-circuits to fallback after consecutive failures

    The previous `requests`-based implementation reused urllib3's default
    connection pool, which held a dead keepalive socket through DNS / TCP
    state changes on the network. The wedge was silent: HTTP layer returned
    200 via the fallback path, but `summary` field landed empty.
    """
    try:
        import httpx
    except ImportError:
        logger.error("httpx package not installed; install with: pip install httpx")
        return _fallback_truncation(msg)

    # Circuit-breaker: if Ollama has been consistently failing, skip it
    # entirely for the cooldown window. Saves us from absorbing 5+ * 30s
    # of timeout wait per message during an outage.
    if not _ollama_breaker.allow():
        logger.debug(
            "Ollama circuit breaker open; using fallback truncation for msg.id=%s",
            getattr(msg, "id", "?"),
        )
        return _fallback_truncation(msg)

    prompt_text = SUMMARIZATION_PROMPT.format(
        user_text=msg.user_text,
        assistant_text=msg.assistant_text,
    )
    payload = {
        "model": SUMMARIZER_MODEL,
        "prompt": prompt_text,
        "stream": False,
    }

    # Short-lived client per call. No keepalive pool reuse — each call
    # opens a fresh socket and closes it on exit. Slightly higher per-call
    # cost (one TCP+HTTP handshake, ~5-20ms on local network) for the cost
    # of correctness in long-lived daemons.
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
    limits = httpx.Limits(
        max_connections=1,
        max_keepalive_connections=0,
        keepalive_expiry=0.0,
    )
    try:
        with httpx.Client(timeout=timeout, limits=limits) as client:
            response = client.post(
                f"{OLLAMA_URL}/api/generate",
                json=payload,
                headers={"Connection": "close"},
            )
            response.raise_for_status()
            result = response.json()
            summary = result.get("response", "")
            if summary and summary.strip():
                _ollama_breaker.record_success()
                return summary.strip()
            # 200 with empty response is a soft failure: the model returned
            # nothing usable. Count it against the breaker (this is exactly
            # the silent-wedge symptom we're guarding against) and fall back.
            logger.warning(
                "Ollama returned empty 'response' field for msg.id=%s; treating as failure",
                getattr(msg, "id", "?"),
            )
            _ollama_breaker.record_failure()
            return _fallback_truncation(msg)
    except Exception as e:
        logger.error(f"Ollama summarization failed: {e}")
        _ollama_breaker.record_failure()
        return _fallback_truncation(msg)


def summarize_message(msg: Message) -> str:
    """
    Summarize a message using the configured backend.

    Args:
        msg: Message object to summarize

    Returns:
        Summary string (≤400 words typically)
    """
    if SUMMARIZER_BACKEND == "anthropic":
        return _summarize_anthropic(msg)
    elif SUMMARIZER_BACKEND == "ollama":
        return _summarize_ollama(msg)
    else:
        logger.warning(f"Unknown SUMMARIZER_BACKEND: {SUMMARIZER_BACKEND}; using fallback")
        return _fallback_truncation(msg)
