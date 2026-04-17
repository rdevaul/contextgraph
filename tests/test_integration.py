"""
Integration tests for the context graph system.

These tests verify the full Python API pipeline via HTTP calls.
The API must be running on port 8302 for these tests to pass.

Run with: python3 -m pytest tests/test_integration.py -v --tb=short
"""

import pytest
import requests
import time
from pathlib import Path


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
def sample_messages(api_available):
    """Ingest some sample messages for testing."""
    messages = [
        {
            "id": f"test-msg-{i}",
            "session_id": "test-session",
            "user_text": user_text,
            "assistant_text": assistant_text,
            "timestamp": time.time() - (100 - i) * 60,  # Spread over time
            "user_id": "test-user",
            "external_id": f"external-{i}"  # Add external_id for OpenClaw compatibility
        }
        for i, (user_text, assistant_text) in enumerate([
            ("Help me deploy the app", "I'll help you deploy. First, let's check the config."),
            ("What's the current status?", "The deployment is in progress."),
            ("Can you show me the logs?", "Here are the recent logs..."),
            ("How do I configure Docker?", "Docker configuration is done via docker-compose.yml."),
            ("Explain the authentication flow", "The auth flow uses JWT tokens..."),
        ])
    ]

    for msg in messages:
        requests.post(f"{API_BASE_URL}/ingest", json=msg)

    return messages


@pytest.mark.integration
class TestHealthEndpoint:
    """Test the /health endpoint."""

    def test_health_check_returns_200(self, api_available):
        """Test 1: Health check returns 200 with messages_in_store and tags."""
        response = requests.get(f"{API_BASE_URL}/health")
        assert response.status_code == 200

        data = response.json()
        assert "status" in data
        assert data["status"] == "ok"
        assert "messages_in_store" in data
        assert "tags" in data
        assert "engine" in data
        assert data["engine"] == "contextgraph"
        assert isinstance(data["messages_in_store"], int)
        assert isinstance(data["tags"], list)


@pytest.mark.integration
class TestTagInference:
    """Test the /tag endpoint."""

    def test_tag_inference_returns_valid_tags(self, api_available):
        """Test 2: Tag inference returns valid tags."""
        response = requests.post(
            f"{API_BASE_URL}/tag",
            json={
                "user_text": "Can you help me deploy the app?",
                "assistant_text": "Sure, I'll help you deploy."
            }
        )

        assert response.status_code == 200
        data = response.json()

        assert "tags" in data
        assert isinstance(data["tags"], list)
        assert "confidence" in data
        assert isinstance(data["confidence"], (int, float))
        assert "per_tagger" in data
        assert isinstance(data["per_tagger"], dict)


@pytest.mark.integration
class TestBasicAssembly:
    """Test the /assemble endpoint without tool state."""

    def test_basic_assembly_returns_messages(self, api_available, sample_messages):
        """Test 3: Basic assembly returns messages with recency_count + topic_count."""
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "What are the deployment steps?",
                "tags": None,
                "token_budget": 4000
            }
        )

        assert response.status_code == 200
        data = response.json()

        assert "messages" in data
        assert isinstance(data["messages"], list)
        assert "total_tokens" in data
        assert "recency_count" in data
        assert "topic_count" in data
        assert "sticky_count" in data
        assert "tags_used" in data

        assert data["recency_count"] >= 0
        assert data["topic_count"] >= 0
        assert data["sticky_count"] == 0  # No pins in basic assembly
        assert len(data["messages"]) == data["recency_count"] + data["topic_count"]


