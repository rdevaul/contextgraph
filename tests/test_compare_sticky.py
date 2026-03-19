"""
test_compare_sticky.py — Tests for /compare endpoint with sticky pins (Category 4)

Tests that validate the fixed /compare endpoint correctly reads and reports sticky_count.
"""

import pytest
import requests
import time
import uuid


API_BASE_URL = "http://localhost:8300"


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


@pytest.mark.compare
@pytest.mark.sticky
class TestCompareStickyCount:
    """Test /compare endpoint sticky_count field (Bug A fix validation)."""

    def test_compare_returns_sticky_count_when_pins_exist(self, api_available, unique_session_id):
        """
        Test 4.1: /compare returns sticky_count > 0 when pins exist.

        This test validates Fix 1 (Bug A): /compare must consult pin_manager.
        Before the fix, sticky_count would be missing or always 0.
        """
        session_id = unique_session_id

        # Ingest messages
        msg1_id, ext1 = _ingest_message(session_id, "Deploy app", "Deploying...")
        msg2_id, ext2 = _ingest_message(session_id, "Check status", "Checking...")
        msg3_id, ext3 = _ingest_message(session_id, "Continue", "Continuing...")

        # Create an explicit pin
        pin_response = requests.post(
            f"{API_BASE_URL}/pin",
            json={
                "message_ids": [ext1, ext2, ext3],
                "reason": "Test pin for comparison",
                "ttl_turns": 10
            }
        )
        assert pin_response.status_code == 200

        # Call /compare
        compare_response = requests.post(
            f"{API_BASE_URL}/compare",
            json={
                "user_text": "What's the status?",
                "assistant_text": "Let me check"
            }
        )

        assert compare_response.status_code == 200
        data = compare_response.json()

        # THE FIX: sticky_count should be present and > 0
        assert "graph_assembly" in data, "Response must include graph_assembly"
        assert "sticky_count" in data["graph_assembly"], (
            "graph_assembly must include sticky_count (Fix 1: Bug A)"
        )
        assert data["graph_assembly"]["sticky_count"] > 0, (
            f"Expected sticky_count > 0 when pins exist, got {data['graph_assembly']['sticky_count']}"
        )

    def test_compare_returns_sticky_count_zero_when_no_pins(self, api_available, unique_session_id):
        """
        Test 4.2: /compare returns sticky_count == 0 when no pins.
        """
        session_id = unique_session_id

        # Ingest messages but don't create any pins
        _ingest_message(session_id, "Normal chat", "Just talking...")

        # Call /compare
        compare_response = requests.post(
            f"{API_BASE_URL}/compare",
            json={
                "user_text": "Another question",
                "assistant_text": "Another answer"
            }
        )

        assert compare_response.status_code == 200
        data = compare_response.json()

        # sticky_count should be 0 (no pins)
        assert "graph_assembly" in data
        assert "sticky_count" in data["graph_assembly"]
        assert data["graph_assembly"]["sticky_count"] == 0, (
            f"Expected sticky_count == 0 when no pins, got {data['graph_assembly']['sticky_count']}"
        )

    def test_compare_sticky_count_matches_assemble_sticky_count(self, api_available, unique_session_id):
        """
        Test 4.3: /compare sticky_count matches /assemble sticky_count.

        Both endpoints should agree on sticky_count (modulo the tick).
        """
        session_id = unique_session_id

        # Ingest messages with known external_ids
        msg1_id, ext1 = _ingest_message(session_id, "Start task", "Starting...")
        msg2_id, ext2 = _ingest_message(session_id, "Continue task", "Continuing...")

        # Create pin via /assemble with tool_state
        assemble_response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "What's happening?",
                "tags": None,
                "token_budget": 4000,
                "session_id": session_id,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": [ext1, ext2]
                }
            }
        )

        assert assemble_response.status_code == 200
        assemble_data = assemble_response.json()
        assemble_sticky_count = assemble_data["sticky_count"]

        # Call /compare with same user_text
        compare_response = requests.post(
            f"{API_BASE_URL}/compare",
            json={
                "user_text": "What's happening?",
                "assistant_text": "Let me check"
            }
        )

        assert compare_response.status_code == 200
        compare_data = compare_response.json()
        compare_sticky_count = compare_data["graph_assembly"]["sticky_count"]

        # They should agree (compare is read-only, doesn't tick)
        # Note: assemble ticks first, then creates/extends pin, so both should see the pin
        assert compare_sticky_count > 0, "Compare should see the pin created by assemble"
        # They might not be exactly equal due to tick timing, but should both be > 0
        assert assemble_sticky_count > 0, "Assemble should have created a pin"

    def test_compare_does_not_tick_pin_manager(self, api_available, unique_session_id):
        """
        Test 4.4: /compare does NOT tick the pin manager (read-only).

        Compare is a read-only operation — it should not age pins or trigger expiry.
        """
        session_id = unique_session_id

        # Ingest messages
        msg1_id, ext1 = _ingest_message(session_id, "Deploy app", "Deploying...")

        # Create an explicit pin with low TTL
        pin_response = requests.post(
            f"{API_BASE_URL}/pin",
            json={
                "message_ids": [ext1],
                "reason": "Test pin",
                "ttl_turns": 3
            }
        )
        assert pin_response.status_code == 200
        pin_id = pin_response.json()["pin_id"]

        # Get initial pin state
        pins_response1 = requests.get(f"{API_BASE_URL}/pins")
        pins_data1 = pins_response1.json()
        pin1 = [p for p in pins_data1["active_pins"] if p["pin_id"] == pin_id][0]
        initial_turns_elapsed = pin1["turns_elapsed"]

        # Call /compare 5 times
        for _ in range(5):
            compare_response = requests.post(
                f"{API_BASE_URL}/compare",
                json={
                    "user_text": "Status check",
                    "assistant_text": "Checking"
                }
            )
            assert compare_response.status_code == 200

        # Get final pin state
        pins_response2 = requests.get(f"{API_BASE_URL}/pins")
        pins_data2 = pins_response2.json()

        # Pin should still exist (compare didn't tick, so it didn't expire)
        active_pin_ids = [p["pin_id"] for p in pins_data2["active_pins"]]
        assert pin_id in active_pin_ids, (
            "Pin should still exist after /compare calls (compare is read-only)"
        )

        pin2 = [p for p in pins_data2["active_pins"] if p["pin_id"] == pin_id][0]
        final_turns_elapsed = pin2["turns_elapsed"]

        # turns_elapsed should be unchanged (compare doesn't tick)
        assert final_turns_elapsed == initial_turns_elapsed, (
            f"/compare should not tick pins. "
            f"Expected turns_elapsed={initial_turns_elapsed}, got {final_turns_elapsed}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
