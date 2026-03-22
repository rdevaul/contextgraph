"""
test_automated_filtering.py — Tests for automated turn detection and filtering.

Tests cover:
- is_automated detection logic for cron, heartbeat, and local-watcher patterns
- MessageStore filtering methods (get_recent, get_by_tag, get_non_automated)
- Assembler exclusion of automated turns
- Backward compatibility (existing records without is_automated flag)
"""

import sys
import time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from logger import _is_automated_turn, log_interaction
from store import MessageStore, Message
from assembler import ContextAssembler


# ── Detection Logic Tests ─────────────────────────────────────────────────────


class TestAutomatedDetection:
    """Tests for _is_automated_turn() detection logic."""

    def test_cron_payload_detected(self):
        """Cron job payloads starting with [cron: should be detected."""
        assert _is_automated_turn("[cron:3d4fde45-1234] Some cron job")
        assert _is_automated_turn("[cron:abc] Another cron")
        assert _is_automated_turn("[cron:webhook-handler]")

    def test_heartbeat_prompt_detected(self):
        """Heartbeat prompt text should be detected."""
        text = "Read HEARTBEAT.md if it exists and respond with a summary"
        assert _is_automated_turn(text)

    def test_local_watcher_detected(self):
        """Local watcher events should be detected."""
        assert _is_automated_turn("[local-watcher] File changed: src/main.py")
        assert _is_automated_turn("[local-watcher] New file detected")

    def test_heartbeat_acknowledgement_detected(self):
        """HEARTBEAT_OK acknowledgement should be detected."""
        assert _is_automated_turn("HEARTBEAT_OK")
        # Should be exact match, with whitespace normalization
        assert _is_automated_turn("  HEARTBEAT_OK  ")

    def test_cron_uuid_pattern_detected(self):
        """UUID-style cron IDs should be detected."""
        assert _is_automated_turn("[cron:3d4fde45-8b2c-4e19-9f6a-1234567890ab]")
        assert _is_automated_turn("[cron:a1b2c3d4-e5f6]")

    def test_normal_messages_not_detected(self):
        """Normal conversation messages should NOT be detected as automated."""
        assert not _is_automated_turn("Hello, how are you?")
        assert not _is_automated_turn("Can you help me with a code review?")
        assert not _is_automated_turn("What is the capital of France?")
        assert not _is_automated_turn("Review the pull request #123")
        assert not _is_automated_turn("Let's discuss the project architecture")

    def test_edge_cases(self):
        """Test edge cases and potential false positives."""
        # "cron" in normal text should not trigger
        assert not _is_automated_turn("I need to setup a cron job")
        assert not _is_automated_turn("cron is a useful tool")

        # Heartbeat in different context
        assert not _is_automated_turn("The heartbeat monitoring is working")

        # Local watcher in normal text
        assert not _is_automated_turn("I'm using a local watcher for development")


# ── MessageStore Filtering Tests ──────────────────────────────────────────────


class TestMessageStoreFiltering:
    """Tests for MessageStore filtering of automated turns."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a temporary MessageStore for testing."""
        return MessageStore(db_path=str(tmp_path / "test.db"))

    def test_add_automated_message(self, store):
        """Test adding a message with is_automated=True."""
        msg = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time(),
            user_text="[cron:test] Automated job",
            assistant_text="Job complete",
            is_automated=True
        )
        store.add_message(msg)

        # Verify it was stored
        retrieved = store.get_by_id(msg.id)
        assert retrieved is not None
        assert retrieved.is_automated is True

    def test_get_recent_excludes_automated_by_default(self, store):
        """get_recent() should exclude automated turns by default."""
        # Add normal message
        normal = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time(),
            user_text="Normal message",
            assistant_text="Normal response",
            is_automated=False
        )
        store.add_message(normal)

        # Add automated message
        automated = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time() + 1,
            user_text="[cron:test] Automated",
            assistant_text="Automated response",
            is_automated=True
        )
        store.add_message(automated)

        # get_recent should only return normal message
        recent = store.get_recent(10)
        assert len(recent) == 1
        assert recent[0].id == normal.id

    def test_get_recent_includes_automated_when_requested(self, store):
        """get_recent(include_automated=True) should include automated turns."""
        # Add both types
        normal = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time(),
            user_text="Normal message",
            assistant_text="Normal response",
            is_automated=False
        )
        store.add_message(normal)

        automated = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time() + 1,
            user_text="[cron:test] Automated",
            assistant_text="Automated response",
            is_automated=True
        )
        store.add_message(automated)

        # With include_automated=True, should get both
        recent = store.get_recent(10, include_automated=True)
        assert len(recent) == 2
        ids = {msg.id for msg in recent}
        assert normal.id in ids
        assert automated.id in ids

    def test_get_by_tag_excludes_automated_by_default(self, store):
        """get_by_tag() should exclude automated turns by default."""
        # Add normal message with tag
        normal = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time(),
            user_text="Normal message",
            assistant_text="Normal response",
            tags=["test-tag"],
            is_automated=False
        )
        store.add_message(normal)

        # Add automated message with same tag
        automated = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time() + 1,
            user_text="[cron:test] Automated",
            assistant_text="Automated response",
            tags=["test-tag"],
            is_automated=True
        )
        store.add_message(automated)

        # get_by_tag should only return normal message
        tagged = store.get_by_tag("test-tag")
        assert len(tagged) == 1
        assert tagged[0].id == normal.id

    def test_get_by_tag_includes_automated_when_requested(self, store):
        """get_by_tag(include_automated=True) should include automated turns."""
        # Add both types with same tag
        normal = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time(),
            user_text="Normal message",
            assistant_text="Normal response",
            tags=["test-tag"],
            is_automated=False
        )
        store.add_message(normal)

        automated = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time() + 1,
            user_text="[cron:test] Automated",
            assistant_text="Automated response",
            tags=["test-tag"],
            is_automated=True
        )
        store.add_message(automated)

        # With include_automated=True, should get both
        tagged = store.get_by_tag("test-tag", include_automated=True)
        assert len(tagged) == 2
        ids = {msg.id for msg in tagged}
        assert normal.id in ids
        assert automated.id in ids

    def test_get_non_automated(self, store):
        """get_non_automated() should only return non-automated messages."""
        # Add mix of messages
        messages = []
        for i in range(5):
            msg = Message.new(
                session_id="test",
                user_id="user1",
                timestamp=time.time() + i,
                user_text=f"Message {i}",
                assistant_text=f"Response {i}",
                is_automated=(i % 2 == 0)  # Every other message is automated
            )
            store.add_message(msg)
            messages.append(msg)

        # get_non_automated should return only odd-indexed messages
        non_automated = store.get_non_automated()
        assert len(non_automated) == 2
        for msg in non_automated:
            assert not msg.is_automated


