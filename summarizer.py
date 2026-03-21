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
"""

import os
import logging
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
    """Summarize using Anthropic Claude API."""
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic package not installed; install with: pip install anthropic>=0.40")
        return _fallback_truncation(msg)

    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set; cannot use anthropic backend")
        return _fallback_truncation(msg)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt_text = SUMMARIZATION_PROMPT.format(
            user_text=msg.user_text,
            assistant_text=msg.assistant_text
        )
        response = client.messages.create(
            model=SUMMARIZER_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt_text}]
        )
        # Extract text from response
        summary = response.content[0].text if response.content else ""
        return summary.strip() if summary else _fallback_truncation(msg)
    except Exception as e:
        logger.error(f"Anthropic summarization failed: {e}")
        return _fallback_truncation(msg)


def _summarize_ollama(msg: Message) -> str:
    """Summarize using Ollama local API."""
    import json
    try:
        import requests
    except ImportError:
        logger.error("requests package not installed; install with: pip install requests")
        return _fallback_truncation(msg)

    try:
        prompt_text = SUMMARIZATION_PROMPT.format(
            user_text=msg.user_text,
            assistant_text=msg.assistant_text
        )
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": SUMMARIZER_MODEL,
                "prompt": prompt_text,
                "stream": False
            },
            timeout=60
        )
        response.raise_for_status()
        result = response.json()
        summary = result.get("response", "")
        return summary.strip() if summary else _fallback_truncation(msg)
    except Exception as e:
        logger.error(f"Ollama summarization failed: {e}")
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
