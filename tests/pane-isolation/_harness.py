"""
Shared test harness for pane-isolation tests.

Builds a fresh MessageStore against a temp SQLite DB so each test starts clean.
Provides helpers for inserting messages with controllable channel_label,
session_id, and tags.

References:
- Bus approval: 20260501220916-a4feb6f0
- Handoff:      HANDOFF-2026-05-01-rich.md
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from typing import Optional

# Make repo root importable when running test files directly.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from store import Message, MessageStore  # noqa: E402
from assembler import ContextAssembler  # noqa: E402


def fresh_store() -> tuple[MessageStore, str]:
    """Create a fresh MessageStore against a temp SQLite DB.

    Returns (store, db_path) so callers can clean up if they want.
    """
    fd, db_path = tempfile.mkstemp(prefix="ctxgraph-test-", suffix=".db")
    os.close(fd)
    # Remove the empty file so MessageStore initializes its own schema cleanly.
    os.unlink(db_path)
    store = MessageStore(db_path=db_path)
    return store, db_path


def insert(
    store: MessageStore,
    *,
    msg_id: str,
    user_text: str,
    assistant_text: str,
    tags: list[str],
    channel_label: Optional[str] = None,
    session_id: Optional[str] = None,
    timestamp: Optional[float] = None,
    is_automated: bool = False,
) -> Message:
    """Insert a fully-specified message and return it."""
    msg = Message(
        id=msg_id,
        session_id=session_id or f"session-{msg_id}",
        user_id="test-user",
        timestamp=timestamp if timestamp is not None else time.time(),
        user_text=user_text,
        assistant_text=assistant_text,
        tags=list(tags),
        token_count=max(1, len((user_text + assistant_text).split())),
        external_id=msg_id,
        is_automated=is_automated,
        channel_label=channel_label,
    )
    store.add_message(msg)
    return msg


def make_assembler(store: MessageStore, token_budget: int = 4000) -> ContextAssembler:
    """Construct an assembler with a token budget large enough for tests."""
    return ContextAssembler(store, token_budget=token_budget)
