"""
Test suite for server-side sticky thread detection.

This test suite validates the fix for the sticky thread detection bug.
The bug: plugin-side tracking of pending_chain_ids is wiped on gateway restart,
causing sticky_count to always be 0 in production.

The fix: Server-side detection using get_recent() when pending_chain_ids is empty.

Run with: python3 -m pytest tests/test_sticky_server_detection.py -v
"""

import pytest
import requests
import time
import uuid


API_BASE_URL = "http://localhost:8300"


@pytest.fixture(scope="module")
def api_available():
    """Check if the API is available. Skip tests if not running."""
    try:
        response = requests.get(f"{API_BASE_URL}/health", timeout=2)
        return response.status_code == 200
    except requests.RequestException:
        pytest.skip("API is not running on port 8300. Start with: python3 -m api.server")


@pytest.fixture(autouse=True)
def clean_pins(api_available):
    """Clear all pins before and after each test."""
    # Clear pins before test
    pins_response = requests.get(f"{API_BASE_URL}/pins")
    if pins_response.status_code == 200:
        pins_data = pins_response.json()
        for pin in pins_data.get("active_pins", []):
            requests.post(f"{API_BASE_URL}/unpin", json={"pin_id": pin["pin_id"]})

    yield

    # Clear pins after test
    pins_response = requests.get(f"{API_BASE_URL}/pins")
    if pins_response.status_code == 200:
        pins_data = pins_response.json()
        for pin in pins_data.get("active_pins", []):
            requests.post(f"{API_BASE_URL}/unpin", json={"pin_id": pin["pin_id"]})


def _unique_session_id():
    """Generate unique session ID for test isolation."""
    return f"test-session-{uuid.uuid4()}"


def _ingest_message(session_id, user_text, assistant_text, external_id=None):
    """Helper to ingest a message and return its ID."""
    msg_id = f"test-msg-{uuid.uuid4()}"
    ext_id = external_id or f"test-ext-{uuid.uuid4()}"

    response = requests.post(
        f"{API_BASE_URL}/ingest",
        json={
            "id": msg_id,
            "session_id": session_id,
            "user_text": user_text,
            "assistant_text": assistant_text,
            "timestamp": time.time(),
            "user_id": "test-user",
            "external_id": ext_id
        }
    )

    assert response.status_code == 200, f"Ingest failed: {response.text}"
    return msg_id, ext_id


