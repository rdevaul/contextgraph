"""
test_sticky.py — Tests for the sticky pin manager and sticky layer assembly.
"""

import pytest
import tempfile
import time
from pathlib import Path

from sticky import StickyPinManager, StickyPin
from assembler import ContextAssembler
from store import MessageStore, Message
from reframing import detect_reference


class TestStickyPinManager:
    """Tests for StickyPinManager."""

    @pytest.fixture
    def temp_state_file(self):
        """Create a temporary state file for testing."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
            yield f.name
        # Cleanup
        Path(f.name).unlink(missing_ok=True)

    @pytest.fixture
    def manager(self, temp_state_file):
        """Create a fresh StickyPinManager for each test."""
        return StickyPinManager(state_path=temp_state_file)

    def test_add_pin(self, manager):
        """Test adding a pin."""
        pin_id = manager.add_pin(
            message_ids=["msg-1", "msg-2"],
            pin_type="explicit",
            reason="Test pin",
            ttl_turns=10,
            total_tokens=100
        )

        assert pin_id is not None
        assert len(manager.get_active_pins()) == 1

        pin = manager.get_pin_by_id(pin_id)
        assert pin is not None
        assert pin.message_ids == ["msg-1", "msg-2"]
        assert pin.pin_type == "explicit"
        assert pin.reason == "Test pin"
        assert pin.ttl_turns == 10
        assert pin.turns_elapsed == 0
        assert pin.total_tokens == 100

    def test_remove_pin(self, manager):
        """Test removing a pin."""
        pin_id = manager.add_pin(
            message_ids=["msg-1"],
            pin_type="explicit",
            reason="Test",
            ttl_turns=5,
            total_tokens=50
        )

        assert len(manager.get_active_pins()) == 1
        success = manager.remove_pin(pin_id)
        assert success
        assert len(manager.get_active_pins()) == 0

        # Try removing again
        success = manager.remove_pin(pin_id)
        assert not success

    def test_get_pinned_message_ids(self, manager):
        """Test getting all pinned message IDs."""
        manager.add_pin(
            message_ids=["msg-1", "msg-2"],
            pin_type="explicit",
            reason="Pin 1",
            ttl_turns=10,
            total_tokens=100
        )
        manager.add_pin(
            message_ids=["msg-3", "msg-2"],  # msg-2 is duplicated
            pin_type="tool_chain",
            reason="Pin 2",
            ttl_turns=5,
            total_tokens=75
        )

        pinned_ids = manager.get_pinned_message_ids()
        # Should deduplicate msg-2
        assert set(pinned_ids) == {"msg-1", "msg-2", "msg-3"}

    def test_tick_increments_elapsed(self, manager):
        """Test that tick increments turns_elapsed."""
        pin_id = manager.add_pin(
            message_ids=["msg-1"],
            pin_type="explicit",
            reason="Test",
            ttl_turns=5,
            total_tokens=50
        )

        pin = manager.get_pin_by_id(pin_id)
        assert pin.turns_elapsed == 0

        manager.tick()
        pin = manager.get_pin_by_id(pin_id)
        assert pin.turns_elapsed == 1

        manager.tick()
        pin = manager.get_pin_by_id(pin_id)
        assert pin.turns_elapsed == 2

    def test_tick_expires_stale_pins(self, manager):
        """Test that tick expires pins when turns_elapsed > ttl_turns."""
        # Add a pin with TTL of 3
        pin_id = manager.add_pin(
            message_ids=["msg-1"],
            pin_type="explicit",
            reason="Test",
            ttl_turns=3,
            total_tokens=50
        )

        assert len(manager.get_active_pins()) == 1

        # Tick 3 times - pin should still be active
        manager.tick()
        manager.tick()
        manager.tick()
        assert len(manager.get_active_pins()) == 1

        # Fourth tick should expire it (turns_elapsed=4 > ttl_turns=3)
        expired = manager.tick()
        assert pin_id in expired
        assert len(manager.get_active_pins()) == 0

    def test_lru_eviction(self, manager):
        """Test that adding more than MAX_ACTIVE_PINS evicts oldest."""
        # Add 5 pins (MAX_ACTIVE_PINS)
        pin_ids = []
        for i in range(5):
            pin_id = manager.add_pin(
                message_ids=[f"msg-{i}"],
                pin_type="explicit",
                reason=f"Pin {i}",
                ttl_turns=10,
                total_tokens=50
            )
            pin_ids.append(pin_id)
            time.sleep(0.01)  # Ensure different created_at timestamps

        assert len(manager.get_active_pins()) == 5

        # Add a 6th pin - should evict the oldest (first one)
        new_pin_id = manager.add_pin(
            message_ids=["msg-new"],
            pin_type="explicit",
            reason="New pin",
            ttl_turns=10,
            total_tokens=50
        )

        assert len(manager.get_active_pins()) == 5
        # First pin should be evicted
        assert manager.get_pin_by_id(pin_ids[0]) is None
        # New pin should exist
        assert manager.get_pin_by_id(new_pin_id) is not None

    def test_update_or_create_tool_chain_pin(self, manager):
        """Test updating or creating tool chain pins."""
        # First call should create a new pin
        pin_id_1 = manager.update_or_create_tool_chain_pin(
            message_ids=["msg-1", "msg-2"],
            reason="Tool chain 1",
            total_tokens=100,
            ttl_turns=10
        )

        assert len(manager.get_active_pins()) == 1
        pin = manager.get_pin_by_id(pin_id_1)
        assert pin.pin_type == "tool_chain"
        assert pin.message_ids == ["msg-1", "msg-2"]

        # Second call should extend the existing pin
        pin_id_2 = manager.update_or_create_tool_chain_pin(
            message_ids=["msg-2", "msg-3", "msg-4"],
            reason="Tool chain 2",
            total_tokens=150,
            ttl_turns=10
        )

        # Should be the same pin
        assert pin_id_1 == pin_id_2
        assert len(manager.get_active_pins()) == 1

        pin = manager.get_pin_by_id(pin_id_1)
        # Should have added msg-3 and msg-4 (msg-2 was already there)
        assert set(pin.message_ids) == {"msg-1", "msg-2", "msg-3", "msg-4"}
        assert pin.total_tokens == 150
        assert pin.turns_elapsed == 0  # Should reset

    def test_state_persistence(self, temp_state_file):
        """Test that pins are persisted to and loaded from disk."""
        # Create manager and add pins
        manager1 = StickyPinManager(state_path=temp_state_file)
        pin_id = manager1.add_pin(
            message_ids=["msg-1", "msg-2"],
            pin_type="explicit",
            reason="Test pin",
            ttl_turns=10,
            total_tokens=100
        )

        # Create a new manager with the same state file
        manager2 = StickyPinManager(state_path=temp_state_file)

        # Should load the pin from disk
        assert len(manager2.get_active_pins()) == 1
        pin = manager2.get_pin_by_id(pin_id)
        assert pin is not None
        assert pin.message_ids == ["msg-1", "msg-2"]
        assert pin.reason == "Test pin"


class TestStickyLayerAssembly:
    """Tests for sticky layer in ContextAssembler."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.db') as f:
            yield f.name
        # Cleanup
        Path(f.name).unlink(missing_ok=True)

    @pytest.fixture
    def store(self, temp_db):
        """Create a test message store."""
        return MessageStore(db_path=temp_db)

    @pytest.fixture
    def assembler(self, store):
        """Create a test assembler."""
        return ContextAssembler(store, token_budget=1000)

    def test_sticky_layer_budget_allocation(self, store, assembler):
        """Test that sticky layer respects budget allocation."""
        # Add some messages
        for i in range(10):
            msg = Message.new(
                session_id="test",
                user_id="test-user",
                timestamp=float(i),
                user_text=f"User message {i}",
                assistant_text=f"Assistant response {i}",
                tags=["test"],
                token_count=50
            )
            store.add_message(msg)

        # Get recent messages for pinning
        recent = store.get_recent(3)
        pinned_ids = [msg.id for msg in recent]

        # Assemble with pinned messages
        result = assembler.assemble(
            incoming_text="Test query",
            inferred_tags=["test"],
            pinned_message_ids=pinned_ids
        )

        # Should have sticky messages
        assert result.sticky_count > 0
        assert result.sticky_count == len([m for m in result.messages if m.id in pinned_ids])

        # Total tokens should not exceed budget
        assert result.total_tokens <= 1000

    def test_sticky_layer_deduplication(self, store, assembler):
        """Test that sticky layer deduplicates with recency/topic layers."""
        # Add messages
        for i in range(5):
            msg = Message.new(
                session_id="test",
                user_id="test-user",
                timestamp=float(i),
                user_text=f"User message {i}",
                assistant_text=f"Assistant response {i}",
                tags=["test"],
                token_count=50
            )
            store.add_message(msg)

        # Pin the most recent message
        recent = store.get_recent(1)
        pinned_ids = [recent[0].id]

        result = assembler.assemble(
            incoming_text="Test query",
            inferred_tags=["test"],
            pinned_message_ids=pinned_ids
        )

        # The pinned message should only appear once in the result
        message_ids = [m.id for m in result.messages]
        assert message_ids.count(pinned_ids[0]) == 1

    def test_no_sticky_layer(self, store, assembler):
        """Test that assembly works without sticky layer (backward compatibility)."""
        # Add messages
        for i in range(5):
            msg = Message.new(
                session_id="test",
                user_id="test-user",
                timestamp=float(i),
                user_text=f"User message {i}",
                assistant_text=f"Assistant response {i}",
                tags=["test"],
                token_count=50
            )
            store.add_message(msg)

        # Assemble without pinned messages
        result = assembler.assemble(
            incoming_text="Test query",
            inferred_tags=["test"],
            pinned_message_ids=None
        )

        assert result.sticky_count == 0
        assert result.recency_count > 0 or result.topic_count > 0

    def test_sticky_layer_with_external_ids(self, store, assembler):
        """Test that sticky layer works with external_ids (OpenClaw message IDs)."""
        # Add messages with external_ids
        external_ids = []
        for i in range(5):
            external_id = f"openclaw-msg-{i}"
            msg = Message.new(
                session_id="test",
                user_id="test-user",
                timestamp=float(i),
                user_text=f"User message {i}",
                assistant_text=f"Assistant response {i}",
                tags=["test"],
                token_count=50,
                external_id=external_id
            )
            store.add_message(msg)
            external_ids.append(external_id)

        # Pin messages using external_ids (not internal store IDs)
        pinned_external_ids = [external_ids[0], external_ids[2], external_ids[4]]

        # Assemble with external_ids as pinned_message_ids
        result = assembler.assemble(
            incoming_text="Test query",
            inferred_tags=["test"],
            pinned_message_ids=pinned_external_ids
        )

        # Should have sticky messages found by external_id
        assert result.sticky_count > 0
        assert result.sticky_count == len(pinned_external_ids)

        # Verify the pinned messages are in the result
        pinned_messages = [m for m in result.messages if m.external_id in pinned_external_ids]
        assert len(pinned_messages) == len(pinned_external_ids)

        # Total tokens should not exceed budget
        assert result.total_tokens <= 1000


class TestReferenceDetection:
    """Tests for reference detection patterns."""

    def test_detect_reference_positive_cases(self):
        """Test that reference patterns are correctly detected."""
        positive_cases = [
            "any updates?",
            "what's the status?",
            "did that work?",
            "what happened with the deployment?",
            "how did it go?",
            "can you check?",
            "is that done?",
            "any luck?",
            "where are we on this?",
        ]

        for text in positive_cases:
            assert detect_reference(text), f"Should detect reference in: {text}"

    def test_detect_reference_negative_cases(self):
        """Test that non-reference messages are not detected."""
        negative_cases = [
            "Please deploy the application to production",
            "I need help with Python",
            "Create a new feature for user authentication",
            "The system is showing an error",
        ]

        for text in negative_cases:
            assert not detect_reference(text), f"Should not detect reference in: {text}"

    def test_detect_reference_system_artifacts(self):
        """Test that system artifacts are excluded."""
        system_messages = [
            "ran out of context and had to compact",
            "[System Message] what's the status?",
        ]

        for text in system_messages:
            assert not detect_reference(text), f"Should exclude system artifact: {text}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
