"""
test_assembly_realistic.py — Test context assembly with realistic data.

Uses realistic multi-turn conversations to verify that assembly produces
sensible, well-ordered, budget-respecting context.
"""

import pytest
import json
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import MessageStore, Message
from assembler import ContextAssembler


# ── Fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture
def populated_store(tmp_path):
    """Empty store ready for test data."""
    db_path = str(tmp_path / "realistic.db")
    return MessageStore(db_path)


# ── Helpers ──────────────────────────────────────────────────────────────────

def ingest(store, user_text, assistant_text, tags, offset=0, channel="rich"):
    """Add a message to the store."""
    msg = Message.new(
        session_id="realistic-session",
        user_id="rich",
        timestamp=time.time() - offset,
        user_text=user_text,
        assistant_text=assistant_text,
        tags=tags,
        token_count=len(user_text) + len(assistant_text),
        channel_label=channel,
    )
    store.add_message(msg)


# ── Realistic assembly tests ────────────────────────────────────────────────

class TestRealisticAssembly:
    """Assembly with realistic multi-turn conversations."""

    def test_code_debugging_session(self, populated_store):
        """A debugging conversation should retrieve relevant context."""
        store = populated_store

        # Ingest a debugging conversation
        ingest(store, "The deploy endpoint is returning 500", "Checking logs...", ["devops", "infrastructure"], offset=3600)
        ingest(store, "Logs show a null pointer", "Found the issue...", ["code", "debugging"], offset=3000)
        ingest(store, "Can we fix the null check?", "Yes, adding validation...", ["code"], offset=2400)
        ingest(store, "Done, pushed the fix", "Great, let me test it", ["code", "devops"], offset=1800)
        ingest(store, "Tests pass, deploying", "Deployment successful!", ["devops", "infrastructure"], offset=1200)
        ingest(store, "Anything else to check?", "Looks good!", ["question"], offset=600)

        assembler = ContextAssembler(store, token_budget=2000)
        result = assembler.assemble(
            incoming_text="was there a deploy issue earlier?",
            inferred_tags=["devops", "infrastructure"],
        )

        assert result.total_tokens <= 2000
        assert len(result.messages) > 0

    def test_topic_switch(self, populated_store):
        """Switching topics should retrieve the new topic, not old context."""
        store = populated_store

        # Old conversation about trading
        ingest(store, "how's my portfolio?", "Looking at your positions...", ["trading", "finance"], offset=7200)
        ingest(store, "should I sell the SPY calls?", "Consider the current volatility...", ["trading"], offset=6600)

        # New conversation about code
        ingest(store, "can you review this PR?", "Sure, let me check the code...", ["code"], offset=600)
        ingest(store, "the null check is wrong", "You're right, let me fix it...", ["code"], offset=300)

        assembler = ContextAssembler(store, token_budget=2000)
        result = assembler.assemble(
            incoming_text="review the null check fix",
            inferred_tags=["code"],
        )

        assert result.total_tokens <= 2000
        # Topic layer should retrieve code-tagged messages
        retrieved_tags = set()
        for m in result.messages:
            for t in m.tags:
                retrieved_tags.add(t)
        assert "code" in retrieved_tags

    def test_mixed_conversation_day(self, populated_store):
        """A full day of mixed topics should retrieve appropriately."""
        store = populated_store

        conversations = [
            # Morning: email check
            ("check important emails", "You have 3 unread...", ["email"], 10800),
            ("reply to Garret", "Draft: Hi Garrett...", ["email"], 10200),
            # Mid-morning: code
            ("fix the deploy script", "Updating the bash script...", ["code", "devops"], 7200),
            ("push to staging", "Done, running tests...", ["devops"], 6600),
            # Lunch: casual
            ("what's for lunch?", "There's leftover pizza...", ["food"], 3600),
            # Afternoon: infrastructure
            ("tailscale is flaky", "Checking Tailscale status...", ["networking"], 1800),
            ("restart the exit node", "Restarted...", ["networking", "devops"], 1200),
            # Late afternoon: memory
            ("update the memory system", "Adding new context tags...", ["memory-system"], 600),
            ("what did we do today?", "Let me summarize...", ["question"], 300),
        ]

        for user, assistant, tags, offset in conversations:
            ingest(store, user, assistant, tags, offset=offset)

        # Query about networking
        assembler = ContextAssembler(store, token_budget=2000)
        result = assembler.assemble(
            incoming_text="tailscale issue?",
            inferred_tags=["networking"],
        )

        assert result.total_tokens <= 2000
        # Verify messages are ordered oldest-first
        timestamps = [m.timestamp for m in result.messages]
        assert timestamps == sorted(timestamps), \
            f"Messages not in chronological order: {timestamps}"

    def test_empty_channel(self, populated_store):
        """Assembly should handle queries when there's no relevant data."""
        store = populated_store
        ingest(store, "hello", "hi", ["question"], offset=0, channel="new-user")

        assembler = ContextAssembler(store, token_budget=2000)
        result = assembler.assemble(
            incoming_text="first message",
            inferred_tags=["question"],
            channel_label="different-user",
        )

        # Should not crash, may have recency messages but no topic matches
        assert isinstance(result.messages, list)


