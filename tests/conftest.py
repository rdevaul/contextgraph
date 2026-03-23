"""
Pytest configuration for context graph tests.

Defines markers and shared fixtures.
"""

import sys
from pathlib import Path
import pytest
import uuid
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "integration: Integration tests that require the API server running"
    )
    config.addinivalue_line(
        "markers",
        "regression: Regression tests for specific bugs"
    )
    config.addinivalue_line(
        "markers",
        "plugin_contract: Plugin contract tests that verify TypeScript files"
    )
    config.addinivalue_line(
        "markers",
        "e2e: End-to-end smoke tests"
    )
    config.addinivalue_line(
        "markers",
        "sticky: Sticky thread tests"
    )
    config.addinivalue_line(
        "markers",
        "slow: Tests that take >5 seconds"
    )
    config.addinivalue_line(
        "markers",
        "compare: Tests for /compare endpoint"
    )


@pytest.fixture
def unique_session_id():
    """Generate unique session ID for test isolation."""
    return f"test-{uuid.uuid4()}"


@pytest.fixture
def temp_log_path(tmp_path):
    """Use a temp log path for comparison log tests."""
    return tmp_path / "comparison-log.jsonl"


@pytest.fixture(scope="module")
def api_available():
    """Check if the API is available. Skip tests if not running."""
    try:
        response = requests.get("http://localhost:8300/health", timeout=2)
        return response.status_code == 200
    except requests.RequestException:
        pytest.skip("API is not running on port 8300. Start with: python3 -m api.server")


@pytest.fixture
def in_memory_store():
    """Create an in-memory MessageStore for testing."""
    from store import MessageStore
    return MessageStore(":memory:")


@pytest.fixture
def sample_messages():
    """Provide sample messages for testing."""
    from store import Message
    import time

    base_time = time.time() - 3600  # 1 hour ago

    return [
        Message.new(
            session_id="test",
            user_id="user1",
            timestamp=base_time,
            user_text="How do I deploy to production?",
            assistant_text="Use the deploy script in scripts/deploy.sh",
            tags=["devops", "deployment"],
            token_count=50,
        ),
        Message.new(
            session_id="test",
            user_id="user1",
            timestamp=base_time + 60,
            user_text="What's the nginx config?",
            assistant_text="The nginx config is in /etc/nginx/sites-available/",
            tags=["networking", "devops"],
            token_count=40,
        ),
        Message.new(
            session_id="test",
            user_id="user1",
            timestamp=base_time + 120,
            user_text="[cron:abc-123] Daily backup completed",
            assistant_text="Acknowledged",
            tags=["monitoring"],
            token_count=20,
            is_automated=True,
        ),
    ]