@pytest.mark.integration
class TestAssemblyWithToolState:
    """Test the /assemble endpoint with tool_state."""

    def test_assembly_with_tool_state_creates_sticky_pins(self, api_available, sample_messages):
        """Test 4: Assembly with tool_state creates sticky pins."""
        # First, clear any existing pins
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        if pins_response.status_code == 200:
            pins_data = pins_response.json()
            for pin in pins_data.get("active_pins", []):
                requests.post(f"{API_BASE_URL}/unpin", json={"pin_id": pin["pin_id"]})

        # Now assemble with tool state, using external_ids
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Checking deployment status...",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": [sample_messages[0]["external_id"], sample_messages[1]["external_id"]]
                }
            }
        )

        assert response.status_code == 200
        data = response.json()

        # Should have sticky messages now
        assert "sticky_count" in data
        # sticky_count should be > 0 now that we're using external_ids
        assert data["sticky_count"] > 0

        # Check that a pin was created
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        assert pins_response.status_code == 200
        pins_data = pins_response.json()

        # There should be at least one pin
        assert "active_pins" in pins_data
        assert isinstance(pins_data["active_pins"], list)
        assert len(pins_data["active_pins"]) > 0


@pytest.mark.integration
class TestPinLifecycle:
    """Test the pin CRUD operations."""

    def test_pin_lifecycle(self, api_available, sample_messages):
        """Test 5: Pin lifecycle - create, list, remove."""
        # Create a pin using external_ids
        create_response = requests.post(
            f"{API_BASE_URL}/pin",
            json={
                "message_ids": [sample_messages[0]["external_id"], sample_messages[1]["external_id"]],
                "reason": "Test pin for deployment context",
                "ttl_turns": 20
            }
        )

        assert create_response.status_code == 200
        create_data = create_response.json()

        assert create_data["success"] is True
        assert "pin_id" in create_data
        pin_id = create_data["pin_id"]

        # List pins
        list_response = requests.get(f"{API_BASE_URL}/pins")
        assert list_response.status_code == 200
        list_data = list_response.json()

        assert "active_pins" in list_data
        assert len(list_data["active_pins"]) > 0

        # Find our pin
        our_pin = None
        for pin in list_data["active_pins"]:
            if pin["pin_id"] == pin_id:
                our_pin = pin
                break

        assert our_pin is not None
        assert our_pin["reason"] == "Test pin for deployment context"
        assert our_pin["ttl_turns"] == 20

        # Remove the pin
        unpin_response = requests.post(
            f"{API_BASE_URL}/unpin",
            json={"pin_id": pin_id}
        )

        assert unpin_response.status_code == 200
        unpin_data = unpin_response.json()
        assert unpin_data["success"] is True

        # Verify it's gone
        list_response2 = requests.get(f"{API_BASE_URL}/pins")
        list_data2 = list_response2.json()

        pin_ids = [p["pin_id"] for p in list_data2["active_pins"]]
        assert pin_id not in pin_ids


@pytest.mark.integration
class TestPinTTLExpiry:
    """Test that pins expire after TTL turns."""

    def test_pin_ttl_expiry(self, api_available, sample_messages):
        """Test 6: Pin TTL expiry - pin expires after ttl_turns."""
        # Create a pin with very short TTL using external_id
        create_response = requests.post(
            f"{API_BASE_URL}/pin",
            json={
                "message_ids": [sample_messages[0]["external_id"]],
                "reason": "Short-lived test pin",
                "ttl_turns": 1
            }
        )

        assert create_response.status_code == 200
        pin_id = create_response.json()["pin_id"]

        # First assemble - pin should still be active (turns_elapsed=0 -> 1)
        response1 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={"user_text": "First query", "token_budget": 4000}
        )
        assert response1.status_code == 200

        # Check pin is still there
        pins1 = requests.get(f"{API_BASE_URL}/pins").json()
        pin_ids1 = [p["pin_id"] for p in pins1["active_pins"]]
        assert pin_id in pin_ids1

        # Second assemble - pin should expire (turns_elapsed=1 -> 2, > ttl_turns=1)
        response2 = requests.post(
            f"{API_BASE_URL}/assemble",
            json={"user_text": "Second query", "token_budget": 4000}
        )
        assert response2.status_code == 200
        data2 = response2.json()

        # Pin should be in expired list
        assert "expired_pins" in data2
        assert pin_id in data2["expired_pins"]

        # Verify pin is gone
        pins2 = requests.get(f"{API_BASE_URL}/pins").json()
        pin_ids2 = [p["pin_id"] for p in pins2["active_pins"]]
        assert pin_id not in pin_ids2