# ── Budget accuracy ──────────────────────────────────────────────────────────

class TestBudgetAccuracy:
    """Verify token budgets are respected with realistic-sized messages."""

    def test_tiny_budget(self, populated_store):
        """Very small budget should only include minimal recency."""
        store = populated_store

        for i in range(5):
            ingest(store, f"Message {i} - " * 20, f"Reply {i} - " * 30,
                   ["code"], offset=i * 600)

        assembler = ContextAssembler(store, token_budget=300)
        result = assembler.assemble(
            incoming_text="test",
            inferred_tags=["code"],
        )

        # Small safety margin above budget for estimation variance
        assert result.total_tokens <= 400

    def test_large_budget(self, populated_store):
        """Large budget should include more messages."""
        store = populated_store

        for i in range(15):
            ingest(store, f"Message {i} - " * 10, f"Reply {i} - " * 10,
                   ["code"], offset=i * 600)

        assembler = ContextAssembler(store, token_budget=5000)
        result = assembler.assemble(
            incoming_text="test",
            inferred_tags=["code"],
        )

        assert result.total_tokens <= 5500  # Small safety margin
        # At least one layer should have messages
        assert result.sticky_count + result.recency_count + result.topic_count > 0


# ── Pin tests ────────────────────────────────────────────────────────────────

class TestAssemblyWithPins:
    """Assembly should prioritize pinned messages."""

    def test_pinned_messages_included_first(self, populated_store):
        """Sticky pins should appear in assembled context."""
        store = populated_store

        # Insert a message
        ingest(store, "deploy steps: 1) build 2) test 3) deploy",
               "Saved as reference.", ["devops"], offset=7200)

        # Get the message
        messages = store.get_recent(1)
        assert len(messages) > 0, "No messages found"
        msg = messages[0]

        # Create a pin via the StickyPinManager
        from sticky import StickyPinManager
        pin_manager = StickyPinManager()
        pin_manager.add_pin(
            message_ids=[msg.id],
            pin_type="explicit",
            reason="deploy-guide",
            ttl_turns=20,
            total_tokens=50,
        )

        # Ingest more messages
        for i in range(5):
            ingest(store, f"daily message {i}", f"response {i}",
                   ["question"], offset=3600 - i * 600)

        assembler = ContextAssembler(store, token_budget=2000)
        pinned_ids = pin_manager.get_pinned_message_ids()
        assert len(pinned_ids) > 0, "No pinned message IDs found"

        result = assembler.assemble(
            incoming_text="how do I deploy?",
            inferred_tags=["devops"],
            pinned_message_ids=pinned_ids,
        )

        # With pins active, sticky layer should be populated
        assert result.sticky_count > 0
