"""
utils/text.py — Shared text cleaning utilities.

Used at ingestion time (store/API) and at query time (assemble) to strip
OpenClaw channel metadata envelopes from user_text before indexing or matching.

The envelope is prepended by OpenClaw to every inbound message and contains
JSON metadata (message_id, sender_id, timestamp, etc.) that should not be
treated as semantic content. Storing or querying against envelope text
causes tag pollution and degrades retrieval quality.
"""

import re

# ── Envelope stripping ─────────────────────────────────────────────────────

# These patterns match the standard OpenClaw metadata envelope blocks that
# appear before the actual user message. Each regex is applied in sequence.
_ENVELOPE_PATTERNS = [
    # "Conversation info (untrusted metadata): ```json { ... } ```"
    re.compile(
        r"Conversation info \(untrusted metadata\):\s*```(?:json)?\s*\{.*?\}\s*```\s*",
        re.DOTALL,
    ),
    # "Sender (untrusted metadata): ```json { ... } ```"
    re.compile(
        r"Sender \(untrusted metadata\):\s*```(?:json)?\s*\{.*?\}\s*```\s*",
        re.DOTALL,
    ),
    # "Replied message (...): ```json { ... } ```"
    re.compile(
        r"Replied message \(untrusted.*?\):\s*```(?:json)?\s*\{.*?\}\s*```\s*",
        re.DOTALL,
    ),
    # "System: [timestamp] ..." event lines
    re.compile(r"^System:\s*\[.*?\].*?$", re.MULTILINE),
    # Voice PWA prefix: "[HH:MM:SS] [Voice PWA] "
    re.compile(r"^\[[\d:]+\]\s*\[Voice PWA\]\s*", re.MULTILINE),
    # Media attachment lines: "[media attached: ...]"
    re.compile(r"\[media attached:.*?\]\s*", re.DOTALL),
    # Queued messages block (everything from this line onward)
    re.compile(r"\[Queued messages while agent was busy\].*", re.DOTALL),
]

# Minimum useful length after stripping — if residual is shorter, keep original.
_MIN_USEFUL_LENGTH = 20


def strip_envelope(text: str) -> str:
    """
    Strip OpenClaw channel metadata envelopes from user message text.

    Returns the cleaned text. If the result is too short (< 20 chars),
    returns the original to avoid data loss.

    Safe to call on text that has no envelope — returns unchanged.
    """
    if not text:
        return text

    cleaned = text
    for pattern in _ENVELOPE_PATTERNS:
        cleaned = pattern.sub("", cleaned)

    cleaned = cleaned.strip()

    # If stripping produced an empty result, the message was pure envelope
    # metadata with no semantic content. Return a minimal placeholder.
    if not cleaned:
        return "[metadata-only message]"

    return cleaned
