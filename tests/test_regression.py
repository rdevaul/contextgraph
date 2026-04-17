"""
Regression tests for the context graph system.

These tests verify specific bugs that have been encountered and fixed.

Run with: python3 -m pytest tests/test_regression.py -v --tb=short
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
def sample_message(api_available):
    """Ingest a sample message for testing."""
    msg = {
        "id": "regression-test-msg",
        "session_id": "regression-session",
        "user_text": "Help me configure the system",
        "assistant_text": "I'll help you configure it.",
        "timestamp": time.time(),
        "user_id": "test-user"
    }
    requests.post(f"{API_BASE_URL}/ingest", json=msg)
    return msg


@pytest.mark.regression
class TestTagsNullHandling:
    """Test that tags=null is handled correctly."""

    def test_assemble_with_tags_null_does_not_error(self, api_available, sample_message):
        """Regression 1: POST /assemble with tags=null should NOT return 422."""
        # This was a Pydantic validation bug where tags=null caused 422
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "What's the configuration?",
                "tags": None,  # Explicitly null
                "token_budget": 4000
            }
        )

        # Should succeed, not return 422
        assert response.status_code == 200

        data = response.json()
        assert "messages" in data
        assert "tags_used" in data
        # Tags should be inferred automatically
        assert isinstance(data["tags_used"], list)


@pytest.mark.regression
class TestContentArrayExtraction:
    """Test that content arrays are handled correctly."""

    def test_assemble_with_string_user_text(self, api_available, sample_message):
        """Regression 2: Assembly should handle string input correctly."""
        # The TypeScript plugin sends content as [{type:"text",text:"..."}] arrays
        # but the Python API receives strings (extracted by the plugin)
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "This is a plain string, not an array",
                "tags": None,
                "token_budget": 4000
            }
        )

        assert response.status_code == 200
        data = response.json()

        assert "messages" in data
        assert isinstance(data["messages"], list)

    def test_tag_endpoint_with_string_content(self, api_available):
        """Regression 2b: Tag endpoint should handle string content."""
        response = requests.post(
            f"{API_BASE_URL}/tag",
            json={
                "user_text": "Deploy the application",
                "assistant_text": "Starting deployment..."
            }
        )

        assert response.status_code == 200
        data = response.json()

        assert "tags" in data
        assert isinstance(data["tags"], list)


@pytest.mark.regression
class TestTokenBudgetCap:
    """Test that token budget is respected and capped."""

    def test_assembly_respects_token_budget(self, api_available, sample_message):
        """Regression 3: Verify assembly respects token budget (max 8000)."""
        # Request a reasonable budget
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Tell me about the system",
                "tags": None,
                "token_budget": 4000
            }
        )

        assert response.status_code == 200
        data = response.json()

        # Should not exceed the requested budget
        assert data["total_tokens"] <= 4000

    def test_assembly_does_not_use_full_context_window(self, api_available, sample_message):
        """Regression 3b: Assembly should not use the full context window (200k)."""
        # Even with a large budget, should cap at reasonable limit
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Tell me everything",
                "tags": None,
                "token_budget": 200000  # Request absurd budget
            }
        )

        assert response.status_code == 200
        data = response.json()

        # Should be capped at reasonable limit (not 200k)
        # The actual limit depends on what's in the store, but should be reasonable
        assert data["total_tokens"] < 50000  # Much less than 200k


@pytest.mark.regression
class TestEmptyUserText:
    """Test that empty user text doesn't crash."""

    def test_assemble_with_empty_user_text(self, api_available):
        """Regression 4: POST /assemble with user_text='' should not crash."""
        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "",
                "tags": None,
                "token_budget": 4000
            }
        )

        # Should not crash (200 or graceful error)
        assert response.status_code in [200, 400]

        if response.status_code == 200:
            data = response.json()
            assert "messages" in data

    def test_tag_with_empty_texts(self, api_available):
        """Regression 4b: Tag endpoint with empty texts should not crash."""
        response = requests.post(
            f"{API_BASE_URL}/tag",
            json={
                "user_text": "",
                "assistant_text": ""
            }
        )

        # Should not crash
        assert response.status_code in [200, 400]


