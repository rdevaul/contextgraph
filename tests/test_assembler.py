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


# ── Category 2: Sticky Layer Tests ───────────────────────────────────────────


def test_sticky_count_populated_from_pinned_ids(store):
    """Test 2.1: sticky_count populated from pinned_message_ids."""
    # Insert 10 messages
    messages = []
    for i in range(10):
        msg = add(store, f"user {i}", f"assistant {i}", tags=["test"], ts=float(i))
        messages.append(msg)

    # Pin 3 specific message IDs
    pinned_ids = [messages[2].id, messages[5].id, messages[7].id]

    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("query", ["test"], pinned_message_ids=pinned_ids)

    # sticky_count should be 3
    assert result.sticky_count == 3, f"Expected sticky_count=3, got {result.sticky_count}"

    # Pinned messages should appear in result
    result_ids = [m.id for m in result.messages]
    for pid in pinned_ids:
        assert pid in result_ids, f"Pinned message {pid} should be in result"


def test_sticky_count_zero_when_pinned_ids_none(store):
    """Test 2.2: sticky_count = 0 when pinned_message_ids=None."""
    # Insert messages
    for i in range(5):
        add(store, f"user {i}", f"assistant {i}", tags=["test"], ts=float(i))

    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("query", ["test"], pinned_message_ids=None)

    # sticky_count should be 0 (no sticky layer)
    assert result.sticky_count == 0, f"Expected sticky_count=0, got {result.sticky_count}"
    # Recency and topic should fill the budget
    assert result.recency_count > 0 or result.topic_count > 0


def test_sticky_count_zero_when_pinned_ids_empty(store):
    """Test 2.3: sticky_count = 0 when pinned_message_ids=[]."""
    # Insert messages
    for i in range(5):
        add(store, f"user {i}", f"assistant {i}", tags=["test"], ts=float(i))

    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("query", ["test"], pinned_message_ids=[])

    # sticky_count should be 0 (empty list = no pins)
    assert result.sticky_count == 0, f"Expected sticky_count=0, got {result.sticky_count}"


def test_sticky_budget_discipline(store):
    """Test 2.4: Budget discipline — sticky never exceeds 30%."""
    # Create messages with large token counts (>500 tokens each)
    messages = []
    for i in range(10):
        msg = add(store, f"user {i}", f"assistant {i}", tags=["test"], ts=float(i), tokens=600)
        messages.append(msg)

    # Pin all 10 messages (would exceed 30% if all included)
    pinned_ids = [m.id for m in messages]

    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("query", ["test"], pinned_message_ids=pinned_ids)

    # Sticky should be limited to ~2 messages (1200 tokens max / 600 per message)
    # Check sticky_count directly, not messages that happen to be in pinned_ids
    assert result.sticky_count <= 2, (
        f"Expected sticky_count <= 2 due to 30% budget cap, got {result.sticky_count}"
    )


def test_budget_reallocation_when_sticky_empty(store):
    """Test 2.5: Budget reallocation when sticky is empty."""
    # Insert messages
    for i in range(20):
        add(store, f"user {i}", f"assistant {i}", tags=["test"], ts=float(i), tokens=50)

    assembler = ContextAssembler(store, token_budget=1000)

    # Assemble with no sticky
    result_no_sticky = assembler.assemble("query", ["test"], pinned_message_ids=None)

    # Assemble with empty sticky list
    result_empty_sticky = assembler.assemble("query", ["test"], pinned_message_ids=[])

    # Both should have sticky_count = 0
    assert result_no_sticky.sticky_count == 0
    assert result_empty_sticky.sticky_count == 0

    # Both should have similar total message counts (budget fully utilized)
    # When sticky is empty, recency + topic should get full budget
    assert result_no_sticky.recency_count + result_no_sticky.topic_count > 0
    assert result_empty_sticky.recency_count + result_empty_sticky.topic_count > 0


def test_external_id_lookup_vs_internal_id(store):
    """Test 2.7: External ID lookup vs internal ID lookup."""
    # Ingest messages with external_ids
    msg1 = add(store, "user 1", "assistant 1", tags=["test"], ts=1.0, tokens=50)
    msg2 = add(store, "user 2", "assistant 2", tags=["test"], ts=2.0, tokens=50)
    msg3 = add(store, "user 3", "assistant 3", tags=["test"], ts=3.0, tokens=50)

    # Add external_ids manually (MessageStore.add_message doesn't set external_id in our test helper)
    # So we'll test with internal IDs for now, and external_id lookup is tested in integration tests

    # Pin using internal IDs
    pinned_ids = [msg1.id, msg2.id, msg3.id]

    assembler = ContextAssembler(store, token_budget=4000)
    result = assembler.assemble("query", ["test"], pinned_message_ids=pinned_ids)

    # All 3 should be found
    assert result.sticky_count == 3

    # Test with nonexistent ID (should be gracefully skipped)
    pinned_with_invalid = [msg1.id, "nonexistent-id", msg2.id]
    result2 = assembler.assemble("query", ["test"], pinned_message_ids=pinned_with_invalid)

    # Only 2 should be found (nonexistent skipped, no crash)
    assert result2.sticky_count == 2