@pytest.mark.sticky
class TestStickyServerDetection:
    """Test server-side sticky thread detection."""

    def test_sticky_pins_created_without_chain_ids(self, api_available):
        """
        CORE REGRESSION TEST: Sticky pins should be created even with empty pending_chain_ids.

        This is the main bug fix validation. Before the fix, empty pending_chain_ids
        meant sticky_count was always 0 after gateway restart.
        """
        session_id = _unique_session_id()

        # Ingest some messages to have recent history
        _ingest_message(session_id, "Start deployment", "Starting deployment process...")
        _ingest_message(session_id, "Check status", "Checking deployment status...")
        _ingest_message(session_id, "Continue", "Deployment is 50% complete...")

        # Call /assemble with tool_state but empty pending_chain_ids
        # This simulates the bug: plugin restarted, lost track of chain IDs
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "What's the status?",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": []  # Empty - this is the bug scenario
                }
            }
        )

        assert response.status_code == 200
        data = response.json()

        # THE FIX: sticky_count should be > 0 even with empty chain_ids
        assert data["sticky_count"] > 0, (
            f"Expected sticky_count > 0 with server-side detection, got {data['sticky_count']}. "
            "The fix should use get_recent() when pending_chain_ids is empty."
        )

        # Verify a tool_chain pin was created
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        assert pins_response.status_code == 200
        pins_data = pins_response.json()

        tool_chain_pins = [p for p in pins_data["active_pins"] if p["pin_type"] == "tool_chain"]
        assert len(tool_chain_pins) == 1, "Expected exactly one tool_chain pin to be created"

        pin = tool_chain_pins[0]
        assert len(pin["message_ids"]) > 0, "Pin should contain message IDs"
        assert pin["total_tokens"] > 0, "Pin should have non-zero token count"

    def test_sticky_pins_not_created_without_tool_state(self, api_available):
        """
        Sticky pins should NOT be created when tool_state is null.

        Normal queries without tool activity should not trigger sticky pinning.
        """
        session_id = _unique_session_id()

        # Ingest some messages
        _ingest_message(session_id, "What is the weather?", "The weather is sunny.")
        _ingest_message(session_id, "Tell me a joke", "Why did the chicken cross the road?")

        # Call /assemble without tool_state
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Another question",
                "tags": None,
                "token_budget": 4000,
                "tool_state": None  # No tool state
            }
        )

        assert response.status_code == 200
        data = response.json()

        # sticky_count should be 0 (no tool activity)
        assert data["sticky_count"] == 0, (
            f"Expected sticky_count = 0 without tool_state, got {data['sticky_count']}"
        )

        # Verify no tool_chain pins exist
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        assert pins_response.status_code == 200
        pins_data = pins_response.json()

        tool_chain_pins = [p for p in pins_data["active_pins"] if p["pin_type"] == "tool_chain"]
        assert len(tool_chain_pins) == 0, "No tool_chain pins should be created without tool_state"

    def test_sticky_pins_created_with_chain_ids_still_works(self, api_available):
        """
        BACKWARD COMPATIBILITY TEST: Providing pending_chain_ids should still work.

        The old behavior (plugin provides chain IDs) should continue to work.
        """
        session_id = _unique_session_id()

        # Ingest messages with known external_ids
        msg1_id, ext1 = _ingest_message(
            session_id,
            "Deploy app",
            "Deploying app...",
            external_id="test-ext-deploy-1"
        )
        msg2_id, ext2 = _ingest_message(
            session_id,
            "Check deployment",
            "Checking...",
            external_id="test-ext-deploy-2"
        )

        # Call /assemble with explicit pending_chain_ids
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "What's happening?",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": [ext1, ext2]  # Explicit chain IDs
                }
            }
        )

        assert response.status_code == 200
        data = response.json()

        # sticky_count should be > 0
        assert data["sticky_count"] > 0, (
            f"Expected sticky_count > 0 with explicit chain_ids, got {data['sticky_count']}"
        )

        # Verify pin was created
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        assert pins_response.status_code == 200
        pins_data = pins_response.json()

        tool_chain_pins = [p for p in pins_data["active_pins"] if p["pin_type"] == "tool_chain"]
        assert len(tool_chain_pins) == 1, "Expected exactly one tool_chain pin"

    def test_sticky_pins_fallback_uses_recent_messages(self, api_available):
        """
        Server-side fallback should use get_recent() messages.

        When pending_chain_ids is empty, the server should fetch recent messages
        and pin them based on recency.
        """
        session_id = _unique_session_id()

        # Ingest exactly 3 messages
        _ingest_message(session_id, "Message 1", "Response 1")
        _ingest_message(session_id, "Message 2", "Response 2")
        _ingest_message(session_id, "Message 3", "Response 3")

        # Call /assemble with empty chain_ids
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Follow-up",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": []
                }
            }
        )

        assert response.status_code == 200
        data = response.json()

        # Should have sticky messages
        assert data["sticky_count"] > 0, "Expected sticky messages from recent history"

        # Verify the pin contains messages
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        assert pins_response.status_code == 200
        pins_data = pins_response.json()

        tool_chain_pins = [p for p in pins_data["active_pins"] if p["pin_type"] == "tool_chain"]
        assert len(tool_chain_pins) == 1

        pin = tool_chain_pins[0]
        # Should have pinned recent messages (up to 5 max as per config)
        # NOTE: get_recent() is global, so it may include messages from other tests
        assert len(pin["message_ids"]) >= 3, f"Expected at least 3 pinned messages, got {len(pin['message_ids'])}"
        assert len(pin["message_ids"]) <= 5, f"Expected at most 5 pinned messages, got {len(pin['message_ids'])}"
        assert pin["total_tokens"] > 0, "Pin should have non-zero tokens"

    def test_sticky_count_appears_in_response(self, api_available):
        """
        Basic smoke test: /assemble response should always include sticky_count.
        """
        session_id = _unique_session_id()

        # Simple assemble call
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Hello",
                "tags": None,
                "token_budget": 4000
            }
        )

        assert response.status_code == 200
        data = response.json()

        # sticky_count key must exist
        assert "sticky_count" in data, "Response must include sticky_count"
        assert isinstance(data["sticky_count"], int), "sticky_count must be an integer"
        assert data["sticky_count"] >= 0, "sticky_count must be non-negative"

    def test_multiple_tool_turns_extend_existing_pin(self, api_available):
        """
        Multiple tool turns should extend the existing pin, not create duplicates.

        The update_or_create_tool_chain_pin method should find and extend
        an existing tool_chain pin rather than creating a new one.
        """
        session_id = _unique_session_id()

        # Ingest first batch of messages
        _ingest_message(session_id, "Start task", "Starting task...")
        _ingest_message(session_id, "Continue", "Continuing...")

        # First tool turn - creates pin
        response1 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "First query",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": []
                }
            }
        )

        assert response1.status_code == 200
        data1 = response1.json()
        assert data1["sticky_count"] > 0

        # Get pin ID
        pins_response1 = requests.get(f"{API_BASE_URL}/pins")
        pins_data1 = pins_response1.json()
        initial_pins = pins_data1["active_pins"]
        assert len(initial_pins) == 1
        pin_id_1 = initial_pins[0]["pin_id"]

        # Ingest more messages
        _ingest_message(session_id, "More work", "Doing more work...")

        # Second tool turn - should extend existing pin
        response2 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Second query",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": []
                }
            }
        )

        assert response2.status_code == 200
        data2 = response2.json()
        assert data2["sticky_count"] > 0

        # Should still be only ONE pin (extended, not duplicated)
        pins_response2 = requests.get(f"{API_BASE_URL}/pins")
        pins_data2 = pins_response2.json()
        final_pins = pins_data2["active_pins"]

        assert len(final_pins) == 1, (
            f"Expected 1 pin (extended), got {len(final_pins)}. "
            "update_or_create_tool_chain_pin should extend existing pin, not create duplicates."
        )

        # Should be the same pin ID
        pin_id_2 = final_pins[0]["pin_id"]
        assert pin_id_2 == pin_id_1, "Pin ID should remain the same (extended, not replaced)"

        # TTL should be reset (turns_elapsed should be 0 or low)
        # Note: The assemble call itself ticks the pin, so turns_elapsed might be 1
        assert final_pins[0]["turns_elapsed"] <= 1, (
            "Extended pin should have reset TTL (turns_elapsed near 0)"
        )

    def test_pin_ttl_progression(self, api_available):
        """
        Test 3.5: Pin TTL progression.

        Create pin via tool_state, then call /assemble (no tool_state) N times.
        Verify turns_elapsed increments and pin expires after ttl_turns.
        """
        session_id = _unique_session_id()

        # Ingest messages
        _ingest_message(session_id, "Start task", "Starting...")
        _ingest_message(session_id, "Continue", "Continuing...")

        # Create pin with TTL of 3
        response1 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Query",
                "tags": None,
                "token_budget": 4000,
                "session_id": session_id,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": []
                }
            }
        )
        assert response1.status_code == 200
        assert response1.json()["sticky_count"] > 0

        # Get pin ID and TTL
        pins1 = requests.get(f"{API_BASE_URL}/pins").json()
        pin_id = pins1["active_pins"][0]["pin_id"]
        initial_ttl = pins1["active_pins"][0]["ttl_turns"]
        initial_elapsed = pins1["active_pins"][0]["turns_elapsed"]

        # Tick N+1 times (without tool_state, so pin ages)
        # With turns_elapsed > ttl_turns semantics, need one extra tick to expire
        for i in range(initial_ttl - initial_elapsed + 1):
            response = requests.post(
                f"{API_BASE_URL}/assemble",
                json={
                    "user_text": f"Query {i}",
                    "session_id": session_id,
                    "token_budget": 4000,
                    "tool_state": None
                }
            )
            assert response.status_code == 200

            pins = requests.get(f"{API_BASE_URL}/pins").json()
            if len(pins["active_pins"]) == 0:
                # Pin expired
                break

        # After TTL ticks, pin should be expired
        pins_final = requests.get(f"{API_BASE_URL}/pins").json()
        pin_ids_final = [p["pin_id"] for p in pins_final["active_pins"]]
        assert pin_id not in pin_ids_final, "Pin should be expired after ttl_turns ticks"

    def test_sticky_count_persists_across_turns(self, api_available):
        """
        Test 3.7: sticky_count persists across turns.

        Create pin via tool_state in turn 1.
        In turn 2, call /assemble with no tool_state.
        Second call should still see sticky_count > 0 (pin persists).
        """
        session_id = _unique_session_id()

        # Ingest messages
        msg1_id, ext1 = _ingest_message(session_id, "Start", "Starting...")
        msg2_id, ext2 = _ingest_message(session_id, "Continue", "Continuing...")

        # Turn 1: Create pin
        response1 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Start task",
                "session_id": session_id,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": [ext1, ext2]
                }
            }
        )
        assert response1.status_code == 200
        data1 = response1.json()
        assert data1["sticky_count"] > 0, "Turn 1: Pin should be created"

        # Turn 2: No tool_state, pin should persist
        response2 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Follow-up query",
                "session_id": session_id,
                "token_budget": 4000,
                "tool_state": None  # No tool state
            }
        )
        assert response2.status_code == 200
        data2 = response2.json()
        assert data2["sticky_count"] > 0, (
            "Turn 2: Pin should persist from turn 1 (sticky_count > 0)"
        )

    def test_budget_cap_respected_in_live_assembly(self, api_available):
        """
        Test 3.8: Budget cap respected in live assembly.

        Ingest large messages, pin 10 of them, assemble with small budget.
        Verify sticky_count < 10 and total_tokens <= budget.
        """
        session_id = _unique_session_id()

        # Ingest 10 messages with ~200 tokens each
        large_text = "word " * 50  # ~250 words, ~325 tokens per message
        external_ids = []
        for i in range(10):
            msg_id, ext_id = _ingest_message(
                session_id,
                f"User: {large_text}",
                f"Assistant: {large_text}"
            )
            external_ids.append(ext_id)

        # Pin all 10 via explicit pin
        pin_response = requests.post(
            f"{API_BASE_URL}/pin",
            json={
                "message_ids": external_ids,
                "reason": "Test budget cap",
                "ttl_turns": 20
            }
        )
        assert pin_response.status_code == 200

        # Assemble with small budget (400 tokens)
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Query",
                "session_id": session_id,
                "token_budget": 400
            }
        )
        assert response.status_code == 200
        data = response.json()

        # sticky_count should be < 10 (budget cap hit)
        assert data["sticky_count"] < 10, (
            f"Expected sticky_count < 10 due to budget cap, got {data['sticky_count']}"
        )

        # total_tokens should not exceed budget
        assert data["total_tokens"] <= 400, (
            f"Budget exceeded: {data['total_tokens']} > 400"
        )


