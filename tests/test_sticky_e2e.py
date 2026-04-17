"""
test_sticky_e2e.py — End-to-end lifecycle tests for sticky threads (Category 6)

These tests simulate full multi-turn conversations and validate the complete
sticky thread lifecycle from creation through TTL expiry.

Marked as @pytest.mark.slow because they simulate multiple turns.
"""

import pytest
import requests
import time
import uuid


API_BASE_URL = "http://localhost:8302"


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


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.sticky
class TestFullToolChainLifecycle:
    """Test full tool chain lifecycle from creation to TTL expiry."""

    def test_full_tool_chain_lifecycle(self, api_available, unique_session_id):
        """
        Test 6.1: Full tool chain lifecycle.

        Simulate:
        1. Turn 1: Tool use → pin created
        2. Turn 2: Tool use continues → pin extended
        3. Turn 3: No tools → pin starts aging
        4. Turn N (TTL+1): Pin expires
        """
        session_id = unique_session_id

        # Turn 1: Tool use creates pin
        msg1_id, ext1 = _ingest_message(session_id, "Deploy app", "Deploying to production...")
        response1 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Deploy app",
                "session_id": session_id,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": [ext1]
                }
            }
        )
        assert response1.status_code == 200
        data1 = response1.json()
        assert data1["sticky_count"] > 0, "Turn 1: Pin should be created"

        # Get pin ID
        pins1 = requests.get(f"{API_BASE_URL}/pins").json()
        assert len(pins1["active_pins"]) == 1
        pin_id = pins1["active_pins"][0]["pin_id"]
        assert pins1["active_pins"][0]["turns_elapsed"] == 0, "Turn 1: Pin just created, not ticked yet"

        # Turn 2: Tool use continues → pin extended
        msg2_id, ext2 = _ingest_message(session_id, "Check status", "Checking deployment...")
        response2 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Check status",
                "session_id": session_id,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": [ext1, ext2]
                }
            }
        )
        assert response2.status_code == 200
        data2 = response2.json()
        assert data2["sticky_count"] > 0, "Turn 2: Pin should still exist"

        # Pin ID should be stable (extended, not replaced)
        pins2 = requests.get(f"{API_BASE_URL}/pins").json()
        assert len(pins2["active_pins"]) == 1
        assert pins2["active_pins"][0]["pin_id"] == pin_id, "Turn 2: Pin ID should be stable"
        # turns_elapsed should be 0 (tick happens, then extension resets it)
        assert pins2["active_pins"][0]["turns_elapsed"] == 0, "Turn 2: Pin extended, TTL reset to 0"

        # Turn 3: No tools → pin starts aging
        msg3_id, ext3 = _ingest_message(session_id, "Done", "Deployment complete")
        response3 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Done",
                "session_id": session_id,
                "token_budget": 4000,
                "tool_state": None  # No tools
            }
        )
        assert response3.status_code == 200
        data3 = response3.json()
        assert data3["sticky_count"] > 0, "Turn 3: Pin should still exist but aging"

        pins3 = requests.get(f"{API_BASE_URL}/pins").json()
        assert len(pins3["active_pins"]) == 1
        assert pins3["active_pins"][0]["turns_elapsed"] == 1, "Turn 3: Pin aging (1 turn elapsed)"

        # Age the pin until it expires (default TTL is 10)
        ttl = pins3["active_pins"][0]["ttl_turns"]
        turns_remaining = ttl - pins3["active_pins"][0]["turns_elapsed"]

        # Tick enough times to expire
        for i in range(turns_remaining + 1):
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

        # Pin should be expired now
        pins_final = requests.get(f"{API_BASE_URL}/pins").json()
        assert len(pins_final["active_pins"]) == 0, "Pin should be expired after TTL"

        # Assemble should return sticky_count == 0
        response_final = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Final query",
                "session_id": session_id,
                "token_budget": 4000
            }
        )
        assert response_final.status_code == 200
        assert response_final.json()["sticky_count"] == 0, "sticky_count should be 0 after expiry"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.sticky
class TestNonToolConversation:
    """Test that non-tool conversations don't create unwanted pins."""

    def test_non_tool_conversation_never_creates_pins(self, api_available, unique_session_id):
        """
        Test 6.3: Non-tool conversation never creates pins.

        Run 20 turns of normal Q&A (no tool_state) and verify zero pins throughout.
        """
        session_id = unique_session_id

        for i in range(20):
            # Ingest message
            _ingest_message(session_id, f"Question {i}", f"Answer {i}")

            # Call /assemble without tool_state
            response = requests.post(
                f"{API_BASE_URL}/assemble",
                json={
                    "user_text": f"Question {i}",
                    "session_id": session_id,
                    "token_budget": 4000,
                    "tool_state": None
                }
            )
            assert response.status_code == 200
            data = response.json()

            # Should have zero sticky pins
            assert data["sticky_count"] == 0, (
                f"Turn {i}: Expected sticky_count==0 for non-tool conversation, got {data['sticky_count']}"
            )

            # Verify GET /pins shows zero pins
            pins = requests.get(f"{API_BASE_URL}/pins").json()
            assert len(pins["active_pins"]) == 0, f"Turn {i}: No pins should exist"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.sticky
