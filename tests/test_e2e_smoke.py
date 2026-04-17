"""
End-to-end smoke test for the context graph system.

This test simulates a realistic multi-turn conversation with tool calls
to verify the full pipeline works correctly.

Run with: python3 -m pytest tests/test_e2e_smoke.py -v --tb=short
"""

import pytest
import requests
import time


API_BASE_URL = "http://localhost:8302"


@pytest.fixture(scope="module")
def api_available():
    """Check if the API is available. Skip tests if not running."""
    try:
        response = requests.get(f"{API_BASE_URL}/health", timeout=2)
        return response.status_code == 200
    except requests.RequestException:
        pytest.skip("API is not running on port 8302. Start with: python3 -m api.server")


@pytest.fixture(scope="module")
def clean_state(api_available):
    """Clean up pins before starting."""
    # Clear all existing pins
    pins_response = requests.get(f"{API_BASE_URL}/pins")
    if pins_response.status_code == 200:
        pins_data = pins_response.json()
        for pin in pins_data.get("active_pins", []):
            requests.post(f"{API_BASE_URL}/unpin", json={"pin_id": pin["pin_id"]})

    return True


@pytest.mark.e2e
class TestMultiTurnConversation:
    """End-to-end smoke test simulating a multi-turn conversation."""

    def test_multi_turn_conversation_with_tool_calls(self, api_available, clean_state):
        """
        E2E test: Simulate a multi-turn conversation with tool calls.

        Flow:
        1. Turn 1: User asks to deploy (no tool state) - normal assembly
        2. Turn 2: System is deploying + tool_state with tool calls - sticky pin created
        3. Turn 3: User asks for updates - sticky pin still active
        4. Turn 4+: Several turns pass - pin eventually expires
        """

        # ── Turn 1: Initial request (no tools yet) ──────────────────────────
        print("\n[Turn 1] Initial request - no tool state")

        # Ingest the first exchange
        msg1_id = f"e2e-msg-1-{time.time()}"
        msg1_external_id = f"e2e-external-1-{time.time()}"
        requests.post(
            f"{API_BASE_URL}/ingest",
            json={
                "id": msg1_id,
                "session_id": "e2e-session",
                "user_text": "Deploy the app to production",
                "assistant_text": "I'll help you deploy the app. Let me start the deployment process.",
                "timestamp": time.time(),
                "user_id": "e2e-user",
                "external_id": msg1_external_id
            }
        )

        # Assemble for the next turn (no tool state yet)
        response1 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Deploy the app to production",
                "tags": None,
                "token_budget": 4000
            }
        )

        assert response1.status_code == 200
        data1 = response1.json()

        # Should have normal assembly (no sticky pins)
        assert data1["sticky_count"] == 0
        assert data1["recency_count"] >= 0
        assert data1["topic_count"] >= 0

        print(f"  → sticky: {data1['sticky_count']}, recency: {data1['recency_count']}, topic: {data1['topic_count']}")

        # ── Turn 2: Tool calls in progress ─────────────────────────────────
        print("\n[Turn 2] Tool calls in progress - creating sticky pin")

        # Ingest turn 2 (assistant used tools)
        msg2_id = f"e2e-msg-2-{time.time()}"
        msg2_external_id = f"e2e-external-2-{time.time()}"
        requests.post(
            f"{API_BASE_URL}/ingest",
            json={
                "id": msg2_id,
                "session_id": "e2e-session",
                "user_text": "Checking deployment status...",
                "assistant_text": "Running deployment checks... [tool results here]",
                "timestamp": time.time(),
                "user_id": "e2e-user",
                "external_id": msg2_external_id
            }
        )

        # Assemble with tool state (simulating active tool chain) using external_ids
        response2 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Checking deployment status...",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": [msg1_external_id, msg2_external_id]
                }
            }
        )

        assert response2.status_code == 200
        data2 = response2.json()

        print(f"  → sticky: {data2['sticky_count']}, recency: {data2['recency_count']}, topic: {data2['topic_count']}")

        # Check that a pin was created
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        assert pins_response.status_code == 200
        pins_data = pins_response.json()

        assert "active_pins" in pins_data
        assert len(pins_data["active_pins"]) >= 1, "Expected at least one sticky pin to be created"

        # Find the tool_chain pin
        tool_chain_pin = None
        for pin in pins_data["active_pins"]:
            if pin["pin_type"] == "tool_chain":
                tool_chain_pin = pin
                break

        assert tool_chain_pin is not None, "Expected a tool_chain pin to be created"
        print(f"  → Created pin: {tool_chain_pin['pin_id']} (TTL: {tool_chain_pin['ttl_turns']} turns)")

        # ── Turn 3: User follows up (pin still active) ─────────────────────
        print("\n[Turn 3] User asks for updates - sticky pin should still be active")

        # Ingest turn 3
        msg3_id = f"e2e-msg-3-{time.time()}"
        requests.post(
            f"{API_BASE_URL}/ingest",
            json={
                "id": msg3_id,
                "session_id": "e2e-session",
                "user_text": "Any updates on the deployment?",
                "assistant_text": "The deployment is 80% complete. Almost done!",
                "timestamp": time.time(),
                "user_id": "e2e-user"
            }
        )

        # Assemble (no tool state - normal user query)
        response3 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Any updates on the deployment?",
                "tags": None,
                "token_budget": 4000
            }
        )

        assert response3.status_code == 200
        data3 = response3.json()

        # Sticky pin should still be active (pinned messages in context)
        # With external_id support, sticky_count should be > 0
        print(f"  → sticky: {data3['sticky_count']}, recency: {data3['recency_count']}, topic: {data3['topic_count']}")
        assert data3['sticky_count'] > 0, "Expected sticky messages to be in context"

        # Verify pin still exists
        pins_response3 = requests.get(f"{API_BASE_URL}/pins")
        pins_data3 = pins_response3.json()
        pin_ids3 = [p["pin_id"] for p in pins_data3["active_pins"]]
        assert tool_chain_pin["pin_id"] in pin_ids3, "Pin should still be active after turn 3"

        # ── Turn 4+: Multiple turns pass, pin expires ──────────────────────
        print("\n[Turn 4+] Multiple turns pass - pin should eventually expire")

        # The pin has TTL of 10 turns (default for tool_chain)
        # We need to call /assemble multiple times to tick it down
        ttl_turns = tool_chain_pin["ttl_turns"]

        for i in range(ttl_turns + 2):  # Extra turns to ensure expiry
            response_tick = requests.post(
                f"{API_BASE_URL}/assemble",
                json={
                    "user_text": f"Turn {i + 4} query",
                    "tags": None,
                    "token_budget": 4000
                }
            )
            assert response_tick.status_code == 200

            # Check if pin expired in this turn
            tick_data = response_tick.json()
            if tool_chain_pin["pin_id"] in tick_data.get("expired_pins", []):
                print(f"  → Pin expired after {i + 1} additional turns")
                break

        # ── Verify pin is gone ─────────────────────────────────────────────
        print("\n[Final] Verify pin has expired")

        pins_response_final = requests.get(f"{API_BASE_URL}/pins")
        pins_data_final = pins_response_final.json()
        pin_ids_final = [p["pin_id"] for p in pins_data_final["active_pins"]]

        assert tool_chain_pin["pin_id"] not in pin_ids_final, "Pin should have expired after TTL turns"

        print("  → Pin successfully expired ✓")

        # ── Verify system still works normally ─────────────────────────────
        print("\n[Final] Verify system works normally after pin expiry")

        response_final = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "What's the final status?",
                "tags": None,
                "token_budget": 4000
            }
        )

        assert response_final.status_code == 200
        data_final = response_final.json()

        # Should work normally without sticky pins
        assert data_final["sticky_count"] == 0
        assert len(data_final["messages"]) == data_final["recency_count"] + data_final["topic_count"]

        print(f"  → sticky: {data_final['sticky_count']}, recency: {data_final['recency_count']}, topic: {data_final['topic_count']}")
        print("\n✓ E2E smoke test passed: Full pipeline works correctly!")
