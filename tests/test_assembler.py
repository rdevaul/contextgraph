"""Tests for assembler.py"""
import time
import tempfile
import pytest

from store import Message, MessageStore
from assembler import ContextAssembler


@pytest.fixture
def store(tmp_path):
    return MessageStore(db_path=str(tmp_path / "test.db"))


def add(store, user_text, assistant_text, tags, ts=None, tokens=50):
    msg = Message.new(
        session_id="s1", user_id="u1",
        timestamp=ts or time.time(),
        user_text=user_text, assistant_text=assistant_text,
        tags=tags, token_count=tokens,
    )
    store.add_message(msg)
    return msg


def test_recency_layer_populated(store):
    add(store, "hello", "hi", tags=[], ts=1.0)
    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("incoming", [])
    assert result.recency_count == 1
    assert len(result.messages) == 1


def test_topic_layer_deduplicates(store):
    msg = add(store, "tailscale config", "use loopback", tags=["networking"], ts=1.0)
    assembler = ContextAssembler(store, token_budget=4000)
    # networking tag would retrieve msg, but it's also in recency — should not duplicate
    result = assembler.assemble("gateway issue", ["networking"])
    ids = [m.id for m in result.messages]
    assert len(ids) == len(set(ids)), "Duplicate messages in context"


def test_topic_layer_surfaces_old_relevant_message(store):
    # Old message with relevant tag
    old = add(store, "tailscale went offline", "kill old process",
              tags=["networking"], ts=1.0, tokens=20)
    # Many recent messages on unrelated topics (fills recency layer)
    for i in range(12):
        add(store, f"shopping item {i}", f"added item {i}",
            tags=["shopping-list"], ts=100.0 + i, tokens=20)

    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("fix the gateway", ["networking"])

    ids = [m.id for m in result.messages]
    assert old.id in ids, "Old relevant message should appear in topic layer"


def test_result_sorted_oldest_first(store):
    add(store, "first", "a", tags=[], ts=1.0)
    add(store, "second", "b", tags=[], ts=2.0)
    add(store, "third", "c", tags=[], ts=3.0)
    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("anything", [])
    timestamps = [m.timestamp for m in result.messages]
    assert timestamps == sorted(timestamps), "Messages should be oldest-first"


def test_token_budget_respected(store):
    # Add messages that would exceed budget if all included
    for i in range(20):
        add(store, f"msg {i}", f"resp {i}", tags=["security"],
            ts=float(i), tokens=100)
    assembler = ContextAssembler(store, token_budget=500)
    result = assembler.assemble("security question", ["security"])
    assert result.total_tokens <= 500


def test_empty_store(store):
    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("anything", ["security"])
    assert result.messages == []
    assert result.total_tokens == 0