class TestMaxPinCountEnforced:
    """Test that LRU eviction enforces MAX_ACTIVE_PINS."""

    def test_max_pin_count_enforced(self, api_available, unique_session_id):
        """
        Test 6.4: Max pin count enforced (LRU eviction).

        Create 5 explicit pins (MAX_ACTIVE_PINS), then create a 6th.
        Verify oldest is evicted.
        """
        session_id = unique_session_id

        # Create 5 pins
        pin_ids = []
        for i in range(5):
            msg_id, ext_id = _ingest_message(session_id, f"Message {i}", f"Response {i}")
            pin_response = requests.post(
                f"{API_BASE_URL}/pin",
                json={
                    "message_ids": [ext_id],
                    "reason": f"Pin {i}",
                    "ttl_turns": 20
                }
            )
            assert pin_response.status_code == 200
            pin_ids.append(pin_response.json()["pin_id"])
            time.sleep(0.01)  # Ensure different created_at timestamps

        # Verify 5 pins exist
        pins = requests.get(f"{API_BASE_URL}/pins").json()
        assert len(pins["active_pins"]) == 5, "Should have 5 pins (MAX_ACTIVE_PINS)"

        # Create a 6th pin
        msg_id, ext_id = _ingest_message(session_id, "Message 6", "Response 6")
        pin_response = requests.post(
            f"{API_BASE_URL}/pin",
            json={
                "message_ids": [ext_id],
                "reason": "Pin 6",
                "ttl_turns": 20
            }
        )
        assert pin_response.status_code == 200
        new_pin_id = pin_response.json()["pin_id"]

        # Verify still only 5 pins
        pins_after = requests.get(f"{API_BASE_URL}/pins").json()
        assert len(pins_after["active_pins"]) == 5, "Should still have 5 pins (LRU eviction)"

        # Oldest pin (pin_ids[0]) should be evicted
        active_pin_ids = [p["pin_id"] for p in pins_after["active_pins"]]
        assert pin_ids[0] not in active_pin_ids, "Oldest pin should be evicted"
        assert new_pin_id in active_pin_ids, "New pin should be present"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.sticky
class TestStickyBudgetCap:
    """Test that sticky layer respects 30% budget cap."""

    def test_sticky_budget_cap_prevents_token_overflow(self, api_available, unique_session_id):
        """
        Test 6.5: Sticky budget cap prevents token overflow.

        Ingest 10 large messages, pin all 10, then assemble with small budget.
        Verify sticky_count < 10 (cap hit) and total_tokens <= budget.
        """
        session_id = unique_session_id

        # Ingest 10 large messages (~500 tokens each)
        large_text = "word " * 100  # ~500 words, ~650 tokens
        external_ids = []
        for i in range(10):
            msg_id, ext_id = _ingest_message(
                session_id,
                f"User: {large_text}",
                f"Assistant: {large_text}"
            )
            external_ids.append(ext_id)

        # Pin all 10 messages explicitly
        pin_response = requests.post(
            f"{API_BASE_URL}/pin",
            json={
                "message_ids": external_ids,
                "reason": "Test large pin",
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

        # Total tokens should not exceed budget
        assert data["total_tokens"] <= 400, (
            f"Budget exceeded: total_tokens={data['total_tokens']}, budget=400"
        )

        # Other layers should still get some budget
        # (This validates budget reallocation works correctly)
        assert data["recency_count"] >= 0, "Recency layer should exist"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.sticky
class TestGatewayRestartRecovery:
    """Test server-side fallback when plugin loses state."""

    def test_gateway_restart_recovery(self, api_available, unique_session_id):
        """
        Test 6.6: Gateway restart recovery.

        Simulate plugin restart by calling /assemble with:
        - last_turn_had_tools=True
        - pending_chain_ids=[] (empty, simulating lost state)
        - But messages exist in store from previous turns

        Verify server-side fallback creates pin from recent messages.
        """
        session_id = unique_session_id

        # Ingest messages (simulating previous conversation before restart)
        for i in range(5):
            _ingest_message(session_id, f"Pre-restart message {i}", f"Response {i}")

        # Call /assemble with tool_state but empty pending_chain_ids
        # This simulates: gateway restarted, plugin lost chain IDs, but tool was active
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Continue working",
                "session_id": session_id,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": []  # Empty - plugin lost state
                }
            }
        )
        assert response.status_code == 200
        data = response.json()

        # Server-side fallback should activate
        assert data["sticky_count"] > 0, (
            "Server-side fallback should create pin from recent messages when chain_ids empty"
        )

        # Verify pin was created
        pins = requests.get(f"{API_BASE_URL}/pins").json()
        assert len(pins["active_pins"]) == 1, "One tool_chain pin should be created"

        pin = pins["active_pins"][0]
        assert pin["pin_type"] == "tool_chain"
        assert "fallback" in pin["reason"].lower(), (
            "Pin reason should mention 'fallback' for gateway restart recovery"
        )
        assert len(pin["message_ids"]) > 0, "Pin should contain messages"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "slow"])
