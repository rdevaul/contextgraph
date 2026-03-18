"""
Pytest configuration for context graph tests.

Defines markers and shared fixtures.
"""

import sys
from pathlib import Path
import pytest

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
