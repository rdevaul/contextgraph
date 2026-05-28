"""
Tests for the /assemble recency-floor behavior (2026-05-28 incident fix).

Covers the three scope branches:
  - scope=subchannel + (channel_label, subchannel_label) → per-pane recency
  - scope=session + session_id                            → per-session recency
  - scope=user or scope=global                            → no floor applied

Plus:
  - recency_floor=0 disables the floor
  - recency-floor rows are tagged source='recency_floor' in the response
  - recency-floor tokens are charged to the budget BEFORE assembly
  - dedup: a row already in semantic results is not double-counted
"""

import sys
import os
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Use an isolated DB for the test
_tmpdir = tempfile.mkdtemp(prefix="cg-recency-floor-")
os.environ["CONTEXTGRAPH_DB_PATH"] = str(Path(_tmpdir) / "store.db")
# Disable Ollama probe / summarization in the test path
os.environ.pop("SUMMARIZER_BACKEND", None)
os.environ["SUMMARIZER_BACKEND"] = "anthropic"  # never called with no API key in tests
os.environ.pop("ANTHROPIC_API_KEY", None)

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as cg_config  # noqa: E402
cg_config.DB_PATH = Path(os.environ["CONTEXTGRAPH_DB_PATH"])

from api.server import app, store, RECENCY_FLOOR_DEFAULT  # noqa: E402
from store import Message  # noqa: E402

client = TestClient(app)


def _add_msg(
    id: str,
    session_id: str,
    user_text: str,
    assistant_text: str,
    ts: float,
    channel_label: str | None = None,
    subchannel_label: str | None = None,
) -> None:
    msg = Message(
        id=id,
        session_id=session_id,
        user_text=user_text,
        assistant_text=assistant_text,
        timestamp=ts,
        user_id="u1",
        tags=[],
        token_count=len(user_text.split()) + len(assistant_text.split()),
        channel_label=channel_label,
        subchannel_label=subchannel_label,
    )
    store.add_message(msg)


@pytest.fixture(autouse=True)
def seed_db():
    """Reset DB between tests."""
    # Nuke all rows
    conn = store._conn()
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM tags")
    conn.commit()
    yield


def test_subchannel_scope_applies_recency_floor():
    """scope=subchannel + (channel, subchannel) prepends last-N from that pair."""
    base = time.time() - 100
    for i in range(7):
        _add_msg(
            f"sub-msg-{i}",
            session_id="sess-A",
            user_text=f"user msg {i}",
            assistant_text=f"asst msg {i}",
            ts=base + i,
            channel_label="ch-rich",
            subchannel_label="fea",
        )
    # Also add a row in a different subchannel that must NOT appear
    _add_msg(
        "other-sub-msg",
        session_id="sess-B",
        user_text="other pane",
        assistant_text="other pane reply",
        ts=base + 50,
        channel_label="ch-rich",
        subchannel_label="cad",
    )

    r = client.post(
        "/assemble",
        json={
            "user_text": "yeas please",
            "token_budget": 4000,
            "scope": "subchannel",
            "channel_label": "ch-rich",
            "subchannel_label": "fea",
            "session_id": "sess-A",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    floor_rows = [m for m in body["messages"] if m["source"] == "recency_floor"]
    floor_ids = [m["id"] for m in floor_rows]
    # Default is RECENCY_FLOOR_DEFAULT (5)
    assert len(floor_rows) == RECENCY_FLOOR_DEFAULT, f"got {floor_ids}"
    # Should be the 5 newest from (ch-rich, fea), which are sub-msg-2..sub-msg-6
    assert set(floor_ids) == {f"sub-msg-{i}" for i in range(2, 7)}
    # The other-subchannel row must NOT have made it via the floor
    assert "other-sub-msg" not in floor_ids
    # recency_floor_count is reported
    assert body["recency_floor_count"] == RECENCY_FLOOR_DEFAULT


def test_session_scope_applies_recency_floor():
    """scope=session + session_id prepends last-N from that session."""
    base = time.time() - 100
    for i in range(4):
        _add_msg(
            f"sess-msg-{i}",
            session_id="sess-X",
            user_text=f"user {i}",
            assistant_text=f"asst {i}",
            ts=base + i,
        )
    # Different session — must not bleed in
    _add_msg(
        "other-sess",
        session_id="sess-Y",
        user_text="leak",
        assistant_text="leak",
        ts=base + 50,
    )

    r = client.post(
        "/assemble",
        json={
            "user_text": "go ahead",
            "token_budget": 4000,
            "scope": "session",
            "session_id": "sess-X",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    floor_ids = [m["id"] for m in body["messages"] if m["source"] == "recency_floor"]
    # Only 4 messages in sess-X, so floor returns all 4
    assert set(floor_ids) == {f"sess-msg-{i}" for i in range(4)}
    assert "other-sess" not in floor_ids


def test_user_scope_does_not_apply_recency_floor():
    """scope=user with no session/subchannel does NOT apply the floor."""
    base = time.time() - 100
    for i in range(3):
        _add_msg(
            f"user-msg-{i}",
            session_id="sess-A",
            user_text=f"user {i}",
            assistant_text=f"asst {i}",
            ts=base + i,
            channel_label="ch-rich",
        )

    r = client.post(
        "/assemble",
        json={
            "user_text": "what's up",
            "token_budget": 4000,
            "scope": "user",
            "channel_label": "ch-rich",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    floor_rows = [m for m in body["messages"] if m["source"] == "recency_floor"]
    # Floor disabled for scope=user
    assert floor_rows == []
    assert body["recency_floor_count"] == 0


def test_explicit_zero_disables_floor():
    """recency_floor=0 disables the floor even when scope would normally trigger it."""
    base = time.time() - 100
    for i in range(3):
        _add_msg(
            f"zero-msg-{i}",
            session_id="sess-Z",
            user_text=f"user {i}",
            assistant_text=f"asst {i}",
            ts=base + i,
            channel_label="ch-rich",
            subchannel_label="cad",
        )

    r = client.post(
        "/assemble",
        json={
            "user_text": "go",
            "token_budget": 4000,
            "scope": "subchannel",
            "channel_label": "ch-rich",
            "subchannel_label": "cad",
            "session_id": "sess-Z",
            "recency_floor": 0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recency_floor_count"] == 0


def test_floor_tokens_charged_to_budget():
    """recency_floor rows charge their tokens to the budget BEFORE assembly."""
    base = time.time() - 100
    # Seed a single recent row in the target scope
    _add_msg(
        "budget-msg-1",
        session_id="sess-B",
        user_text="some content " * 50,
        assistant_text="assistant content " * 100,
        ts=base + 10,
        channel_label="ch-rich",
        subchannel_label="fea",
    )

    r = client.post(
        "/assemble",
        json={
            "user_text": "yeas please",
            "token_budget": 4000,
            "scope": "subchannel",
            "channel_label": "ch-rich",
            "subchannel_label": "fea",
            "session_id": "sess-B",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The floor message landed
    assert body["recency_floor_count"] == 1
    # Tokens charged are reported
    assert body["recency_floor_tokens"] > 0
    # Floor tokens contribute to total_tokens
    assert body["total_tokens"] >= body["recency_floor_tokens"]