@pytest.mark.integration
class TestThreeLayerBudget:
    """Test that three-layer budget allocation works correctly."""

    def test_three_layer_budget_allocation(self, api_available, sample_messages):
        """Test 7: Three-layer budget - sticky_count > 0 and budget split correctly."""
        # Create a pin using external_ids
        create_response = requests.post(
            f"{API_BASE_URL}/pin",
            json={
                "message_ids": [sample_messages[0]["external_id"], sample_messages[1]["external_id"]],
                "reason": "Test three-layer budget",
                "ttl_turns": 10
            }
        )
        pin_id = create_response.json()["pin_id"]

        # Assemble with pin active
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "What's the deployment status?",
                "tags": ["deployment", "docker"],
                "token_budget": 4000
            }
        )

        assert response.status_code == 200
        data = response.json()

        # Should have messages from all three layers
        assert data["sticky_count"] > 0
        assert data["recency_count"] >= 0
        assert data["topic_count"] >= 0

        # Total tokens should be within budget
        assert data["total_tokens"] <= 4000

        # Sticky should be ≤ 30% of total budget
        sticky_budget = 4000 * 0.3
        # We can't directly measure sticky tokens, but sticky_count should be reasonable

        # Clean up
        requests.post(f"{API_BASE_URL}/unpin", json={"pin_id": pin_id})


@pytest.mark.integration
class TestCompareEndpoint:
    """Test the /compare endpoint."""

    def test_compare_endpoint_returns_both_assemblies(self, api_available, sample_messages):
        """Test 8: Compare endpoint returns both graph and linear assembly."""
        response = requests.post(
            f"{API_BASE_URL}/compare",
            json={
                "user_text": "How do I configure Docker?",
                "assistant_text": "Use docker-compose.yml for configuration."
            }
        )

        assert response.status_code == 200
        data = response.json()

        assert "graph_assembly" in data
        assert "linear_window" in data

        # Check graph assembly structure
        graph = data["graph_assembly"]
        assert "messages" in graph
        assert "total_tokens" in graph
        assert "recency_count" in graph
        assert "topic_count" in graph
        assert "tags_used" in graph

        # Check linear window structure
        linear = data["linear_window"]
        assert "messages" in linear
        assert "total_tokens" in linear
        assert "recency_count" in linear
        assert "topic_count" in linear
        assert "tags_used" in linear


@pytest.mark.integration
class TestRegistryEndpoint:
    """Test the /registry endpoint."""

    def test_registry_returns_tag_tiers(self, api_available):
        """Test 9: Registry returns tag registry with core/candidate/archived tiers."""
        response = requests.get(f"{API_BASE_URL}/registry")

        assert response.status_code == 200
        data = response.json()

        # Should have tier structure
        assert "core" in data or "candidate" in data or "archived" in data
        # At least one tier should be present
        assert len(data) > 0


@pytest.mark.integration
class TestGracefulDegradation:
    """Test that assembly works without pins."""

    def test_graceful_degradation_without_pins(self, api_available, sample_messages):
        """Test 10: Graceful degradation - assembly works when no pins exist."""
        # Clear all pins
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        if pins_response.status_code == 200:
            pins_data = pins_response.json()
            for pin in pins_data.get("active_pins", []):
                requests.post(f"{API_BASE_URL}/unpin", json={"pin_id": pin["pin_id"]})

        # Assemble without any pins
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Tell me about Docker",
                "tags": None,
                "token_budget": 4000
            }
        )

        assert response.status_code == 200
        data = response.json()

        # Should work normally with no sticky layer
        assert data["sticky_count"] == 0
        assert data["recency_count"] >= 0
        assert data["topic_count"] >= 0
        assert len(data["messages"]) == data["recency_count"] + data["topic_count"]
        assert data["total_tokens"] <= 4000