# ── Assembler Integration Tests ───────────────────────────────────────────────


class TestAssemblerFiltering:
    """Tests for ContextAssembler filtering of automated turns."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a temporary MessageStore for testing."""
        return MessageStore(db_path=str(tmp_path / "test.db"))

    def test_assembler_excludes_automated_from_recency_layer(self, store):
        """Assembler should not include automated turns in recency layer."""
        # Add normal messages
        for i in range(3):
            msg = Message.new(
                session_id="test",
                user_id="user1",
                timestamp=time.time() + i,
                user_text=f"Normal message {i}",
                assistant_text=f"Normal response {i}",
                tags=["normal"],
                token_count=50,
                is_automated=False
            )
            store.add_message(msg)

        # Add automated messages
        for i in range(3):
            msg = Message.new(
                session_id="test",
                user_id="user1",
                timestamp=time.time() + 3 + i,
                user_text=f"[cron:test] Automated {i}",
                assistant_text=f"Automated response {i}",
                tags=["automated"],
                token_count=50,
                is_automated=True
            )
            store.add_message(msg)

        # Assemble context
        assembler = ContextAssembler(store, token_budget=4000)
        result = assembler.assemble("test query", ["normal"])

        # Should only include normal messages
        assert len(result.messages) == 3
        for msg in result.messages:
            assert not msg.is_automated
            assert "Normal message" in msg.user_text

    def test_assembler_excludes_automated_from_topic_layer(self, store):
        """Assembler should not include automated turns in topic layer."""
        # Add normal message with specific tag
        normal = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time(),
            user_text="Normal contextgraph message",
            assistant_text="Normal response",
            tags=["contextgraph"],
            token_count=50,
            is_automated=False
        )
        store.add_message(normal)

        # Add automated message with same tag
        automated = Message.new(
            session_id="test",
            user_id="user1",
            timestamp=time.time() + 1,
            user_text="[cron:test] Automated contextgraph",
            assistant_text="Automated response",
            tags=["contextgraph"],
            token_count=50,
            is_automated=True
        )
        store.add_message(automated)

        # Assemble with contextgraph tag
        assembler = ContextAssembler(store, token_budget=4000)
        result = assembler.assemble("contextgraph query", ["contextgraph"])

        # Should only include normal message
        assert len(result.messages) == 1
        assert result.messages[0].id == normal.id
        assert not result.messages[0].is_automated


# ── Logger Integration Tests ──────────────────────────────────────────────────


class TestLoggerIntegration:
    """Tests for log_interaction() automatic detection."""

    def test_log_interaction_detects_cron(self, tmp_path):
        """log_interaction() should auto-detect cron messages."""
        record = log_interaction(
            user_text="[cron:test] Cron job executed",
            assistant_text="Job complete",
            session_id="test"
        )
        assert record.is_automated is True

    def test_log_interaction_detects_heartbeat(self, tmp_path):
        """log_interaction() should auto-detect heartbeat messages."""
        record = log_interaction(
            user_text="Read HEARTBEAT.md if it exists",
            assistant_text="Heartbeat OK",
            session_id="test"
        )
        assert record.is_automated is True

    def test_log_interaction_normal_message(self, tmp_path):
        """log_interaction() should not flag normal messages as automated."""
        record = log_interaction(
            user_text="Hello, can you help me?",
            assistant_text="Of course!",
            session_id="test"
        )
        assert record.is_automated is False


# ── Backward Compatibility Tests ──────────────────────────────────────────────


class TestBackwardCompatibility:
    """Tests for handling existing records without is_automated flag."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a store and manually insert old-style records."""
        store = MessageStore(db_path=str(tmp_path / "test.db"))
        conn = store._conn()

        # Manually insert a record without is_automated (simulating old DB)
        # Note: Migration will add the column with default 0, but this tests reading
        conn.execute(
            """INSERT INTO messages (id, session_id, user_id, timestamp,
               user_text, assistant_text, token_count, external_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("old-msg-1", "test", "user1", time.time(),
             "Old message", "Old response", 10, None)
        )
        conn.commit()

        return store

    def test_reading_old_records_defaults_to_false(self, store):
        """Old records without is_automated should default to False."""
        msg = store.get_by_id("old-msg-1")
        assert msg is not None
        # Should default to False due to migration
        assert msg.is_automated is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
