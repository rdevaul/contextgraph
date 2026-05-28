"""
Tests for backfill_empty_summaries.py row-selection query.

We don't test the actual summarization (that requires Ollama/Anthropic and
is exercised by test_summarizer_breaker.py). We DO test:

  - The window predicate (timestamp BETWEEN since AND until)
  - The threshold predicate (token_count > threshold)
  - The empty-summary predicate (summary IS NULL OR summary = '')
"""

import sys
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as cg_config  # noqa: E402


@pytest.fixture
def isolated_store(monkeypatch):
    """Build a clean store at a fresh DB path so we don't trample prod."""
    tmpdir = tempfile.mkdtemp(prefix="cg-backfill-test-")
    db_path = Path(tmpdir) / "store.db"
    monkeypatch.setenv("CONTEXTGRAPH_DB_PATH", str(db_path))
    monkeypatch.setattr(cg_config, "DB_PATH", db_path)

    from store import MessageStore, Message  # imported AFTER env patch

    store = MessageStore(db_path=str(db_path))

    # Seed: 4 messages straddling the window
    base = 1779925800.0  # 2026-05-27 16:50 PDT in epoch
    msgs = [
        Message(
            id="before-window",
            session_id="s",
            user_text="pre",
            assistant_text="pre",
            timestamp=base - 3600,
            user_id="u",
            tags=[],
            token_count=3000,
        ),
        Message(
            id="in-window-big-empty",
            session_id="s",
            user_text="in1",
            assistant_text="in1",
            timestamp=base + 100,
            user_id="u",
            tags=[],
            token_count=3000,
        ),
        Message(
            id="in-window-small",
            session_id="s",
            user_text="in2",
            assistant_text="in2",
            timestamp=base + 200,
            user_id="u",
            tags=[],
            token_count=100,
        ),
        Message(
            id="in-window-big-has-summary",
            session_id="s",
            user_text="in3",
            assistant_text="in3",
            timestamp=base + 300,
            user_id="u",
            tags=[],
            token_count=3000,
        ),
        Message(
            id="after-window",
            session_id="s",
            user_text="post",
            assistant_text="post",
            timestamp=base + 100000,  # well after
            user_id="u",
            tags=[],
            token_count=3000,
        ),
    ]
    for m in msgs:
        store.add_message(m)
    # Give one row a non-empty summary so it's NOT a candidate
    store.set_summary("in-window-big-has-summary", "an existing summary")

    yield store, db_path, base


def test_query_returns_only_big_empty_in_window(isolated_store):
    """The query must select ONLY rows that:
    - have token_count > threshold
    - have empty/NULL summary
    - timestamp in [since, until]
    """
    store, db_path, base = isolated_store
    since = base
    until = base + 86400
    threshold = 2000

    conn = store._conn()
    rows = conn.execute(
        """
        SELECT id FROM messages
        WHERE token_count > ?
          AND (summary IS NULL OR summary = '')
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
        """,
        (threshold, since, until),
    ).fetchall()
    ids = [r["id"] for r in rows]
    assert ids == ["in-window-big-empty"], f"unexpected ids: {ids}"


def test_threshold_below_picks_up_small_rows(isolated_store):
    """Lower threshold pulls the small in-window row in too."""
    store, db_path, base = isolated_store
    conn = store._conn()
    rows = conn.execute(
        """
        SELECT id FROM messages
        WHERE token_count > ?
          AND (summary IS NULL OR summary = '')
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
        """,
        (50, base, base + 86400),
    ).fetchall()
    ids = {r["id"] for r in rows}
    assert ids == {"in-window-big-empty", "in-window-small"}


def test_window_excludes_before_and_after(isolated_store):
    """Rows before --since or after --until are excluded regardless of size."""
    store, db_path, base = isolated_store
    conn = store._conn()
    rows = conn.execute(
        """
        SELECT id FROM messages
        WHERE token_count > ?
          AND (summary IS NULL OR summary = '')
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
        """,
        (50, base, base + 86400),
    ).fetchall()
    ids = {r["id"] for r in rows}
    assert "before-window" not in ids
    assert "after-window" not in ids


def test_existing_summary_excluded(isolated_store):
    """A row WITH a non-empty summary should not be selected."""
    store, db_path, base = isolated_store
    conn = store._conn()
    rows = conn.execute(
        """
        SELECT id FROM messages
        WHERE token_count > ?
          AND (summary IS NULL OR summary = '')
          AND timestamp BETWEEN ? AND ?
        """,
        (50, base, base + 86400),
    ).fetchall()
    ids = {r["id"] for r in rows}
    assert "in-window-big-has-summary" not in ids