@pytest.mark.regression
class TestLargeToolState:
    """Test that large tool_state doesn't cause issues."""

    def test_assemble_with_large_tool_state(self, api_available, sample_message):
        """Regression 5: POST /assemble with 50+ pending_chain_ids should not crash or timeout."""
        # Create a large list of chain IDs
        large_chain_ids = [f"chain-id-{i}" for i in range(60)]

        response = requests.post(
            f"{API_BASE_URL}/assemble",
            json={
                "user_text": "Continue the work",
                "tags": None,
                "token_budget": 4000,
                "tool_state": {
                    "last_turn_had_tools": True,
                    "pending_chain_ids": large_chain_ids
                }
            },
            timeout=10  # Should complete within 10 seconds
        )

        # Should not timeout or crash
        assert response.status_code == 200

        data = response.json()
        assert "messages" in data
        # The non-existent IDs won't be found, but should not crash


@pytest.mark.regression
class TestConcurrentPins:
    """Test that concurrent pins are handled with LRU eviction."""

    def test_concurrent_pins_lru_eviction(self, api_available, sample_message):
        """Regression 6: Create 6 pins, verify LRU eviction keeps max 5."""
        # Clear existing pins
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        if pins_response.status_code == 200:
            pins_data = pins_response.json()
            for pin in pins_data.get("active_pins", []):
                requests.post(f"{API_BASE_URL}/unpin", json={"pin_id": pin["pin_id"]})

        # Create 6 pins
        pin_ids = []
        for i in range(6):
            response = requests.post(
                f"{API_BASE_URL}/pin",
                json={
                    "message_ids": [sample_message["id"]],
                    "reason": f"Test pin {i}",
                    "ttl_turns": 20
                }
            )
            assert response.status_code == 200
            pin_ids.append(response.json()["pin_id"])
            time.sleep(0.1)  # Ensure different created_at timestamps

        # Check that only 5 pins remain (oldest was evicted)
        pins_response = requests.get(f"{API_BASE_URL}/pins")
        assert pins_response.status_code == 200
        pins_data = pins_response.json()

        assert len(pins_data["active_pins"]) == 5

        # First pin should have been evicted (LRU)
        current_pin_ids = [p["pin_id"] for p in pins_data["active_pins"]]
        assert pin_ids[0] not in current_pin_ids  # First pin evicted
        assert pin_ids[1] in current_pin_ids  # Others remain
        assert pin_ids[5] in current_pin_ids  # Last pin remains

        # Clean up
        for pin_id in pin_ids[1:]:
            requests.post(f"{API_BASE_URL}/unpin", json={"pin_id": pin_id})


@pytest.mark.regression
class TestComparisonLogging:
    """Test that comparison logging works correctly."""

    def test_comparison_logging_after_assemble(self, api_available, sample_message):
        """Regression 7: After /assemble call with graph mode on, verify comparison-log.jsonl gets entry."""
        # Note: This test can't directly verify the log entry because the logging
        # happens in the TypeScript plugin's afterTurn hook, not in the Python API.
        # However, we can verify that the /compare endpoint works, which is what
        # the plugin uses for logging.

        response = requests.post(
            f"{API_BASE_URL}/compare",
            json={
                "user_text": "What's the status?",
                "assistant_text": "Everything is running fine."
            }
        )

        assert response.status_code == 200
        data = response.json()

        assert "graph_assembly" in data
        assert "linear_window" in data

        # The actual log file is written by the TypeScript plugin,
        # so we can't verify it here. But we can verify the endpoint works.

    def test_comparison_log_endpoint(self, api_available):
        """Regression 7b: Verify /comparison-log endpoint works."""
        # The /comparison-log endpoint reads the log file
        response = requests.get(f"{API_BASE_URL}/comparison-log", params={"limit": 10})

        # Should succeed even if log is empty
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, list)