@pytest.mark.sticky
class TestStickyEdgeCases:
    """Test edge cases for server-side sticky detection."""

    def test_empty_store_does_not_crash(self, api_available):
        """
        Server should handle empty store gracefully.

        If get_recent() returns no messages, the server should not crash.
        This test can't guarantee an empty global store in a shared test environment,
        but it verifies the server doesn't crash when called with tool_state.
        """
        # Use a unique session that has no messages
        session_id = _unique_session_id()

        # Call /assemble with tool_state
        # NOTE: get_recent() is global, so it may find messages from other tests
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "First message ever",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": []
                }
            }
        )

        # Main assertion: should not crash
        assert response.status_code == 200
        data = response.json()

        # Should have sticky_count >= 0 (depends on global store state)
        assert isinstance(data["sticky_count"], int), "sticky_count should be an integer"
        assert data["sticky_count"] >= 0, "sticky_count should be non-negative"

    def test_tool_state_false_does_not_create_pin(self, api_available):
        """
        tool_state with last_turn_had_tools=False should not create pins.
        """
        session_id = _unique_session_id()

        _ingest_message(session_id, "Normal chat", "Just chatting...")

        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Another message",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": False,  # No tools used
                    "pending_chain_ids": []
                }
            }
        )

        assert response.status_code == 200
        data = response.json()

        assert data["sticky_count"] == 0, "No sticky pins should be created when tools weren't used"

        pins_response = requests.get(f"{API_BASE_URL}/pins")
        pins_data = pins_response.json()
        tool_chain_pins = [p for p in pins_data["active_pins"] if p["pin_type"] == "tool_chain"]
        assert len(tool_chain_pins) == 0
